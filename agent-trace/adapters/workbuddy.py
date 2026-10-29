# -*- coding: utf-8 -*-
"""WorkBuddy 适配器（A 级：结构化）。

三个真实数据源，同时用：

  1) ~/.workbuddy/traces/<进程号>/trace_<id>.jsonl   —— 实时 span 流
     每调用一次工具就写两条：
       {"type":"span_start","span":{"name":"Bash","toolName":"Bash",
                                    "startedAt":...}}
       {"type":"span_end",  "span":{"...","duration":19895,"status":"ok"}}
     这是它自己记录的「每一步」，实时写入，不需要任何猜测。

  2) ~/.workbuddy/projects/<工作目录>/<会话>.jsonl   —— 【辅】对话正文
     message（用户/助手）/ function_call / function_result，
     用于把「说了什么、答了什么」补进来。

  3) ~/.workbuddy/logs/<日期>/sdk/conversations/<会话>.log —— 请求终态
     event-machine:dispatch 绑定 requestId；runtime.applyStopReason 才是
     请求完成/失败证据。assistant.status 和 trace_end（包括标题生成）不是。
"""
from __future__ import annotations

from collections import OrderedDict
import glob
import heapq
import json
import os
import time

from adapters.base import Adapter, TailReader
from core.model import (K_ANSWER, K_ASK, K_ERROR, K_RESULT, K_STATUS, K_TOOL, K_TOOL_DONE,
                        K_USER, short)


def _iso_ts(s):
    """'2026-10-27T09:41:24.250Z' -> epoch 秒（按 UTC 解析）。解析不了返回 0。"""
    if not isinstance(s, str) or len(s) < 19:
        return 0.0
    try:
        import datetime
        d = s.replace("Z", "").split("+")[0]
        if "." in d:
            d, frac = d.split(".", 1)
            frac = (frac + "000000")[:6]
        else:
            frac = "000000"
        dt = datetime.datetime.strptime(d, "%Y-%m-%dT%H:%M:%S")
        # 原始串是 UTC，必须按 UTC 折算，否则显示时间会差 8 小时
        return dt.replace(microsecond=int(frac),
                          tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return 0.0


def _text_of(o):
    c = o.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c[:12]:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text") or b.get("content") or b.get("output") or ""
                if isinstance(t, list):
                    t = " ".join(str(x) for x in t[:6])
                parts.append(str(t))
        return " ".join(p for p in parts if p and p != "None")
    if isinstance(c, dict):
        return str(c.get("text") or c.get("content") or "")
    return ""


def _args_of(o):
    a = o.get("arguments")
    if a is None:
        return ""
    if isinstance(a, str):
        try:
            a = json.loads(a)       # arguments 常是 JSON 串，解析后再挑字段
        except Exception:
            return short(a, 120)
    if isinstance(a, dict):
        for k in ("command", "filePath", "path", "pattern", "query", "url", "code"):
            if k in a and isinstance(a[k], str):
                return a[k]
        try:
            return short(json.dumps(a, ensure_ascii=False), 200)
        except Exception:
            return ""
    return ""


def _input_brief(s, n=90):
    """toolInput 是 JSON 串，挑出最能说明在干啥的那个字段。"""
    if not isinstance(s, str) or not s.strip():
        return ""
    try:
        o = json.loads(s)
    except Exception:
        return short(s, n)
    if isinstance(o, str):                  # 双重编码：JSON 里面还是 JSON
        return _input_brief(o, n)
    if isinstance(o, dict):
        v = o.get("command")
        if isinstance(v, str) and v:
            return short(v, n)          # 命令原样截断，不做路径处理
        for k in ("file_path", "filePath", "notebook_path", "path"):
            v = o.get(k)
            if isinstance(v, str) and v:
                # 路径只留最后两段，太长占屏
                parts = v.replace("\\", "/").rstrip("/").split("/")
                return short("/".join(parts[-2:]), n)
        for k in ("pattern", "query", "url", "description"):
            v = o.get(k)
            if isinstance(v, str) and v:
                return short(v, n)
        return short(json.dumps(o, ensure_ascii=False), n)
    return short(str(o), n)


# 内部 span，不是真的“一步”，不往外报
SKIP_SPANS = {"mcp_tools", "generation", "select_tool", "list_tools",
              "todo_write", "conversation_search",
              # 进程级根 span：整个 CLI 会话本身（动辄几分钟），不是"一步"
              "cli", "agent", "session", "run", "turn", "main"}


def _is_sid(name):
    """会话日志名（去掉 .log）是不是一个 UUID。"""
    return (len(name) == 36 and name[8] == "-" and name[13] == "-"
            and name[18] == "-" and name[23] == "-")


class WorkBuddyAdapter(Adapter):
    key = "workbuddy"
    zh = "WorkBuddy"
    en = "WorkBuddy"
    tier = "A"
    T_ACTIVE, T_QUIET, T_AWAY = 0.4, 1.5, 15.0
    SDK_SOURCES = 8
    SDK_TAIL_BYTES = 128 << 10
    SDK_REQUESTS = 128
    SDK_CLOSED = 256

    def __init__(self, info, log=None):
        Adapter.__init__(self, info, log)
        self.traces_dir = ""
        self.projects_dir = ""
        self.tasks_dir = ""
        self.logs_dir = ""
        self.trace = None      # 主：span 流
        self.chat = None       # 辅：对话正文
        self._switch_at = 0
        self._open_span = {}
        self._sdk_readers = {}
        self._sdk_scan_ts = time.time()
        self._requests = OrderedDict()
        self._closed_requests = OrderedDict()
        # 状态栏的行身份 = 对话会话，不是 trace。span 流没有会话字段，
        # 只能跟随"最近一次有时间的对话事件"，否则同一轮会拆成两行。
        self._row = ""
        self._row_ts = 0.0
        self._chat_sid = ""
        self._sdk_sid = ""

    def setup(self):
        home = os.path.expanduser("~")
        wb = os.path.join(home, ".workbuddy")
        if not os.path.isdir(wb):
            for r in self.roots:
                if os.path.isdir(r):
                    wb = r
                    break
        if not os.path.isdir(wb):
            return False
        self.traces_dir = os.path.join(wb, "traces")
        self.projects_dir = os.path.join(wb, "projects")
        self.tasks_dir = os.path.join(wb, "tasks")
        self.logs_dir = os.path.join(wb, "logs")
        self._pick_sdk(first=True)
        self._pick_trace(first=True)
        self._pick_chat(first=True)
        # 空 logs 目录也要继续扫描：GUI 可能还没创建第一个会话文件。
        self.enabled = bool(self.trace or self.chat or os.path.isdir(self.logs_dir))
        if self.enabled:
            self.source = ",".join([x for x in
                                    (os.path.basename(self.trace.path) if self.trace else "",
                                     os.path.basename(self.chat.path) if self.chat else "",
                                     "sdk/conversations" if self._sdk_readers else "")
                                    if x])
            self.log("[workbuddy] span=%s 对话=%s sdk=%d" % (
                os.path.basename(self.trace.path) if self.trace else "-",
                os.path.basename(self.chat.path) if self.chat else "-",
                len(self._sdk_readers)))
        return self.enabled

    def _pick_trace(self, first=False):
        if not os.path.isdir(self.traces_dir):
            return
        fs = [p for p in glob.glob(os.path.join(self.traces_dir, "*", "trace_*.jsonl"))]
        fs += [p for p in glob.glob(os.path.join(self.traces_dir, "trace_*.jsonl"))]
        if not fs:
            return
        best = self._newest_path(fs)
        if not best or (self.trace and self.trace.path == best):
            return
        self.trace = self._replace_reader(self.trace, best, first)
        self._open_span = {}
        if not first:
            self.log("[workbuddy] 切换 trace: %s" % os.path.basename(best))

    def _pick_chat(self, first=False):
        if not os.path.isdir(self.projects_dir):
            return
        fs = []
        for depth in range(3):
            fs.extend(glob.glob(os.path.join(self.projects_dir, *(["*"] * depth), "*.jsonl")))
        best = self._newest_path(fs)
        if not best or (self.chat and self.chat.path == best):
            return
        self.chat = self._replace_reader(self.chat, best, first)
        self.title = os.path.basename(best).replace(".jsonl", "")[:8]
        self._chat_sid = os.path.basename(best).replace(".jsonl", "")

    def _note_row(self, sid, ts):
        if sid and ts >= self._row_ts:
            self._row, self._row_ts = sid, ts

    def _row_of(self):
        return self._row or self.cur_session or self._chat_sid or self._sdk_sid or ""

    @staticmethod
    def _newest_path(paths):
        found = []
        for path in paths:
            try:
                found.append((os.path.getmtime(path), path))
            except OSError:
                continue
        return max(found)[1] if found else ""

    @staticmethod
    def _replace_reader(old, path, first):
        # 新发现的文件在扫描前已经写入，不能按冷启动跳到 EOF。
        reader = TailReader(path, start_at_end=first)
        if old:
            close = getattr(old, "close", None)
            if close:
                close()
        return reader

    # ---------------------------------------------------------- SDK 请求终态
    def _pick_sdk(self, first=False, now=None):
        now = time.time() if now is None else now
        floor = now if first else self._sdk_scan_ts
        if first:
            self._sdk_readers.clear()
            self._requests.clear()
            self._closed_requests.clear()
        # 只扫描最近两个日期的固定目录；不遍历 transport/auth 等日志。
        try:
            with os.scandir(self.logs_dir) as entries:
                dates = heapq.nlargest(2, (e.name for e in entries
                    if len(e.name) == 10 and e.name[4] == e.name[7] == "-"
                    and e.name.replace("-", "").isdigit() and e.is_dir()))
        except OSError:
            dates = []

        def candidates():
            for date in dates:
                directory = os.path.join(self.logs_dir, date, "sdk", "conversations")
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if entry.name.endswith(".log"):
                                try:
                                    yield entry.stat().st_mtime, entry.path
                                except OSError:
                                    continue
                except OSError:
                    continue

        paths = [p for _, p in heapq.nlargest(self.SDK_SOURCES, candidates())]
        for path in paths:
            stem = os.path.basename(path)[:-4]
            if _is_sid(stem):
                self._sdk_sid = stem
                break
        readers = {}
        for path in paths:
            if path in self._sdk_readers:
                readers[path] = self._sdk_readers[path]
            else:
                reader = TailReader(path, max_step=64 << 10, max_line=16 << 10)
                # 有界历史仅用于恢复 request 关联；floor 禁止重放旧终态。
                # 新文件在扫描前的初始写入也在尾部，不能直接跳到 EOF。
                reader.rewind(self.SDK_TAIL_BYTES)
                readers[path] = (reader, floor)
        self._sdk_readers = readers
        self._sdk_scan_ts = now

    @staticmethod
    def _sdk_record(line):
        stamp, _, rest = line.partition(" ")
        label, _, payload = rest.partition(" ")
        if label not in ("state-machine:transition", "event-machine:dispatch",
                         "runtime.applyStopReason", "ask-user-question:pending-created",
                         "ask-user-question:respond"):
            return None
        ts = _iso_ts(stamp)
        if not ts:
            return None
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return ts, label, data

    def _close_request(self, key, kind):
        self._closed_requests[key] = kind
        self._closed_requests.move_to_end(key)
        while len(self._closed_requests) > self.SDK_CLOSED:
            self._closed_requests.popitem(last=False)

    def _sdk_event(self, record, floor):
        ts, label, data = record
        sid, iid = data.get("conversationId"), data.get("instanceId")
        if not all(isinstance(v, str) and 0 < len(v) <= 200 for v in (sid, iid)):
            return []
        self._note_row(sid, ts)
        key = (sid, iid)
        state = self._requests.get(key)
        started = False
        if state and ts < state["seen"]:
            return []  # 轮换/另一日期文件中迟到的旧记录不能命中新请求。
        prompt = (label == "state-machine:transition"
                  and data.get("input") == "PROMPT_SENT" and data.get("valid") is True)
        if prompt:
            if state and ts <= state["prompt"]:
                return []
            if state and state["request"] and not state["terminal"]:
                self._close_request((sid, state["request"]), None)  # 已被新请求替代
            state = {"prompt": ts, "seen": ts, "request": "", "terminal": False}
        elif label == "event-machine:dispatch":
            rid = data.get("requestId")
            if not isinstance(rid, str) or not 0 < len(rid) <= 200:
                return []
            if (sid, rid) in self._closed_requests:
                return []
            if state is None:
                # 启动尾部可能已没有 PROMPT_SENT，但 dispatch 自带请求关联。
                state = {"prompt": 0, "seen": ts, "request": "", "terminal": False}
            if state["terminal"] or state["request"] not in ("", rid):
                return []
            started = not state["request"] and ts >= floor
            state["request"] = rid
            call = data.get("toolCallId")
            if data.get("input") == "tool_call" and isinstance(call, str) and 0 < len(call) <= 200:
                calls = state.setdefault("calls", OrderedDict())
                calls[call] = ts
                while len(calls) > 64:
                    calls.popitem(last=False)
        elif label in ("ask-user-question:pending-created", "ask-user-question:respond"):
            call = data.get("toolCallId")
            if (not state or state["terminal"] or not state["request"]
                    or not isinstance(call, str) or call not in state.get("calls", {})
                    or data.get("requestId", state["request"]) != state["request"]):
                return []
            pending = state.setdefault("pending", OrderedDict())
            if label == "ask-user-question:pending-created":
                if ts < floor or data.get("toolName") != "AskUserQuestion" or call in pending:
                    return []
                pending[call] = ts
                while len(pending) > 16:
                    pending.popitem(last=False)
                state["asked_at"] = 0
            else:
                if call not in pending:
                    return []
                pending.pop(call)
                state["calls"].pop(call, None)
                state["seen"] = ts
                if not pending and ts >= floor:
                    self.cur_session, self.cur_turn = sid, state["request"]
                    self.phase = "thinking"
                    return [self.emit(K_STATUS, "已收到选择，继续处理", ts, sid,
                                      state["request"], phase="thinking",
                                      evidence="sdk.ask-user-question:respond")]
        elif label == "runtime.applyStopReason":
            if not state or not state["request"] or state["terminal"]:
                return []
            rid = state["request"]
            # 当前已验证的 stop 记录没有 requestId；若将来出现则也必须匹配。
            if data.get("requestId", rid) != rid:
                return []
            failed = data.get("hasFatalError") is True or data.get("hasTurnError") is True
            completed = (data.get("event") == "TURN_COMPLETED"
                         and data.get("stopReason") == "end_turn"
                         and data.get("requestState") == "completed"
                         and data.get("hasFatalError") is False
                         and data.get("hasTurnError") is False)
            if not (failed or completed):
                return []
            state["terminal"], state["seen"] = True, ts
            state.pop("pending", None)
            state.pop("calls", None)
            request_key = (sid, rid)
            if request_key in self._closed_requests:
                return []
            kind = K_ERROR if failed else K_RESULT
            self._close_request(request_key, kind)
            if ts < floor or (self.cur_session == sid and self.cur_turn != rid
                              and self.last_event > ts):
                return []  # chat 已看到下一请求时，不能把积压的旧终态用到它上面。
            self.cur_session, self.cur_turn = sid, rid
            return [self.emit(kind, "请求失败" if failed else "完成", ts, sid, rid,
                              terminal=True, evidence="sdk.runtime.applyStopReason", instance_id=iid,
                              stop_reason=data.get("stopReason"),
                              request_state=data.get("requestState"))]
        else:
            # TURN_COMPLETED transition / persisted completed 是同一 stop 的后续
            # 投影，没有 requestId；不能作为新请求的独立完成信号。
            return []
        state["seen"] = ts
        self._requests[key] = state
        self._requests.move_to_end(key)
        while len(self._requests) > self.SDK_REQUESTS:
            self._requests.popitem(last=False)
        if (started and (self.cur_session, self.cur_turn) != (sid, state["request"])
                and not (self.cur_session == sid and self.last_event > ts)):
            self.cur_session, self.cur_turn = sid, state["request"]
            self.phase = "thinking"
            return [self.emit(K_STATUS, "请求处理中", ts, sid, state["request"],
                              evidence="sdk.event-machine:dispatch")]
        return []

    def _poll_sdk(self):
        records = []
        for reader, floor in self._sdk_readers.values():
            for line in reader.read_lines():
                record = self._sdk_record(line)
                if record:
                    records.append((record, floor))
        # 跨午夜时两个文件可能含有同一个 instance；按 ISO 时间合并。
        records.sort(key=lambda item: item[0][0])
        out = []
        for record, floor in records:
            out.extend(self._sdk_event(record, floor))
        return out

    def tick(self, now):
        out = []
        if now >= self._switch_at:
            self._switch_at = now + 3.0
            try:
                self._pick_trace()
                self._pick_chat()
                self._pick_sdk(now=now)
            except OSError:
                pass
        if self.trace:
            for line in self.trace.read_lines():
                out.extend(self._span(line, now))
        if self.chat:
            for line in self._take(self.chat.read_lines()):
                out.extend(self._msg(line, now))
        out.extend(self._poll_sdk())
        for (sid, _), state in self._requests.items():
            if state["terminal"] or not state.get("pending"):
                continue
            rid = state["request"]
            if (now - state.get("asked_at", 0) < 5
                    and not any(s.session == sid and s.turn == rid for s in out)):
                continue
            state["asked_at"] = now
            self.cur_session, self.cur_turn = sid, rid
            out.append(self.emit(K_ASK, "等待你选择，请在 WorkBuddy 中回答", now,
                                 sid, rid, tool="AskUserQuestion", phase="asking",
                                 evidence="sdk.ask-user-question:pending-created"))
        return self._take(out)

    def replay(self, n=25):
        """自检用：回退游标，重放最近的真实步骤。"""
        Adapter.replay(self, n)
        for t in (self.trace, self.chat):
            if t:
                try:
                    t.rewind(80000)
                except Exception:
                    pass

    # ---------------------------------------------------------- span 流
    def _span(self, line, now):
        try:
            o = json.loads(line)
        except Exception:
            return []
        if o.get("type") not in ("span_start", "span_end"):
            return []
        sp = o.get("span") or {}
        name = str(sp.get("toolName") or sp.get("name") or "step")
        # 提问 span 没有会话身份；等待与恢复以 SDK 的请求关联为准。
        if name == "AskUserQuestion":
            return []
        sid = str(sp.get("spanId") or "")
        styp = str(sp.get("type") or "function")
        if styp != "function" and name in SKIP_SPANS:
            return []          # 内部动作（列工具、组 prompt）不算一步
        if o["type"] == "span_start":
            if sid in self._open_span:
                return []      # 同一个 span 只报一次“开始”，避免重复
            inp = _input_brief(sp.get("toolInput"))
            txt = name + ((" | " + inp) if inp else "")
            ts = _iso_ts(sp.get("startedAt")) or now
            self._open_span[sid] = ts
            if len(self._open_span) > 400:      # 有界，防止长时间运行无限增长
                for k in list(self._open_span)[:200]:
                    self._open_span.pop(k, None)
            s = self.emit(K_TOOL if styp == "function" else K_ANSWER, txt, ts,
                          self._row_of(), self._span_turn(sid), span=sid, tool=name)
            self.phase = "tool"
            return [s]
        # span_end
        dur = sp.get("duration")
        inp = _input_brief(sp.get("toolInput"))
        err = sp.get("error")
        status = str(sp.get("status") or "")
        tail = ""
        if isinstance(dur, (int, float)) and dur > 0:
            tail = " (%ss)" % (dur / 1000.0)
        ts = _iso_ts(sp.get("endedAt")) or now
        if status == "error" or (isinstance(err, str) and err):
            txt = name + " 失败" + (": " + short(str(err), 120) if err else "")
            s = self.emit(K_ERROR, txt, ts, self._row_of(), self._span_turn(sid),
                          span=sid, tool=name)
            self.phase = "error"
            return [s]
        txt = name + ((" | " + inp) if inp else "") + tail
        s = self.emit(K_TOOL_DONE if styp == "function" else K_ANSWER, txt, ts,
                      self._row_of(), self._span_turn(sid), span=sid, tool=name,
                      ms=int(dur) if isinstance(dur, (int, float)) else 0)
        self.phase = "tool"
        return [s]

    def _span_turn(self, span_id):
        """同一请求里的所有 span 共用请求 id，旧请求的迟到 span 才能被挡住。"""
        row = self._row_of()
        if row and row == self.cur_session and self.cur_turn:
            return self.cur_turn
        return span_id

    # ---------------------------------------------------------- 对话正文
    def _msg(self, line, now):
        try:
            o = json.loads(line)
        except Exception:
            return []
        if not isinstance(o, dict):
            return []
        t = str(o.get("type") or "")
        ts = o.get("timestamp")
        ts = (ts / 1000.0) if isinstance(ts, (int, float)) and ts > 1e11 else now
        sid = str(o.get("sessionId") or "")
        if sid:
            self._note_row(sid, ts)
        prov = o.get("providerData") or {}
        seq, rid = 0, ""
        if isinstance(prov, dict):
            rid = str(prov.get("conversationRequestId") or "")[:200]
            try:
                seq = int(prov.get("stepSeq") or 0)
            except Exception:
                seq = 0
        if rid and (sid, rid) in self._closed_requests:
            return []  # 延迟归档/重复消息不能把已结束或已替代的请求重新变为 answering。
        if not rid and o.get('role') != 'user':
            states = [state for (session, _), state in self._requests.items() if session == sid]
            if states and all(state['terminal'] for state in states):
                return []
        if rid and t in ("message", "function_call", "function_result", "turn_end"):
            self.cur_session, self.cur_turn = sid, rid
        if t == "message":
            role = str(o.get("role") or "")
            txt = _text_of(o)
            if not txt:
                return []
            if role == "user":
                self.phase = "thinking"
                self.steps = 0
                return [self.emit(K_USER, txt, ts, sid, rid, seq=seq)]
            if role == "assistant":
                self.phase = "answering"
                return [self.emit(K_ANSWER, txt, ts, sid, rid, seq=seq)]
        if t == "function_call":
            name = str(o.get("name") or "tool")
            args = _args_of(o)
            self.phase = "tool"
            return [self.emit(K_TOOL, name + ((" | " + args) if args else ""), ts,
                              sid, rid, seq=seq, tool=name, args=short(args, 120))]
        if t == "function_result":
            txt = _text_of(o) or str(o.get("message") or "")
            if txt:
                self.phase = "tool"
                return [self.emit(K_TOOL_DONE, txt, ts, sid, rid, seq=seq)]
        if t == "turn_end":
            if rid:
                self._close_request((sid, rid), K_RESULT)
            self.phase = "done"
            return [self.emit(K_RESULT, _text_of(o) or "完成", ts, sid, rid, seq=seq)]
        return []

    def status(self, now=None):
        st = Adapter.status(self, now)
        st.title = short(self.title or self.source, 40)
        return st
