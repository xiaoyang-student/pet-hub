# -*- coding: utf-8 -*-
"""输出：落盘 / 控制台 / 推送到桌宠。全部带异常保护与节流。"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import warnings
from collections import OrderedDict

from core.model import K_RESULT


def _is_terminal(e):
    # 工具级 error 不是整轮失败；只接受明确的布尔证据，不把 "false" 当 True。
    return e.kind == K_RESULT or (getattr(e, "meta", None) or {}).get("terminal") is True


def _turn_id(e):
    turn = getattr(e, "turn", None)
    return "" if turn is None else str(turn)


class _TurnBarrier:
    """只记录有身份的终态，避免把整个会话永久锁住。"""
    LIMIT = 1024
    TTL = 24 * 60 * 60.0

    def __init__(self):
        self.closed = OrderedDict()

    def accept(self, key, terminal, now):
        while self.closed:
            if now - next(iter(self.closed.values())) < self.TTL:
                break
            self.closed.popitem(last=False)
        if not key[2].strip():
            return True
        if key in self.closed:
            return False
        if terminal:
            self.closed[key] = now
            if len(self.closed) > self.LIMIT:
                self.closed.popitem(last=False)
        return True

    def forget_app(self, app):
        for key in list(self.closed):
            if key[0] == app:
                del self.closed[key]


class _TurnOrder:
    """同一会话内的轮次先后：按首次出现定序，旧轮迟到事件不再投影。

    轮次 ID 是不透明字符串（UUID / 自定义串），没有可比较的顺序，只能用
    "本进程第一次见到它"的先后。旧轮补写的终态仍然记进 _TurnBarrier，
    只是不覆盖当前轮：否则状态栏会把"上一轮的工具结果"显示成"这一轮在跑"，
    随后又会基于旧轮算停滞时间。容量与 _TurnBarrier 同义，都是尽力而为。
    """

    LIMIT = 512

    def __init__(self):
        self._seen = OrderedDict()    # (app, sid, turn) -> 序号
        self._latest = OrderedDict()  # (app, sid) -> (turn, 序号)
        self._n = 0

    def admit(self, key, turn):
        """True = 当前轮（或空轮次，无法定序）；False = 已见过的旧轮。"""
        t = "" if turn is None else str(turn)
        if not t.strip():
            return True
        latest = self._latest.get(key)
        if latest is not None and latest[0] == t:
            return True
        ident = (key[0], key[1], t)
        if ident in self._seen:
            return False
        self._n += 1
        self._seen[ident] = self._n
        self._latest[key] = (t, self._n)
        self._latest.move_to_end(key)
        self._trim()
        return True

    def _trim(self):
        while len(self._seen) > self.LIMIT:
            ident, order = self._seen.popitem(last=False)
            cur = self._latest.get(ident[:2])
            if cur is not None and cur[1] == order:
                del self._latest[ident[:2]]
        while len(self._latest) > self.LIMIT:
            self._latest.popitem(last=False)

    def forget_app(self, app):
        for ident in list(self._seen):
            if ident[0] == app:
                del self._seen[ident]
        for key in list(self._latest):
            if key[0] == app:
                del self._latest[key]


class JsonlSink:
    """追加写 JSONL，2 秒或 20 条刷一次盘。"""

    def __init__(self, path, every=2.0, flush_n=20):
        self.path = path
        self.every = every
        self.flush_n = flush_n
        self._n = 0
        self._at = time.time()
        d = os.path.dirname(path)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        self.f = None
        self._retry_at = 0.0
        self._open()

    RETRY_SEC = 30.0

    def _open(self):
        try:
            self.f = open(self.path, "a", encoding="utf-8")
        except Exception:
            self.f = None
            self._retry_at = time.time() + self.RETRY_SEC

    def push(self, e):
        if not self.f:
            # 旧实现打开失败后 self.f 永远是 None，于是永久静默——
            # 一次瞬时的权限/占用问题就能把整个日志关掉，且没有任何提示。
            # 这里改成定期重试。
            if time.time() < self._retry_at:
                return
            self._open()
            if not self.f:
                return
        try:
            self.f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            try:
                self.f.close()
            except Exception:
                pass
            self.f = None
            self._retry_at = time.time() + self.RETRY_SEC
            return
        self._n += 1
        if self._n >= self.flush_n or time.time() - self._at >= self.every:
            try:
                self.f.flush()
            except Exception:
                pass
            self._n = 0
            self._at = time.time()

    def close(self):
        try:
            if self.f:
                self.f.flush()
                self.f.close()
        except Exception:
            pass
        self.f = None


class StdoutSink:
    """一行一步，适合直接看。"""

    def __init__(self, fp=None, maxlen=140):
        self.fp = fp
        self.maxlen = maxlen

    def push(self, e):
        d = e.to_dict()
        line = "%s [%s] %-9s %s" % (d["iso"], e.app, e.kind, d["text"])
        txt = line if len(line) <= self.maxlen else line[: self.maxlen - 1] + "…"
        try:
            print(txt, file=self.fp)
        except Exception:
            pass


# 桌宠状态栏上显示的软件名（key -> 名字）
APP_NAMES = {
    "workbuddy": "WorkBuddy",
    "qoder": "Qoder",
    "zcode": "ZCode",
    "qwen_work": "千问办公",
    "doubao_work": "豆包工作",
    "doubao": "豆包",
    "baidu_dazi": "百度搭子",
    "chatgpt": "ChatGPT / Codex",
}

# 步骤类型 -> 桌宠能懂的任务状态
# 桥只认五个状态：running / thinking / waiting / done / error。
# 更细的语义走 phase 字段（状态栏上渲染成小徽标），不改协议就能扩展。
_STATUS_OF = {
    "user": "running", "plan": "running", "think": "thinking",
    "tool": "running", "tool_done": "running", "edit": "running",
    "answer": "running", "status": "running",
    "ask": "waiting",          # 需要用户拍板 —— 有真实询问事件才算
    "denied": "error",         # 用户拒绝了授权
    "stalled": "running",      # 疑似停滞：绝不报 done，只加徽标说明
    "result": "done", "error": "error",
}

# 这些状态不能被节流丢掉：丢一条 done 会让任务永远停在"运行中"，
# 丢一条 ask 会让用户错过需要他拍板的时刻。
_PRIORITY_KINDS = frozenset(("ask", "denied", "result", "error", "app_exit"))


class PetHubSink:
    """推给 PetHub 桌宠桥（默认 http://127.0.0.1:8765/api/events）。

    输出字段为 {platform, task_id, title, status, message, progress, ask, phase,
    turn, terminal}，这里把 Step 翻译成桥接 schema：
        platform = 软件中文名（状态栏第一列就是它）
        task_id  = 会话 id（Step.session），没有才退回软件 key。
                   这样同一款软件开多个会话时是状态栏上的多行，
                   而不是互相覆盖成一行。
        title    = 当前这一步在干嘛
        phase    = 细粒度阶段（received/thinking/tool/asking/denied/
                   answering/done/stalled/error），状态栏渲染成徽标
        ask      = True 时桌宠会主动开口提示"等你拍板"

    注意：不再发送 ts 字段。桥的 apply_event 从来不读入站 ts（它一律用
    服务端当前时间），发过去只是让人误以为可以指定任务时间。

    断连期间按会话合并最新状态，由引擎空闲 tick 指数退避重试。
    """

    BACKOFF_MAX = 60.0
    MAX_PENDING = 1024
    FLUSH_BATCH = 4

    def __init__(self, url="http://127.0.0.1:8765/api/events", throttle=1.2,
                 timeout=1.5, max_pending=MAX_PENDING):
        self.url = url
        self.throttle = max(0.0, throttle)
        self.timeout = timeout
        self.max_pending = max(1, int(max_pending))
        self._at = float("-inf")
        self._fail = 0
        self._next_try = 0.0
        self._held = OrderedDict()
        self._terminal_turns = _TurnBarrier()
        self._turn_order = _TurnOrder()
        self.evicted = 0
        self.critical_evicted = 0

    def push(self, e):
        name = APP_NAMES.get(e.app, e.app)
        if e.kind == "app_exit":
            self._terminal_turns.forget_app(name)
            self._turn_order.forget_app(name)
            # 退出屏障必须先于同平台重启后的状态送达。
            for key in list(self._held):
                if key[0] == name:
                    del self._held[key]
            self._queue((name, None), {
                "platform": name, "type": "sync", "tasks": [], "src": "trace",
            })
            self.tick()
            return

        text = (e.text or "").strip()
        status = _STATUS_OF.get(e.kind, "running")
        meta = getattr(e, "meta", None) or {}
        tid = (str(getattr(e, "session", "") or "") or e.app or name)[:64]
        turn, terminal = _turn_id(e), _is_terminal(e)
        if not self._turn_order.admit((name, tid), turn):
            # 旧轮迟到：终态仍记账，避免它继续被当成活跃轮重放。
            self._terminal_turns.accept((name, tid, turn), terminal, time.time())
            self.tick()
            return
        # 与 task_id 的既有队列身份一致；发送成功后也保留屏障。
        if not self._terminal_turns.accept((name, tid, turn), terminal, time.time()):
            self.tick()
            return
        phase = "cancelled" if meta.get("outcome") == "cancelled" else str(meta.get("phase") or "")
        can_ask = (not terminal and status not in ("done", "error")
                   and phase not in ("denied", "error", "cancelled"))
        if phase == "asking":
            if can_ask:
                status = "waiting"
            else:
                phase = "denied" if e.kind == "denied" else (
                    status if status in ("done", "error") else "idle")
        if not can_ask and status == "waiting":
            status = "error" if phase in ("denied", "error") else "running"
        payload = {
            "platform": name,
            "task_id": tid,
            "turn": turn,
            "terminal": terminal,
            "title": text[:80] or name,
            "status": status,
            "message": text[:200],
            "progress": None,
            "src": "trace",
        }
        if phase:
            payload["phase"] = phase[:24]
        if can_ask and (status == "waiting" or meta.get("ask")):
            payload["ask"] = True
        if meta.get("tool"):
            payload["tool"] = str(meta["tool"])[:32]
        # 先替换再发送，不能让旧的节流或失败状态回写新状态。
        self._queue((name, tid), payload)
        self.tick()

    @staticmethod
    def _priority(payload):
        return (payload.get("type") == "sync" or payload.get("ask") or
                payload.get("terminal") is True or
                payload.get("status") in ("waiting", "done", "error"))

    def _evicted(self, payload):
        self.evicted += 1
        if self._priority(payload):
            self.critical_evicted += 1
            if self.critical_evicted == 1:
                warnings.warn("PetHub pending capacity %d exceeded; critical state "
                              "evicted (see critical_evicted counter)" % self.max_pending,
                              RuntimeWarning, stacklevel=3)

    def _queue(self, key, payload):
        previous = self._held.get(key)
        # 即使历史屏障因 TTL/容量被淘汰，仍不能覆盖尚未送达的同轮终态。
        if (previous and previous.get("terminal") is True
                and str(previous.get("turn") or "").strip()
                and previous.get("turn") == payload.get("turn")):
            return
        if key not in self._held and len(self._held) >= self.max_pending:
            # 满载先淘汰最老普通状态；关键状态溢出须可观测，退出屏障最后淘汰。
            victim = next((k for k, p in self._held.items()
                           if not self._priority(p)), None)
            if victim is None and self._priority(payload):
                victim = next((k for k in self._held if k[1] is not None), None)
                if victim is None and key[1] is None:
                    victim = next(iter(self._held))
            if victim is None:
                self._evicted(payload)
                return
            self._evicted(self._held.pop(victim))
        self._held[key] = payload

    def next_wake(self, now):
        if not self._held:
            return None
        due = self._next_try
        if not self._fail and not any(self._priority(p) for p in self._held.values()):
            due = max(due, self._at + self.throttle)
        return max(0.0, due - now)

    def tick(self, now=None):
        now = time.time() if now is None else now
        if now < self._next_try or not self._held:
            return
        ready = []
        for priority in (True, False):
            if not priority and not self._fail and now < self._at + self.throttle:
                break
            for key, payload in self._held.items():
                if bool(self._priority(payload)) != priority:
                    continue
                if key[1] is not None and (key[0], None) in self._held:
                    continue
                ready.append((key, payload))
                if len(ready) >= self.FLUSH_BATCH:
                    break
            if len(ready) >= self.FLUSH_BATCH:
                break
        for key, payload in ready:
            if self._held.get(key) is not payload:
                continue
            try:
                ok = self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception:
                ok = False
            self._at = max(now, time.time())
            if not ok:
                self._fail = min(self._fail + 1, 6)
                self._next_try = self._at + min(self.BACKOFF_MAX, 2.0 ** self._fail)
                return
            if payload.get("terminal") is True:
                # 长期离线期间过期/淘汰的屏障，在终态实际送达时重新建立。
                self._terminal_turns.accept((*key, payload["turn"]), True, now)
            if self._held.get(key) is payload:
                del self._held[key]
            self._fail = 0
            self._next_try = 0.0

    def _flush_held(self, now):
        self.tick(now)

    def _send(self, data):
        try:
            req = urllib.request.Request(self.url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read(64)
            return True
        except Exception:
            return False

    def close(self):
        self.tick()
