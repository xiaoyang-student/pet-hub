# -*- coding: utf-8 -*-
"""Qoder 适配器（A 级：结构化）。

真实数据源（都是 Qoder 自己写的，不是猜的）：
  1) %APPDATA%/com.qoder.app.stable/chat-session-turn-payload-buffer.sqlite
     表 active_turn_projection_items —— 当前回合的实时步骤缓冲，
     按 ordinal 递增，item_json 是原始消息体（文本 / 工具调用 / 工具返回）。
  2) 同目录 main.sqlite
     表 chat_session_messages（历史消息）、turn_file_change_files（改了哪些文件、
     增删多少行）、chat_sessions（会话标题）。
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict

from adapters.base import Adapter, ReadOnlyDB, newest
from core.model import (K_ANSWER, K_ASK, K_EDIT, K_ERROR, K_RESULT, K_THINK, K_TOOL,
                        K_TOOL_DONE, K_USER, K_STATUS, P_ASKING, P_THINKING, short)


def _content_to_text(c):
    """Claude 风格 content 数组 -> 文本。"""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c[:10]:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t)
        return " ".join(parts)
    return ""


def _tool_of(b):
    name = b.get("name") or "tool"
    inp = b.get("input")
    s = ""
    if isinstance(inp, dict):
        for k in ("command", "file_path", "path", "pattern", "query", "url", "description"):
            if k in inp and isinstance(inp[k], str):
                s = inp[k]
                break
        if not s:
            try:
                s = json.dumps(inp, ensure_ascii=False)[:160]
            except Exception:
                s = ""
    elif isinstance(inp, str):
        s = inp
    return name, s


class QoderAdapter(Adapter):
    key = "qoder"
    zh = "Qoder"
    en = "Qoder"
    tier = "A"
    QUESTION_TEXT = "请在 Qoder 中选择或回答"
    QUESTION_LIMIT = 16
    QUESTION_CALLS = 16
    # 缓冲表的主键是 (session_id,turn_id,item_id)，不同会话/回合完全可能
    # 共用同一个 (created_at,ordinal)。只用单游标比较会永久漏掉同键的另一行，
    # 所以改成"有界重叠窗口 + 行身份去重"：窗口内重读、按身份只消费一次。
    OVERLAP_MS = 2000
    ROW_LIMIT = 300
    SEEN_LIMIT = 4096

    def __init__(self, info, log=None):
        Adapter.__init__(self, info, log)
        self.buf = None       # 实时回合缓冲
        self.main = None      # 主库
        self.cur = (0, 0)     # (created_at, ordinal) 已读高水位
        self.mcur = (0, 0)    # main.sqlite 游标 (created_at, sequence)
        self._seen_rows = OrderedDict()   # 已消费的缓冲行身份
        # 注意：sequence 是按会话重置的，不能当全局游标，必须用 (created_at,sequence)
        self.session = ""
        self.turn = ""
        self._file_turn = ""
        self._buf_identity = None
        self._buf_anchor = None
        self._watching = OrderedDict()
        self._closed = OrderedDict()
        self._terminal_at = 0.0
        self._questions = OrderedDict()
        self._question_history = OrderedDict()
        self._user_turns = OrderedDict()

    def emit(self, kind, text, now, session="", turn="", seq=0, **meta):
        if (session, turn) in self._questions and not meta.get("terminal"):
            meta["phase"] = P_ASKING
        step = Adapter.emit(self, kind, text, now, session, turn, seq, **meta)
        self.phase = step.meta["phase"]
        return step

    @staticmethod
    def _question_identity(sid, tid, call):
        return all(isinstance(v, str) and 0 < len(v) <= 256 for v in (sid, tid, call))

    def _remember_question(self, key, call=None, native=True):
        # None retires a whole turn; call tombstones survive buffer/archive duplicates.
        if call is None:
            self._question_history[key] = None
        else:
            calls = self._question_history.setdefault(key, OrderedDict())
            if calls is not None:
                calls[call] = native or calls.get(call, False)
                while len(calls) > 256:
                    calls.popitem(last=False)
        self._question_history.move_to_end(key)
        while len(self._question_history) > 256:
            self._question_history.popitem(last=False)

    def _drop_questions(self, key):
        self._questions.pop(key, None)
        self._remember_question(key)

    def _new_user_turn(self, sid, tid, ts):
        if not sid or not tid:
            return
        previous = self._user_turns.get(sid)
        if previous and (previous[0] == tid or ts < previous[1]):
            return
        self._user_turns[sid] = (tid, ts)
        self._user_turns.move_to_end(sid)
        while len(self._user_turns) > 128:
            self._user_turns.popitem(last=False)
        for key in list(self._watching):
            if key[0] == sid and key[1] != tid:
                calls = self._questions.get(key, {})
                if not calls or max(c["ts"] for c in calls.values()) <= ts:
                    self._drop_questions(key)

    def _start_question(self, sid, tid, call, seq, ts):
        if not self._question_identity(sid, tid, call):
            return []
        key = (sid, tid)
        history = self._question_history.get(key, {})
        user = self._user_turns.get(sid)
        if (key in self._closed or history is None or call in history
                or (user and user[0] != tid and ts <= user[1])):
            return []
        self._watch(sid, tid)
        calls = self._questions.setdefault(key, OrderedDict())
        if call in calls:
            return []
        calls[call] = dict(ts=ts, seq=seq, refresh=ts + 5.0)
        while len(calls) > self.QUESTION_CALLS:
            old, _ = calls.popitem(last=False)
            self._remember_question(key, old)
        while len(self._questions) > self.QUESTION_LIMIT:
            self._drop_questions(next(iter(self._questions)))
        return [self.emit(K_ASK, self.QUESTION_TEXT, ts, sid, tid, seq,
                          tool="AskUserQuestion", callId=call)]

    def _resolve_question(self, sid, tid, call, seq, ts, native=False):
        if not self._question_identity(sid, tid, call):
            return []
        key = (sid, tid)
        calls = self._questions.get(key, {})
        pending = calls.get(call)
        if pending and ts < pending["ts"]:
            return []
        self._remember_question(key, call, native=native or pending is not None)
        if pending is None:
            return []
        del calls[call]
        if not calls:
            del self._questions[key]
        return [self.emit(K_STATUS, "选择已结束，继续思考", ts, sid, tid, seq,
                          phase=P_THINKING, terminal=False,
                          tool="AskUserQuestion", callId=call)]

    def _finish_questions(self, out, now):
        asks, activity = [], []
        for step in out:
            if step.kind != K_ASK:
                activity.append(step)
                continue
            calls = self._questions.get((step.session, step.turn), {})
            if step.meta.get("callId") in calls:
                calls[step.meta["callId"]]["refresh"] = now + 5.0
                asks.append(step)
        for (sid, tid), calls in self._questions.items():
            for call, pending in calls.items():
                if now >= pending["refresh"]:
                    pending["refresh"] = now + 5.0
                    asks.append(self.emit(K_ASK, self.QUESTION_TEXT, now, sid, tid,
                                          pending["seq"], tool="AskUserQuestion", callId=call))
        if self._questions:
            self.phase = P_ASKING
        return activity + asks

    def setup(self):
        # 数据根目录：com.qoder.app.stable 优先，其次 .qoder
        roots = [r for r in self.roots if os.path.isdir(r)]
        if not roots:
            return False
        order = sorted(roots, key=lambda p: ("com.qoder.app.stable" not in p))
        buf = ""
        for r in order:
            c = os.path.join(r, "chat-session-turn-payload-buffer.sqlite")
            if os.path.exists(c):
                buf = c
                break
        if not buf:
            hits = [p for p in [newest(order, exts=(".sqlite",))] if p and
                    "turn-payload" in os.path.basename(p).lower()]
            buf = hits[0] if hits else ""
        self.buf = ReadOnlyDB(buf)
        mainp = ""
        for r in order:
            c = os.path.join(r, "main.sqlite")
            if os.path.exists(c):
                mainp = c
                break
        self.main = ReadOnlyDB(mainp)
        self.enabled = bool(buf or mainp)
        if self.enabled:
            self.source = os.path.basename(buf or mainp)
            # 启动时把游标放到当前最新位置，只跟踪之后发生的事
            self._seed()
            self.log("[qoder] 缓冲=%s 主库=%s" % (os.path.basename(buf) if buf else "-",
                                                  os.path.basename(mainp) if mainp else "-"))
        return self.enabled

    def _seed(self):
        if self.buf and self.buf.path:
            self._check_buffer_file()
            rows = self.buf.q("select created_at,ordinal,session_id,turn_id "
                              "from active_turn_projection_items "
                              "order by created_at desc,ordinal desc limit 1")
            if rows:
                try:
                    self.cur = (int(rows[0][0]), int(rows[0][1]))
                except Exception:
                    return
                self._buf_anchor = tuple(rows[0][2:4])
                self._watch(*self._buf_anchor)
            self._prime_seen()
        if self.main and self.main.path:
            rows = self.main.q("select created_at,sequence from chat_session_messages "
                               "order by created_at desc,sequence desc limit 1")
            if rows:
                try:
                    self.mcur = (int(rows[0][0]), int(rows[0][1]))
                except Exception:
                    pass

    def tick(self, now):
        out = []
        if self.buf and self.buf.path:
            out.extend(self._tick_buffer(now))
        if self.main and self.main.path:
            out.extend(self._tick_main(now))
            out.extend(self._poll_terminal(now))
        return self._take(self._finish_questions(out, now))

    def _watch(self, sid, tid):
        key = (sid, tid)
        if sid and tid and key not in self._closed:
            self._watching.setdefault(key, time.monotonic())
            while len(self._watching) > 128:
                old, _ = self._watching.popitem(last=False)
                self._drop_questions(old)

    def _poll_questions(self, sid, tid, now):
        key = (sid, tid)
        calls = self._questions.get(key)
        if not calls:
            return []
        latest = self.main.q(
            "select turn_id,created_at from (select turn_id,created_at,payload_json "
            "from chat_session_messages where session_id=? and role='user' "
            "order by sequence desc limit 1) u where json_valid(payload_json) "
            "and json_extract(payload_json,'$.backgroundTaskId') is null "
            "and json_extract(payload_json,'$.responseKind') is null "
            "and exists (select 1 from json_each(u.payload_json,'$.parts') p "
            "where p.type='object' and json_extract(p.value,'$.type')='text' "
            "and json_extract(p.value,'$.text')<>'') "
            "and not exists (select 1 from json_each(u.payload_json,'$.parts') p "
            "where p.type='object' and json_extract(p.value,'$.type')='tool_result')", (sid,))
        if latest and latest[0][0] != tid:
            stamp = latest[0][1]
            if isinstance(stamp, (int, float)) and stamp > 1e11:
                self._new_user_turn(sid, latest[0][0], stamp / 1000.0)
                if key not in self._questions:
                    return []
        ids = tuple(calls)
        # Expand parts only in the latest indexed assistant row, returning bounded scalars.
        rows = self.main.q(
            "select substr(json_extract(p.value,'$.tool.id'),1,256),"
            "substr(json_extract(p.value,'$.tool.status'),1,16),m.sequence "
            "from (select payload_json,sequence from chat_session_messages "
            "where session_id=? and turn_id=? and role='assistant' "
            "and json_valid(payload_json) "
            "and json_extract(payload_json,'$.backgroundTaskId') is null "
            "and json_extract(payload_json,'$.responseKind') is null "
            "order by sequence desc limit 1) m, json_each(m.payload_json,'$.parts') p "
            "where p.type='object' and json_extract(p.value,'$.type')='tool' "
            "and json_extract(p.value,'$.tool.name')='AskUserQuestion' "
            "and json_extract(p.value,'$.tool.parentToolUseId') is null "
            "and json_extract(p.value,'$.tool.id') in (" + ",".join("?" for _ in ids) + ") "
            "and json_extract(p.value,'$.tool.status') in ('completed','failed','canceled','interrupted') "
            "limit 16", (sid, tid) + ids)
        out = []
        for call, status, seq in rows:
            out.extend(self._resolve_question(sid, tid, call, int(seq or 0), now, native=True))
        return out

    def _poll_terminal(self, now):
        if now < self._terminal_at:
            return []
        self._terminal_at = now + 1.0
        out = []
        keys = list(dict.fromkeys(list(self._questions) + list(self._watching)))[:32]
        for sid, tid in keys:
            key = (sid, tid)
            if time.monotonic() - self._watching[key] > 86400:
                del self._watching[key]
                self._drop_questions(key)
                continue
            self._watching.move_to_end(key)
            # The indexed turn lookup catches in-place completion without rereading large parts arrays.
            rows = self.main.q(
                "select status,sequence,updated_at,"
                "json_extract(payload_json,'$.resultUuid'),"
                "json_extract(payload_json,'$.completedAt') "
                "from chat_session_messages where session_id=? and turn_id=? "
                "and role='assistant' and json_valid(payload_json) "
                "and json_extract(payload_json,'$.backgroundTaskId') is null "
                "and json_extract(payload_json,'$.responseKind') is null "
                "order by sequence desc limit 1", (sid, tid))
            if not rows:
                out.extend(self._poll_questions(sid, tid, now))
                continue
            status, seq, updated, result, completed = rows[0]
            if (not completed or status not in ('completed', 'failed', 'canceled', 'interrupted')
                    or (status == 'completed' and not result)):
                out.extend(self._poll_questions(sid, tid, now))
                continue
            self._closed[key] = status
            self._drop_questions(key)
            del self._watching[key]
            while len(self._closed) > 256:
                self._closed.popitem(last=False)
            latest = self.main.q(
                "select turn_id,sequence from chat_session_messages "
                "where session_id=? and role='user' order by sequence desc limit 1", (sid,))
            if latest and latest[0][0] != tid and int(latest[0][1]) > int(seq):
                continue
            kind = K_RESULT if status == 'completed' else K_ERROR
            text = '本轮已完成' if status == 'completed' else '本轮执行失败'
            evidence = 'resultUuid+completedAt' if result else 'status+completedAt'
            meta = dict(terminal=True, evidence='chat_session_messages.' + evidence)
            if status in ('canceled', 'interrupted'):
                kind, text = K_STATUS, '本轮已取消' if status == 'canceled' else '本轮已中断'
                meta.update(phase='cancelled', outcome='cancelled')
            out.append(self.emit(kind, text, now, sid, tid, int(seq or 0), **meta))
        return out

    def replay(self, n=25):
        """自检用：回退游标，重放最近的真实回合内容。"""
        Adapter.replay(self, n)
        self.cur = (max(0, self.cur[0] - 10 * 60 * 1000), 0)
        self.mcur = (max(0, self.mcur[0] - 10 * 60 * 1000), 0)
        self._seen_rows.clear()
        self._file_turn = ""

    # ----------------------------------------------------------
    def _check_buffer_file(self):
        try:
            st = os.stat(self.buf.path)
        except OSError:
            return
        identity = (st.st_dev, st.st_ino, st.st_ctime_ns if os.name == "nt" else 0)
        if self._buf_identity is not None and identity != self._buf_identity:
            self.buf.close()
            self.cur = (-1, -1)
            self._buf_anchor = None
            self._seen_rows.clear()
        self._buf_identity = identity

    def _tick_buffer(self, now):
        self._check_buffer_file()
        rows = self._buffer_rows(None if self.cur[0] < 0
                                 else self.cur[0] - self.OVERLAP_MS)
        if not rows:
            latest = self.buf.q(
                "select created_at,ordinal,session_id,turn_id "
                "from active_turn_projection_items "
                "order by created_at desc,ordinal desc limit 1")
            if latest:
                anchor = tuple(latest[0])
                high = (int(anchor[0]), int(anchor[1]))
                # 空闲或原地流式改写不是换代；只按回卷、会话/回合变化重置。
                if high < self.cur or (high == self.cur and self._buf_anchor is not None
                                       and anchor[2:4] != self._buf_anchor):
                    self.cur = (-1, -1)
                    self._seen_rows.clear()
                    rows = self._buffer_rows(None)
        out = []
        for sid, tid, itype, ijson, ca, ordinal, parent in rows:
            try:
                ca = int(ca)
                ordinal = int(ordinal)
            except Exception:
                continue
            ident = (sid, tid, itype, ca, ordinal,
                     "" if parent is None else str(parent))
            if ident in self._seen_rows:
                continue
            self._remember_row(ident)
            if (ca, ordinal) > self.cur:
                self.cur = (ca, ordinal)
            self._buf_anchor = (sid, tid)
            if (sid, tid) in self._closed:
                continue
            self._watch(sid, tid)
            if sid:
                self.session = sid
            if tid and tid != self.turn:
                self.turn = tid
                self.steps = 0
            out.extend(self._parse(sid, tid, itype, ijson, ordinal, ca / 1000.0, parent))
        return out

    def _buffer_rows(self, since):
        sql = ("select session_id,turn_id,item_type,item_json,created_at,ordinal,"
               "parent_tool_use_id from active_turn_projection_items ")
        if since is None:
            return self.buf.q(sql + "order by created_at,ordinal,session_id,turn_id "
                              "limit %d" % self.ROW_LIMIT)
        return self.buf.q(sql + "where created_at>=? "
                          "order by created_at,ordinal,session_id,turn_id limit %d"
                          % self.ROW_LIMIT, (since,))

    def _remember_row(self, ident):
        self._seen_rows[ident] = None
        self._seen_rows.move_to_end(ident)
        while len(self._seen_rows) > self.SEEN_LIMIT:
            self._seen_rows.popitem(last=False)

    def _prime_seen(self):
        """启动时把窗口内的历史行标成已读，只跟踪之后发生的事。"""
        if self.cur[0] < 0:
            return
        rows = self.buf.q(
            "select session_id,turn_id,item_type,created_at,ordinal,parent_tool_use_id "
            "from active_turn_projection_items where created_at>=? "
            "order by created_at,ordinal,session_id,turn_id limit %d" % self.ROW_LIMIT,
            (self.cur[0] - self.OVERLAP_MS,))
        for sid, tid, itype, ca, ordinal, parent in rows:
            try:
                key = (int(ca), int(ordinal))
            except Exception:
                continue
            if key <= self.cur:
                self._remember_row((sid, tid, itype, key[0], key[1],
                                    "" if parent is None else str(parent)))

    def _parse(self, sid, tid, itype, ijson, ordinal, ts, parent=None):
        try:
            o = json.loads(ijson)
        except Exception:
            return []
        if (not isinstance(o, dict) or (sid, tid) in self._closed
                or o.get('backgroundTaskId') or o.get('responseKind')):
            return []
        ty = str(o.get("type") or itype or "")
        c = o.get("message", o)
        content = c.get("content") if isinstance(c, dict) else None
        blocks = content if isinstance(content, list) else []
        turns = []
        if ty == "user" or itype == "user":
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            for b in results:
                call = b.get("tool_use_id")
                if self._question_identity(sid, tid, call) and not parent:
                    key = (sid, tid)
                    history = self._question_history.get(key, {})
                    known = (call in self._questions.get(key, {})
                             or history is None or history.get(call, False))
                    turns.extend(self._resolve_question(sid, tid, call, ordinal, ts))
                    if known:
                        continue
                tx = _content_to_text(b.get("content"))
                if tx:
                    turns.append(self.emit(K_TOOL_DONE, tx, ts, sid, tid, ordinal))
            txt = _content_to_text(content)
            if txt and not results:
                if not parent:
                    self._new_user_turn(sid, tid, ts)
                turns.append(self.emit(K_USER, txt, ts, sid, tid, ordinal))
                self.steps = 0
        else:
            for index, b in enumerate(blocks):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use" and b.get("name") == "AskUserQuestion":
                    if ty == "assistant" and not parent:
                        turns.extend(self._start_question(sid, tid, b.get("id"), ordinal, ts))
                    continue
                if index >= 10:
                    continue
                if bt == "tool_use":
                    name, arg = _tool_of(b)
                    turns.append(self.emit(K_TOOL, name + ((" | " + arg) if arg else ""), ts,
                                           sid, tid, ordinal, tool=name, args=short(arg, 120)))
                elif bt == "thinking":
                    tx = _content_to_text(b.get("thinking"))
                    if tx:
                        turns.append(self.emit(K_THINK, tx, ts, sid, tid, ordinal))
            txt = _content_to_text(content)
            if txt:
                turns.append(self.emit(K_ANSWER, txt, ts, sid, tid, ordinal))
        return turns

    # ----------------------------------------------------------
    def _tick_main(self, now):
        rows = self.main.q(
            "select session_id,turn_id,sequence,payload_json,role,created_at "
            "from chat_session_messages "
            "where created_at>? or (created_at=? and sequence>?) "
            "order by created_at,sequence limit 100",
            (self.mcur[0], self.mcur[0], self.mcur[1]))
        out = []
        for sid, tid, seq, payload, role, ca in rows:
            try:
                self.mcur = (int(ca), int(seq))
            except Exception:
                pass
            try:
                o = json.loads(payload)
            except Exception:
                continue
            if not isinstance(o, dict) or o.get('backgroundTaskId') or o.get('responseKind'):
                continue
            if (sid, tid) in self._closed:
                continue
            self._watch(sid, tid)
            if role == 'assistant' and o.get('completedAt'):
                continue
            ts = (ca / 1000.0) if isinstance(ca, (int, float)) and ca > 1e11 else now
            parts = o.get("parts") if isinstance(o.get("parts"), list) else []
            user_result = role == 'user' and any(isinstance(p, dict) and p.get('type') == 'tool_result'
                                                 for p in parts)
            if (role == 'user' and not user_result
                    and any(isinstance(p, dict) and p.get('type') == 'text' and p.get('text') for p in parts)):
                self._new_user_turn(sid, tid, ts)
            for index, p in enumerate(parts):
                if not isinstance(p, dict):
                    continue
                pt = str(p.get("type") or "")
                tool = p.get("tool") if pt == "tool" else p
                if isinstance(tool, dict) and tool.get("name") == "AskUserQuestion":
                    if role != "assistant" or tool.get("parentToolUseId"):
                        continue
                    call = tool.get("id")
                    state = tool.get("status") if pt == "tool" else "running"
                    if pt in ("tool", "tool_use"):
                        if state == "running":
                            out.extend(self._start_question(sid, tid, call, int(seq or 0), ts))
                        elif state in ("completed", "failed", "canceled", "interrupted"):
                            out.extend(self._resolve_question(sid, tid, call, int(seq or 0), now, native=True))
                    continue
                if pt == "tool_result" and role == "user":
                    out.extend(self._resolve_question(sid, tid, p.get("tool_use_id"), int(seq or 0), ts))
                    continue
                if index >= 8:
                    continue
                if pt == "text" and not user_result:
                    tx = short(str(p.get("text") or ""), 200)
                    if tx:
                        k = K_USER if role == "user" else K_ANSWER
                        s = self.emit(k, tx, ts, sid or "", tid or "", int(seq or 0))
                        out.append(s)
                elif pt == "tool_use":
                    name, arg = _tool_of(p)
                    s = self.emit(K_TOOL, name + ((" | " + arg) if arg else ""), ts,
                                  sid or "", tid or "", int(seq or 0),
                                  tool=name, args=short(arg, 120))
                    out.append(s)
                elif pt == "thinking":
                    tx = short(str(p.get("thinking") or p.get("text") or ""), 160)
                    if tx:
                        s = self.emit(K_THINK, tx, ts, sid or "", tid or "",
                                      int(seq or 0))
                        out.append(s)
                elif pt == "hook":
                    # PostToolUse / PreToolUse / UserPromptSubmit / Stop ...
                    h = p.get("hook") or {}
                    ev = str(h.get("event") or "hook")
                    k = (K_TOOL_DONE if "Post" in ev else
                         K_TOOL if "Pre" in ev else
                         K_USER if "Prompt" in ev else
                         K_STATUS)
                    tx = ev + ("" if h.get("status") == "succeeded" else
                               " (%s)" % (h.get("status") or "?"))
                    s = self.emit(k, tx, ts, sid or "", tid or "", int(seq or 0),
                                  hook=ev)
                    out.append(s)
        self._files(now, out)
        return out

    def _files(self, now, out):
        """改了哪些文件（+增删行数），每回合只报一次。"""
        if (not self.turn or self.turn == self._file_turn
                or (self.session, self.turn) in self._closed):
            return
        rows = self.main.q(
            "select f.display_path,f.operation,f.additions,f.deletions "
            "from turn_file_change_files f join turn_file_change_sets s "
            "on s.change_set_id=f.change_set_id "
            "where s.turn_id=? limit 40", (self.turn,))
        if not rows:
            return
        self._file_turn = self.turn
        for path, op, add, dele in rows:
            txt = "%s %s +%s/-%s" % (op or "edit", os.path.basename(str(path or "")),
                                     add or 0, dele or 0)
            s = self.emit(K_EDIT, txt, now, self.session, self.turn,
                          file=str(path or ""), additions=int(add or 0),
                          deletions=int(dele or 0))
            out.append(s)

    def status(self, now=None):
        st = Adapter.status(self, now)
        if not st.title:
            rows = self.main.q(
                "select title from chat_sessions where session_id=? limit 1",
                (self.session,)) if (self.main and self.main.path) else []
            if rows and rows[0][0]:
                st.title = short(str(rows[0][0]), 60)
            else:
                st.title = short(self.session, 20)
        return st
