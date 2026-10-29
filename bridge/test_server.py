# -*- coding: utf-8 -*-
"""桥接服务的回归测试。

跑法（在仓库根目录）：
    python bridge/test_server.py

覆盖的都是审查报告里 P0 级的问题，每条都对应一次真实修复：
  1. 并发：ThreadingHTTPServer 每请求一线程，旧实现的 sync 整批分支、
     _drop_appwatch_dupes、_trace_row_for 全在锁外读写 STATE，
     与持锁的 prune_loop 混跑会抛 "dictionary changed size during iteration"。
  2. 来源校验：旧实现对所有响应回 Access-Control-Allow-Origin: *，
     且 POST 不校验 Origin —— 用户浏览的任意网页都能驱动桌宠说话。
  3. waiting 的时间戳：旧实现在 sync 分支里冻结 waiting 的 ts，
     于是一个真在等用户拍板的任务会在 30 分钟后被 TTL 当成失联删掉。
  4. phase 字段：细粒度状态要能从上报一路透传到 /api/state 与 SSE。

用系统分配的独立临时端口，不干扰正在运行的桌宠实例。全部 stdlib，退出码 0 = 全通过。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S                                     # noqa: E402

HOST, PORT = "127.0.0.1", 0
BASE = ""
PASS = FAIL = 0
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name, cond, got=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s   -> %r" % (name, got))


def post(path, obj, origin=None, raw=None):
    data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if origin is not None:
        h["Origin"] = origin
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with HTTP.open(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def get(path):
    with HTTP.open(BASE + path, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def reset():
    with S.COMMIT_LOCK:
        with S.LOCK:
            S.STATE.clear()


def read_sse(response):
    event = "message"
    data = []
    while True:
        line = response.readline()
        if not line:
            raise EOFError("SSE closed")
        line = line.decode("utf-8").rstrip("\r\n")
        if not line and data:
            return event, json.loads("\n".join(data))
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data.append(line[6:])


def subscriber_count():
    with S.SUBS_LOCK:
        return len(S.SUBS)


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    tick = threading.Event()
    while time.monotonic() < end:
        if predicate():
            return True
        tick.wait(0.01)
    return predicate()


def stream():
    return HTTP.open(BASE + "/api/stream", timeout=5)


def source_regressions():
    print("\n=== 8. sync 来源隔离与会话去重 ===")
    reset()
    for src, tid, status in (("trace", "tr", "running"), ("hook", "h", "waiting"),
                             ("hook", "err", "error"), ("", "legacy", "waiting"),
                             ("observer", "obs", "waiting")):
        S.apply_event({"platform": "Qoder", "task_id": tid, "src": src, "status": status})
    S.apply_event({"platform": "Other", "task_id": "x", "src": "trace"})

    def ids():
        return {t["id"] for t in get("/api/state")["tasks"]}

    S.apply_event({"platform": "Qoder", "type": "sync", "src": "hook",
                   "tasks": [{"task_id": "h", "status": "waiting"},
                             {"task_id": "err", "status": "error"}]})
    check("hook 非空 sync 保留 trace 和其他来源", ids() == {"tr", "h", "err", "legacy", "obs", "x"})
    S.apply_event({"platform": "Qoder", "type": "sync", "src": "trace",
                   "tasks": [{"task_id": "tr2", "status": "running"}]})
    check("trace sync 只撤销旧 trace，不吞独立 waiting/error",
          ids() == {"tr2", "h", "err", "legacy", "obs", "x"})
    S.apply_event({"platform": "Qoder", "task_id": "tr2", "src": "trace", "status": "done"})
    check("trace 单更新也保留独立 waiting/error",
          ids() == {"tr2", "h", "err", "legacy", "obs", "x"})
    S.apply_event({"platform": "Qoder", "type": "sync", "src": "hook", "tasks": []})
    check("hook 空 sync 不删 trace", ids() == {"tr2", "legacy", "obs", "x"})
    S.apply_event({"platform": "Qoder", "type": "sync", "tasks": []})
    check("无 src 的旧协议只清理无 src 来源", ids() == {"tr2", "obs", "x"})
    S.apply_event({"platform": "Qoder", "type": "sync", "src": "trace", "tasks": []})
    check("trace 空 sync 保留其他来源与其他平台", ids() == {"obs", "x"})

    for sync in (False, True):
        reset()
        S.apply_event({"platform": "Qoder", "task_id": "session", "src": "hook", "status": "waiting"})
        task = {"task_id": "session", "status": "running"}
        trace = {"platform": "Qoder", "src": "trace"}
        trace.update({"type": "sync", "tasks": [task]} if sync else task)
        S.apply_event(trace)
        rows = get("/api/state")["tasks"]
        check("同会话 trace %s 去重" % ("sync" if sync else "update"),
              len(rows) == 1 and rows[0]["src"] == "trace" and rows[0]["status"] == "running")
        S.apply_event({"platform": "Qoder", "task_id": "session", "src": "hook",
                       "status": "waiting", "ask": True})
        rows = get("/api/state")["tasks"]
        check("同会话 hook asking 能覆盖 trace", len(rows) == 1 and rows[0]["ask"])
        S.apply_event(trace)
        check("同会话 trace 恢复后不残留 waiting", len(get("/api/state")["tasks"]) == 1
              and get("/api/state")["tasks"][0]["status"] == "running")

    for trace_first in (False, True):
        reset()
        trace = {"platform": "Qoder", "task_id": "tr", "src": "trace"}
        if trace_first:
            S.apply_event(trace)
        for status in ("running", "waiting", "error"):
            S.apply_event({"platform": "Qoder", "task_id": status, "status": status, "src": "hook"})
        S.apply_event(trace)
        check("独立会话不按整个平台合并（trace %s到）" % ("先" if trace_first else "后"),
              ids() == {"tr", "running", "waiting", "error"})
        S.apply_event({"platform": "Qoder", "type": "sync", "src": "hook",
                       "tasks": [{"task_id": status, "status": status}
                                 for status in ("running", "waiting", "error")]})
        check("独立会话 hook sync 也不被吞", ids() == {"tr", "running", "waiting", "error"})

    reset()
    S.apply_event({"platform": "qoder", "task_id": "same", "src": "hook", "status": "error"})
    S.apply_event({"platform": "Qoder", "task_id": "same", "src": "trace"})
    check("平台大小写不影响同会话去重", len(get("/api/state")["tasks"]) == 1)
    S.apply_event({"platform": "qoder", "task_id": "same", "src": "hook", "status": "waiting"})
    rows = get("/api/state")["tasks"]
    check("大小写不同的 hook waiting 也不产生重复会话", len(rows) == 1 and rows[0]["status"] == "waiting")
    S.apply_event({"platform": "Qoder", "task_id": "same", "src": "trace"})
    S.apply_event({"platform": "qoder", "type": "sync", "src": "hook",
                   "tasks": [{"task_id": "same", "status": "running"}]})
    rows = get("/api/state")["tasks"]
    check("同会话普通 hook sync 让位且不删 trace", len(rows) == 1 and rows[0]["src"] == "trace")
    S.apply_event({"platform": "应用接入", "type": "sync", "src": "appwatch",
                   "tasks": [{"task_id": "app:qoder.exe"}, {"task_id": "app:other.exe"}]})
    check("应用观察 sync 仍只让位已接入软件", ids() == {"same", "app:other.exe"})
    S.apply_event({"platform": "Privacy", "task_id": "p", "prompt": "CHAT_SENTINEL",
                   "conversation": ["CHAT_SENTINEL"]})
    snap = get("/api/state")
    check("快照只含状态字段，不携带额外聊天载荷", set(snap) == {"tasks", "updated"}
          and "CHAT_SENTINEL" not in json.dumps(snap))

    for sync in (False, True):
        for trace_first in (False, True):
            for status in ("running", "done", "waiting", "error"):
                reset()
                trace = {"platform": "WorkBuddy", "task_id": "real-session", "src": "trace"}
                legacy = {"platform": "workbuddy"}
                task = {"task_id": "main", "status": status}
                legacy.update({"type": "sync", "tasks": [task]} if sync else task)
                for item in ((trace, legacy) if trace_first else (legacy, trace)):
                    post("/api/events", item)
                expected = {"real-session", "main"} if status in ("waiting", "error") else {"real-session"}
                check("legacy main %s sync=%s trace_first=%s" % (status, sync, trace_first),
                      ids() == expected, ids())
    for sync in (False, True):
        for status in ("running", "done"):
            reset()
            post("/api/events", {"platform": "workbuddy", "task_id": "main",
                                 "status": "waiting", "ask": True})
            post("/api/events", {"platform": "WorkBuddy", "task_id": "real-session",
                                 "src": "trace", "status": "done"})
            post("/api/events", {"platform": "workbuddy", "task_id": "other-session",
                                 "status": "waiting", "ask": True})
            update = {"task_id": "main", "status": status}
            event = {"platform": "workbuddy"}
            event.update({"type": "sync", "tasks": [update,
                          {"task_id": "other-session", "status": "waiting", "ask": True}]}
                         if sync else update)
            post("/api/events", event)
            check("resolved legacy wait %s sync=%s" % (status, sync),
                  ids() == {"real-session", "other-session"}, ids())
            rows = get("/api/state")["tasks"]
            check("resolved legacy update preserves trace completion",
                  next(t for t in rows if t["id"] == "real-session")["status"] == "done")
    reset()
    post("/api/events", {"platform": "Qoder", "task_id": "main", "src": "hook"})
    post("/api/events", {"platform": "Qoder", "task_id": "session", "src": "trace"})
    check("explicit hook main remains independent", ids() == {"main", "session"})


def ordering_regressions():
    print("\n=== 9. 提交、TTL 与 SSE 快照顺序 ===")
    for prune in (False, True):
        reset()
        S.apply_event({"platform": "Order", "task_id": "s", "progress": 0})
        if prune:
            with S.LOCK:
                S.STATE["Order::s"]["ts"] = time.time() - S.TTL - 1
        entered, release, started, done = (threading.Event() for _ in range(4))
        errors = []
        original = S.broadcast

        def blocked(event):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    errors.append("publish barrier timed out")
            original(event)

        def first():
            try:
                if prune:
                    S.prune_once()
                else:
                    S.apply_event({"platform": "Order", "task_id": "s", "progress": 1})
            except Exception as e:
                errors.append(type(e).__name__)

        def second():
            started.set()
            try:
                S.apply_event({"platform": "Order", "task_id": "s", "progress": 2})
            except Exception as e:
                errors.append(type(e).__name__)
            finally:
                done.set()

        with stream() as r, patch.object(S, "broadcast", blocked):
            read_sse(r)
            a, b = threading.Thread(target=first), threading.Thread(target=second)
            a.start()
            try:
                check("%s提交到发布之间可控暂停" % ("TTL" if prune else "更新"), entered.wait(3))
                unlocked = S.LOCK.acquire(timeout=0.5)
                if unlocked:
                    S.LOCK.release()
                check("广播时状态锁已释放", unlocked)
                b.start()
                started.wait(3)
                check("后一提交不能越过前一发布", not done.wait(0.1))
            finally:
                release.set()
                a.join(5)
                if b.ident is not None:
                    b.join(5)
            events = [read_sse(r)[1], read_sse(r)[1]]
            check("实际 SSE 顺序与提交一致", events[0]["type"] == ("removed" if prune else "task")
                  and (prune or events[0]["task"]["progress"] == 1)
                  and events[1]["task"]["progress"] == 2 and not errors, errors)
        check("顺序测试订阅清理", wait_for(lambda: subscriber_count() == 0))

    reset()
    S.apply_event({"platform": "Order", "task_id": "s", "progress": 0})
    captured, release, updated = (threading.Event() for _ in range(3))
    original_snapshot = S.snapshot
    result, errors = [], []

    def paused_snapshot():
        snap = original_snapshot()
        captured.set()
        if not release.wait(5):
            errors.append("snapshot barrier timed out")
        return snap

    def reader():
        try:
            with stream() as r:
                result.append(read_sse(r))
                result.append(read_sse(r))
        except Exception as e:
            errors.append(type(e).__name__)

    def update():
        S.apply_event({"platform": "Order", "task_id": "s", "progress": 1})
        updated.set()

    with patch.object(S, "snapshot", paused_snapshot):
        a, b = threading.Thread(target=reader), threading.Thread(target=update)
        a.start()
        try:
            check("初始快照捕获后暂停登记", captured.wait(3))
            b.start()
            check("快照与登记之间不能插入提交", not updated.wait(0.1))
        finally:
            release.set()
            a.join(5)
            if b.ident is not None:
                b.join(5)
    check("实际 SSE 先旧快照后新事件，无丢失倒序", not errors and len(result) == 2
          and result[0][0] == "snapshot" and result[0][1]["tasks"][0]["progress"] == 0
          and result[1][1]["task"]["progress"] == 1, errors)
    check("快照竞态测试订阅清理", wait_for(lambda: subscriber_count() == 0))

    reset()
    entered, release, connected = (threading.Event() for _ in range(3))
    original_broadcast = S.broadcast
    responses, errors = [], []

    def paused_publish(event):
        entered.set()
        release.wait(5)
        original_broadcast(event)

    def connect():
        try:
            responses.append(stream())
        except Exception as e:
            errors.append(type(e).__name__)
        finally:
            connected.set()

    with patch.object(S, "broadcast", paused_publish):
        writer = threading.Thread(target=lambda: S.apply_event(
            {"platform": "Order", "task_id": "s", "progress": 1}))
        reader = threading.Thread(target=connect)
        writer.start()
        try:
            check("订阅前的提交已暂停发布", entered.wait(3))
            reader.start()
            check("订阅不能跨越尚未发布的提交", not connected.wait(0.1))
        finally:
            release.set()
            writer.join(5)
            if reader.ident is not None:
                reader.join(5)
    try:
        if responses:
            kind, initial = read_sse(responses[0])
            S.apply_event({"platform": "Order", "task_id": "s", "progress": 2})
            following = read_sse(responses[0])[1]
            check("新快照之后不再重放订阅前旧事件", not errors and kind == "snapshot"
                  and initial["tasks"][0]["progress"] == 1 and following["task"]["progress"] == 2)
        else:
            check("新快照之后不再重放订阅前旧事件", False, errors)
    finally:
        for response in responses:
            response.close()
    check("发布与登记竞态测试订阅清理", wait_for(lambda: subscriber_count() == 0))


def resource_regressions():
    print("\n=== 10. 慢订阅隔离、连接上限与写超时 ===")
    reset()
    gated, release = threading.Event(), threading.Event()
    original_headers = S.Handler.end_headers
    slow_sub = []

    def pause_headers(handler):
        original_headers(handler)
        if handler.path == "/api/stream" and not gated.is_set():
            with S.SUBS_LOCK:
                slow_sub.append(S.SUBS[-1])
            gated.set()
            release.wait(5)

    with patch.object(S, "SSE_QUEUE_SIZE", 2), patch.object(S, "SSE_MAX_CONNECTIONS", 2), \
            patch.object(S.Handler, "end_headers", pause_headers):
        slow = stream()
        fast = None
        try:
            check("慢订阅已登记且暂停发送", gated.wait(3))
            fast = stream()
            read_sse(fast)
            t0 = time.monotonic()
            values = []
            for i in range(8):
                code, _ = post("/api/events", {"platform": "Bound", "task_id": "s", "progress": i})
                values.append(read_sse(fast)[1]["task"]["progress"])
                if code != 200:
                    break
            check("慢客户端不阻塞 POST 或正常客户端", values == list(range(8))
                  and time.monotonic() - t0 < 3, values)
            check("溢出标记关闭且队列有界", slow_sub[0].closed.is_set()
                  and slow_sub[0].queue.qsize() == 2)
            try:
                with stream():
                    check("达到连接上限拒绝新订阅", False)
            except urllib.error.HTTPError as e:
                check("达到连接上限拒绝新订阅", e.code == 503, e.code)
                e.close()
            check("关闭中的连接仍占名额", subscriber_count() == 2)
            release.set()
            kind, initial = read_sse(slow)
            check("溢出连接保留初始快照协议", kind == "snapshot" and initial["tasks"] == [])
            check("溢出实际断流而非静默丢事件", slow.read() == b"")
            check("溢出订阅完成清理", wait_for(lambda: subscriber_count() == 1))
            with stream() as recovered:
                kind, snap = read_sse(recovered)
                check("重连快照恢复最新状态", kind == "snapshot" and snap["tasks"][0]["progress"] == 7)
                S.apply_event({"platform": "Bound", "task_id": "s", "progress": 8})
                check("重连后继续接收有序更新", read_sse(recovered)[1]["task"]["progress"] == 8
                      and read_sse(fast)[1]["task"]["progress"] == 8)
        finally:
            release.set()
            slow.close()
            if fast is not None:
                fast.close()
    check("溢出与正常订阅全部清理", wait_for(lambda: subscriber_count() == 0))

    original_setup = S.Handler.setup
    original_subscribe = S.subscribe
    registered = threading.Event()
    timed_sub = []

    def small_send_buffer(handler):
        original_setup(handler)
        handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

    def record_subscribe():
        sub, snap = original_subscribe()
        timed_sub.append(sub)
        registered.set()
        return sub, snap

    reset()
    S.apply_event({"platform": "Timeout", "task_id": "s", "message": "x" * (2 * 1024 * 1024)})
    with patch.object(S.Handler, "setup", small_send_buffer), \
            patch.object(S, "SSE_WRITE_TIMEOUT", 0.2), patch.object(S, "subscribe", record_subscribe):
        raw = socket.socket()
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        raw.settimeout(5)
        try:
            port = int(BASE.rsplit(":", 1)[1])
            t0 = time.monotonic()
            raw.connect((HOST, port))
            raw.sendall(b"GET /api/stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            check("不读 socket 客户端已订阅", registered.wait(3))
            cleaned = wait_for(lambda: subscriber_count() == 0, timeout=3)
            check("真实 socket 写超时释放订阅", cleaned and time.monotonic() - t0 < 3
                  and timed_sub and timed_sub[0].closed.is_set())
            check("写超时不是队列溢出触发", bool(timed_sub) and timed_sub[0].queue.empty())
        finally:
            raw.close()
    reset()

    def broken_headers(handler):
        if handler.path == "/api/stream":
            raise OSError("test disconnected during headers")
        original_headers(handler)

    with patch.object(S.Handler, "end_headers", broken_headers):
        try:
            with stream():
                check("响应头断连被关闭", False)
        except (OSError, EOFError):
            check("响应头断连被关闭", True)
    check("登记后响应头失败无订阅泄漏", wait_for(lambda: subscriber_count() == 0))

    with patch.object(S, "SSE_MAX_CONNECTIONS", 4):
        ready = threading.Barrier(13)
        accepted, held, errors = [], [], []
        guard = threading.Lock()

        def register():
            try:
                ready.wait(5)
                sub, _ = S.subscribe()
                with guard:
                    accepted.append(sub is not None)
                    if sub is not None:
                        held.append(sub)
            except Exception as e:
                errors.append(type(e).__name__)

        threads = [threading.Thread(target=register) for _ in range(12)]
        for t in threads:
            t.start()
        ready.wait(5)
        for t in threads:
            t.join(5)
        try:
            check("并发登记不超连接上限", not errors and len(accepted) == 12
                  and sum(accepted) == subscriber_count() == 4, errors)
            racers = [threading.Thread(target=S.unsubscribe, args=(sub,)) for sub in held]
            broadcaster = threading.Thread(target=lambda: [S.broadcast({"type": "test"}) for _ in range(100)])
            broadcaster.start()
            for t in racers:
                t.start()
            for t in racers + [broadcaster]:
                t.join(5)
            check("清理与广播竞争不重登记或泄漏", subscriber_count() == 0
                  and all(sub.closed.is_set() and sub.queue.qsize() <= S.SSE_QUEUE_SIZE for sub in held))
        finally:
            for sub in held:
                S.unsubscribe(sub)


def hook_regressions():
    import io
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "connectors"))
    import universal_hook as H

    cases = [
        ("empty notification", "waiting", {}, False),
        ("idle notification", "waiting", {"notification_type": "idle_prompt"}, False),
        ("question words are not evidence", "waiting", {"message": "请选择确认授权"}, False),
        ("permission", "waiting", {"notification_type": "permission_prompt"}, True),
        ("question", "waiting", {"tool_name": "AskUserQuestion"}, True),
        ("elicitation", "waiting", {"notificationType": "elicitation_dialog"}, True),
        ("post tool is not pending", "running", {"tool_name": "AskUserQuestion"}, True),
        ("completed prompt is not pending", "done", {"prompt": "请选择确认授权"}, True),
        ("non object ignored", "waiting", [], False),
    ]
    for name, status, event, sent in cases:
        with patch.object(sys, "argv", ["hook", status, "WorkBuddy"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.object(H._opener, "open") as request:
            H.main()
            check("hook " + name, request.called == sent)
            if sent:
                payload = json.loads(request.call_args.args[0].data)
                check("hook ask only while waiting: " + name,
                      payload["ask"] == (status == "waiting"))
    for field in ("session_id", "sessionId", "conversation_id", "conversationId"):
        with patch.object(sys, "argv", ["hook", "done", "WorkBuddy"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps({field: "session"}))), \
                patch.object(H._opener, "open") as request:
            H.main()
            check("hook session alias " + field,
                  json.loads(request.call_args.args[0].data)["task_id"] == "session")


def main():
    global BASE
    hook_regressions()
    httpd = ThreadingHTTPServer((HOST, PORT), S.Handler)
    BASE = "http://%s:%d" % httpd.server_address
    S.HEARTBEAT = 0.05
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    print("测试服务：%s" % BASE)
    try:
        # ---------------------------------------------------------- 1 基本
        print("\n=== 1. 单事件与快照 ===")
        reset()
        code, body = post("/api/events", {
            "platform": "千问办公", "task_id": "s1", "title": "修登录模块",
            "status": "running", "progress": 40, "src": "trace",
            "phase": "tool", "tool": "Bash"})
        check("POST 返回 200 ok", code == 200 and body.get("ok"), (code, body))
        st = get("/api/state")
        t = st["tasks"][0]
        check("phase 透传到快照", t.get("phase") == "tool", t)
        check("tool 透传到快照", t.get("tool") == "Bash", t)
        check("src 透传到快照", t.get("src") == "trace", t)
        post('/api/events', {'platform': '千问办公', 'task_id': 's1',
                            'status': 'done', 'phase': 'done', 'src': 'trace',
                            'turn': 'request-2', 'terminal': True})
        t = get('/api/state')['tasks'][0]
        check('turn 透传到快照', t.get('turn') == 'request-2', t)
        check('terminal 透传到快照', t.get('terminal') is True, t)

        # src 粘住：hook 补发一条不带 src 的更新，不该把 trace 洗掉
        post("/api/events", {"platform": "千问办公", "task_id": "s1",
                             "title": "修登录模块", "status": "running"})
        check("后续更新不带 src 时沿用原 src",
              get("/api/state")["tasks"][0].get("src") == "trace",
              get("/api/state")["tasks"][0])

        # --------------------------------------------------- 2 来源校验
        print("\n=== 2. 来源校验（P0-3）===")
        reset()
        ev = {"platform": "evil", "task_id": "x", "title": "被注入的任务",
              "status": "running"}
        code, _ = post("/api/events", ev, origin="https://evil.example.com")
        check("陌生网页的 Origin 被拒（403）", code == 403, code)
        check("被拒的事件没有进入状态", len(get("/api/state")["tasks"]) == 0,
              get("/api/state")["tasks"])

        code, _ = post("/api/events", dict(ev, platform="file"), origin="null")
        check("WebView2 的 file:// 页面（Origin: null）放行", code == 200, code)
        code, _ = post("/api/events", dict(ev, platform="local"),
                       origin="http://127.0.0.1:8765")
        check("本机帮助页 Origin 放行", code == 200, code)
        code, _ = post("/api/events", dict(ev, platform="curl"))
        check("不带 Origin 的原生客户端（curl/python/MCP）放行", code == 200, code)

        with HTTP.open(BASE + "/api/state", timeout=5) as r:
            acao = r.headers.get("Access-Control-Allow-Origin")
        check("响应不再回 Access-Control-Allow-Origin: *", acao != "*", acao)

        # ------------------------------------------------- 3 waiting 保鲜
        print("\n=== 3. waiting 不被 TTL 误删（P1）===")
        reset()
        post("/api/events", {"platform": "千问办公", "task_id": "w1",
                             "title": "要覆盖原文件吗", "status": "waiting",
                             "ask": True, "src": "trace", "phase": "asking"})
        with S.LOCK:
            S.STATE["千问办公::w1"]["ts"] = time.time() - 100
        # sync 整批重发同一个 waiting 任务
        post("/api/events", {"platform": "千问办公", "type": "sync", "src": "trace",
                             "tasks": [{"task_id": "w1", "title": "要覆盖原文件吗",
                                        "status": "waiting", "ask": True}]})
        with S.LOCK:
            ts = S.STATE["千问办公::w1"]["ts"]
        check("sync 之后 waiting 的 ts 被刷新（不会 30 分钟后被当成失联）",
              time.time() - ts < 5, time.time() - ts)

        # done 仍然要冻结 ts，靠 TTL 自然退场
        post("/api/events", {"platform": "千问办公", "task_id": "d1",
                             "title": "完成了", "status": "done", "src": "trace"})
        with S.LOCK:
            S.STATE["千问办公::d1"]["ts"] = time.time() - 100
        post("/api/events", {"platform": "千问办公", "type": "sync", "src": "trace",
                             "tasks": [{"task_id": "d1", "title": "完成了",
                                        "status": "done"}]})
        with S.LOCK:
            ts = S.STATE["千问办公::d1"]["ts"]
        check("done 的 ts 仍然冻结（TTL 能正常收走它）",
              95 < time.time() - ts < 105, time.time() - ts)

        # done/error 行只再留 TERMINAL_TTL 秒；running 不受这个短期限影响。
        post("/api/events", {"platform": "千问办公", "task_id": "r1",
                             "title": "还在跑", "status": "running", "src": "trace"})
        with S.LOCK:
            S.STATE["千问办公::r1"]["ts"] = time.time() - S.TERMINAL_TTL - 1
            S.STATE["千问办公::d1"]["ts"] = time.time() - S.TERMINAL_TTL - 1
        S.prune_once()
        with S.LOCK:
            ids = set(S.STATE)
        check("done 行超过 TERMINAL_TTL 后退场", "千问办公::d1" not in ids, ids)
        check("running 行不会被完成退场的短期限误删", "千问办公::r1" in ids, ids)

        post("/api/events", {"platform": "千问办公", "task_id": "d2",
                             "title": "完成", "status": "done", "src": "trace"})
        with S.LOCK:
            first = S.STATE["千问办公::d2"]["ts"]
        post("/api/events", {"platform": "千问办公", "task_id": "d2",
                             "title": "完成", "status": "done", "src": "trace"})
        with S.LOCK:
            again = S.STATE["千问办公::d2"]["ts"]
        check("单事件重复上报 done 也不刷新退场计时", first == again, (first, again))

        # ------------------------------------------------------- 4 并发
        print("\n=== 4. 并发 sync（P0-7）===")
        reset()
        errors = []

        def hammer(pid):
            try:
                for i in range(40):
                    post("/api/events", {
                        "platform": "P%d" % pid, "type": "sync",
                        "src": "trace" if pid % 2 else "hook",
                        "tasks": [{"task_id": "t%d" % j,
                                   "title": "任务 %d-%d" % (pid, j),
                                   "status": "running"} for j in range(5)]})
            except Exception as e:
                errors.append("%s: %s" % (type(e).__name__, e))

        # 同时让 prune_loop 的逻辑也在跑，制造持锁/裸写混跑的场景
        stop = threading.Event()

        def pruner():
            while not stop.is_set():
                S.prune_once()
                stop.wait(0.001)

        pt = threading.Thread(target=pruner, daemon=True)
        pt.start()
        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        stop.set()
        pt.join(5)
        check("8 线程 × 40 轮 sync 无异常", not errors and all(not t.is_alive() for t in threads), errors[:3])
        check("没有线程卡死（10 秒内跑完）", time.time() - t0 < 10,
              "%.1fs" % (time.time() - t0))
        st = get("/api/state")
        check("状态收敛到 8 平台 × 5 任务", len(st["tasks"]) == 40, len(st["tasks"]))

        print("\n=== 5. SSE 订阅者并发进出 ===")
        reset()
        sse_errors = []
        ready = threading.Barrier(7)
        release = threading.Event()
        received = []

        def sub():
            try:
                with stream() as r:
                    kind, initial = read_sse(r)
                    if kind != "snapshot" or initial["tasks"]:
                        raise AssertionError("initial snapshot")
                    ready.wait(5)
                    values = [read_sse(r)[1]["task"]["progress"] for _ in range(12)]
                    received.append(values)
                    release.wait(5)
            except Exception as e:
                sse_errors.append(type(e).__name__)
                ready.abort()

        subs = [threading.Thread(target=sub, daemon=True) for _ in range(6)]
        for s in subs:
            s.start()
        try:
            ready.wait(5)
            check("订阅者已登记（读完初始 SSE 且保持连接）", subscriber_count() == 6,
                  subscriber_count())
            for i in range(12):
                post("/api/events", {"platform": "SSE", "task_id": "s", "progress": i})
            complete = wait_for(lambda: len(received) == 6 or bool(sse_errors))
            check("六个客户端实际读取同序 SSE", complete and len(received) == 6
                  and all(v == list(range(12)) for v in received), received)
        finally:
            release.set()
            for s in subs:
                s.join(6)
        check("广播期间 SUBS 没有崩", not sse_errors, sse_errors)
        check("断开订阅全部清理", wait_for(lambda: subscriber_count() == 0), subscriber_count())

        # ------------------------------------------------- 6 三级来源收敛
        print("\n=== 6. trace / hook / 应用接入 三级收敛 ===")
        reset()
        post("/api/events", {"platform": "Qoder", "task_id": "h1",
                             "title": "hook 的粗粒度状态", "status": "running",
                             "src": "hook"})
        post("/api/events", {"platform": "Qoder", "task_id": "h1",
                             "title": "同会话追踪器的精确步骤", "status": "running",
                             "src": "trace", "phase": "tool"})
        post("/api/events", {"platform": "Qoder", "task_id": "h1",
                             "title": "hook 又来一条", "status": "running",
                             "src": "hook"})
        tasks = get("/api/state")["tasks"]
        check("追踪器在报时，hook 的更新让位", len(tasks) == 1, tasks)
        check("留下的是 trace 那条", tasks and tasks[0]["src"] == "trace", tasks)
        # 但 waiting 必须能穿透让位规则
        post("/api/events", {"platform": "Qoder", "task_id": "h2",
                             "title": "要你拍板", "status": "waiting",
                             "ask": True, "src": "hook"})
        tasks = get("/api/state")["tasks"]
        check("waiting 不让位（等用户的事必须显示）", len(tasks) == 2, tasks)

        reset()
        post("/api/events", {"platform": "应用接入", "task_id": "app:qoder.exe",
                             "title": "qoder.exe 在忙", "status": "running",
                             "src": "appwatch"})
        post("/api/events", {"platform": "Qoder", "task_id": "n1",
                             "title": "Qoder 自己上报", "status": "running"})
        tasks = get("/api/state")["tasks"]
        check("软件自己上报后，粗粒度「应用接入」被收掉",
              all(t["platform"] != "应用接入" for t in tasks), tasks)

        # ------------------------------------------------------ 7 容错
        print("\n=== 7. PowerShell 吞引号的容错仍然有效 ===")
        reset()
        code, body = post("/api/events", None, raw=b'{platform:"\xe8\xb1\x86\xe5\x8c\x85",'
                          b'task_id:t1,title:"\xe5\x86\x99\xe5\x91\xa8\xe6\x8a\xa5",'
                          b'status:running,progress:30}')
        check("裸 key:value 仍能解析", code == 200 and body.get("ok"), (code, body))
        tasks = get("/api/state")["tasks"]
        check("容错解析出的字段正确",
              tasks and tasks[0]["platform"] == "豆包" and tasks[0]["progress"] == 30,
              tasks)
        code, body = post("/api/events", None, raw=b"{type:sync,tasks:[{a:1}]}")
        check("带嵌套的非法 JSON 明确报错而不是瞎猜", code == 400, (code, body))

        source_regressions()
        ordering_regressions()
        resource_regressions()
    finally:
        httpd.shutdown()
        httpd.server_close()
        th.join(5)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
