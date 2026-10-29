# -*- coding: utf-8 -*-
"""豆包工作（Doubao Work）专用适配器 —— A 级：真实的结构化事件。

数据源（豆包工作自己写的进程日志，不是猜的）：
  %LOCALAPPDATA%/DoubaoWork/User Data/sdk_storage/log/saman_YYYY.MMDD.N.log
  新版会话事件落在同目录的 saman_frontend_YYYY.MMDD.N.log（渲染进程），
  两个目录都按 mtime 取最新若干个，哪个有新事件就读哪个。

日志里混着三种东西：
  1) Chromium 控制台前缀 + JSON：`[pid:tid:date/time:INFO:CONSOLE(0)] [...] "…"`
     其中带 eventName/meta 的是 office-api 事件，真正能描述任务生命周期：
       on_message_session_start      会话开始（resumed=1 表示恢复未完成任务）
       on_message_receiving_start    助手回复开始接收
       flow_message_time             细粒度阶段（loading/cot/round_N/answer_first_token）
       on_message_receiving_end      本条回复接收结束（receive_success）
       on_message_session_end        会话结束（session_success / agent_error / cancel_source）
       rd_flow_message_streaming_finished  流式输出结束（is_interrupted）
       message_task_association_reply_message_ended  回复关联结束（end_status）
  2) 纯 SSE 事件 `{"event": "...", "data": {...}}`：
       toolcall  工具调用（name / status=received|success / tcid / seq）
       command   下发到沙箱的命令（name / status / need_ack）
  3) 网络框架自己的日志（engine_startup 之类），一律忽略。

一条底线：**只有 on_message_session_end 才是"这一轮结束"的真实证据。**
receiving_end / reply_message_ended 只说明某条回复的接收/关联结束，
同一个任务里可能还有后续轮次，用它们报 done 会把"还在跑"显示成"已完成"。
日志里没有任何可证实的"等你选择/授权"事件（authorization_mode 是策略不是提问），
所以本适配器**不推断 ask**，宁可少报也不谎报。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict

from adapters.base import Adapter, TailReader
from core.model import (K_ANSWER, K_ERROR, K_RESULT, K_STATUS, K_THINK, K_TOOL,
                        K_TOOL_DONE, K_USER, P_ANSWERING, P_IDLE, P_THINKING,
                        short)

# 只认这两类行，别把 55MB 的网络/渲染日志全解析一遍
_OFFICE_EVENTS = frozenset((
    "on_message_session_start", "on_message_session_end",
    "on_message_receiving_start", "on_message_receiving_end",
    "flow_message_time", "rd_flow_message_streaming_finished",
    "message_task_association_reply_message_ended",
    "gui_coding_cu_windows_reply_end",
))
_SSE_EVENTS = frozenset(("toolcall", "command"))
_ROUND_RE = re.compile(r"^round_(\d+)_first_token$")
_END_OK = frozenset(("success", "succeeded", "ok", "completed", "done"))
_END_FAIL = frozenset(("failed", "failure", "error", "timeout", "rejected"))
_END_CANCEL = frozenset(("canceled", "cancelled", "interrupted", "stopped", "aborted"))


def _s(value, n=64):
    return str(value).strip()[:n] if isinstance(value, str) else ""


def _turn_of(meta):
    # answer_id 与 receive_message_id 是同一个标识（真实日志 70/70 相等），
    # 而 message_id / reply_message_id 是另一套 id，混用会让同一轮看起来
    # 换了轮次，后续 session_end 就会被当成“旧轮”而吞掉完成态。
    for k in ("receive_message_id", "answer_id", "send_message_id"):
        v = _s(meta.get(k), 40)
        if v:
            return v
    return ""


def _span(raw):
    """取第一条 `{` 到最后一条 `}`：日志前后还有控制台前缀和引号。"""
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        o = json.loads(raw[i:j + 1])
    except Exception:
        return None
    return o if isinstance(o, dict) else None


class DoubaoWorkAdapter(Adapter):
    key = "doubao_work"
    zh = "豆包工作"
    en = "Doubao Work"
    tier = "A"
    T_ACTIVE, T_QUIET, T_AWAY = 0.5, 2.0, 20.0
    SCAN_EVERY = 5.0
    MAX_TAILS = 4
    MAX_SESSIONS = 64
    SESSION_TTL = 2 * 3600.0
    OPEN_TTL = 30 * 60.0

    def __init__(self, info, log=None):
        Adapter.__init__(self, info, log)
        self.tails = []
        self._paths = []
        self._scan_at = 0.0
        self._sess = OrderedDict()   # sid -> {"turn", "at", "open"}
        self._tool_seen = OrderedDict()
        self._dup_at = OrderedDict()  # 语义去重键 -> 首次出现时间

    # ---------------------------------------------------------- 数据源
    def _log_dirs(self):
        dirs = []
        for r in self.roots:
            if not r or not os.path.isdir(r):
                continue
            for cand in (os.path.join(r, "User Data", "sdk_storage", "log"),
                         os.path.join(r, "sdk_storage", "log")):
                if os.path.isdir(cand):
                    dirs.append(cand)
        return dirs

    @staticmethod
    def _is_task_log(name):
        # 新版把会话事件写在 saman_frontend_*.log（渲染进程），旧版写在
        # saman_*.log（主进程），两个都要认；crashpad/repair 的 mtime 更新
        # 很频繁，混进来会把真正的日志挤出名额。
        return bool(re.match(r"^saman_(frontend_)?\d{4}\.\d{4}\.\d+\.log$", name, re.I))

    def _dup(self, key, now):
        # 渲染进程会把同一个逻辑事件重复写多行（同一 answer_id/message_id），
        # 不去重就会让桌宠把同一步播两遍。
        t = self._dup_at.get(key)
        if t is not None and now - t < 10.0:
            return True
        self._dup_at[key] = now
        self._dup_at.move_to_end(key)
        while len(self._dup_at) > 512:
            self._dup_at.popitem(last=False)
        return False

    def _refresh(self, first):
        cands = []
        for d in self._log_dirs():
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if not self._is_task_log(name):
                    continue
                p = os.path.join(d, name)
                try:
                    cands.append((os.path.getmtime(p), p))
                except OSError:
                    continue
        cands.sort(reverse=True)
        paths = [p for _, p in cands[:self.MAX_TAILS]]
        if paths == self._paths and not first:
            return
        keep = {t.path: t for t in self.tails}
        tails = []
        for p in paths:
            t = keep.get(p)
            if t is None:
                t = TailReader(p, start_at_end=True, max_step=1 << 18)
            tails.append(t)
        self.tails, self._paths = tails, paths

    def setup(self):
        self._scan_at = 0.0
        self._refresh(True)
        if not self.tails:
            return False
        self.enabled = True
        self.source = ",".join(os.path.basename(p) for p in self._paths)
        self.log("[doubao_work] 日志=%s" % self.source)
        return True

    def replay(self, n=25):
        Adapter.replay(self, n)
        for t in self.tails:
            try:
                t.rewind(120000)
            except Exception:
                pass

    # ---------------------------------------------------------- 轮询
    def tick(self, now):
        if now >= self._scan_at:
            self._scan_at = now + self.SCAN_EVERY
            self._refresh(False)
        out = []
        for tail in self.tails:
            try:
                lines = tail.read_lines()
            except Exception:
                continue
            for line in lines:
                out.extend(self._line(line, now))
        self._expire(now)
        return self._take(out)

    def _expire(self, now):
        for st in self._sess.values():
            # 应用异常退出时不会有 session_end；一直算“未结束”会让 flow/工具
            # 事件永远处于多会话歧义里。超时只解除归属，不制造终态。
            if st["open"] and now - st["at"] > self.OPEN_TTL:
                st["open"] = False
        for sid in list(self._sess):
            if now - self._sess[sid]["at"] > self.SESSION_TTL:
                del self._sess[sid]
        while len(self._sess) > self.MAX_SESSIONS:
            self._sess.popitem(last=False)
        while len(self._tool_seen) > 512:
            self._tool_seen.popitem(last=False)

    def _line(self, raw, now):
        # 55MB/天的日志里绝大多数是网络/渲染行；先做子串筛选，
        # 否则每行都要把首尾大括号之间整段解析一遍。
        if '"eventName"' not in raw and '"event"' not in raw:
            return []
        o = _span(raw)
        if o is None:
            return []
        name = o.get("eventName")
        if isinstance(name, str) and name:
            if name not in _OFFICE_EVENTS:
                return []
            meta = o.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            steps = self._office(name, meta, now)
            if steps:
                s0 = steps[0]
                sig = (name, s0.session, s0.turn, s0.kind,
                       _s(meta.get("ev_name"), 40), _s(meta.get("end_status"), 20))
                if self._dup(sig, now):
                    return []
            return steps
        ev = o.get("event")
        # engine_startup 之类的 event 是对象而不是字符串，直接拿去查集合会抛
        # TypeError（dict 不可哈希），整个适配器每轮都失败。
        if isinstance(ev, str) and ev in _SSE_EVENTS:
            data = o.get("data")
            return self._sse(ev, data if isinstance(data, dict) else {}, now)
        return []

    # ---------------------------------------------------------- 会话状态
    def _touch(self, sid, turn, open_=None, now=None):
        if not sid:
            return
        st = self._sess.get(sid)
        if st is None:
            st = {"turn": "", "at": 0.0, "open": False}
            self._sess[sid] = st
        if turn:
            st["turn"] = turn
        if open_ is not None:
            st["open"] = open_
        st["at"] = time.time() if now is None else now
        self._sess.move_to_end(sid)

    def _sole_open(self):
        opens = [sid for sid, st in self._sess.items() if st["open"]]
        return opens[0] if len(opens) == 1 else ""

    # ---------------------------------------------------------- office 事件
    def _scope(self, sid, turn):
        """没有会话 ID 的细分事件（flow / 流式结束 / 回复结束）只认唯一未结束的会话。

        多个会话同时在跑时宁可不报，也不能张冠李戴到别的会话上。
        """
        if sid:
            st = self._sess.get(sid) or {}
            return sid, (st.get("turn", "") or turn)
        only = self._sole_open()
        if not only:
            return "", ""
        st = self._sess.get(only) or {}
        return only, (st.get("turn", "") or turn)

    def _office(self, name, meta, now):
        sid = _s(meta.get("chat_session_id")) or _s(meta.get("session_id"))
        st = self._sess.get(sid) if sid else None
        turn = _turn_of(meta) or (st["turn"] if st else "")
        if name == "on_message_session_start":
            if not sid:
                return []
            # 同一会话未结束时再来一次 session_start 是应用内部的续跑，
            # 不是用户又发了一条，报成“新任务”会多出一张卡片。
            fresh = st is None or not st["open"]
            self._touch(sid, turn, open_=True, now=now)
            if not fresh:
                return []
            resumed = str(meta.get("resumed") or "0").lower() in ("1", "true")
            text = "继续未完成的任务" if resumed else "收到新任务，开始处理"
            return [self.emit(K_USER, text, now, sid, turn, evidence=name)]
        if name == "on_message_receiving_start":
            if not sid:
                return []
            self._touch(sid, turn, open_=True, now=now)
            return [self.emit(K_THINK, "模型开始回复", now, sid, turn,
                              evidence=name)]
        if name == "on_message_receiving_end":
            if not sid:
                return []
            # 不在这里关闭会话：工具调用与后续轮次仍属于同一任务，
            # 关了会让它们失去归属，最后连 session_end 的完成态都无处落。
            self._touch(sid, turn, now=now)
            ok = str(meta.get("receive_success")).lower() in ("1", "true")
            err = _s(meta.get("receive_err"), 60)
            if ok:
                return [self.emit(K_STATUS, "本条回复接收完成", now, sid, turn,
                                  evidence=name, phase=P_ANSWERING)]
            return [self.emit(K_STATUS, "回复接收异常" + (("：" + err) if err else ""),
                              now, sid, turn, evidence=name, phase=P_ANSWERING)]
        if name == "flow_message_time":
            sid, turn = self._scope(sid, turn)
            if not sid:
                return []
            self._touch(sid, turn, now=now)
            return self._flow(meta, sid, turn, now)
        if name == "rd_flow_message_streaming_finished":
            sid, turn = self._scope(sid, turn)
            if not sid:
                return []
            interrupted = str(meta.get("is_interrupted")).lower() in ("1", "true")
            if not interrupted:
                return []
            return [self.emit(K_STATUS, "输出被中断", now, sid, turn,
                              evidence=name, phase="cancelled",
                              outcome="cancelled")]
        if name == "message_task_association_reply_message_ended":
            sid, turn = self._scope(sid, turn)
            if not sid:
                return []
            self._touch(sid, turn, now=now)
            return self._reply_end(meta, sid, turn, now)
        if name == "gui_coding_cu_windows_reply_end":
            # 只是 GUI 侧对回复结束的消费结果，不是独立的结束证据。
            return []
        return self._session_end(meta, sid, turn, now)

    def _flow(self, meta, sid, turn, now):
        ev = _s(meta.get("ev_name"), 40)
        if not ev:
            return []
        if ev == "loading_first_token":
            return [self.emit(K_THINK, "等待首个输出", now, sid, turn, evidence=ev)]
        if ev == "cot_first_token":
            return [self.emit(K_THINK, "开始推理", now, sid, turn, evidence=ev)]
        if ev == "answer_first_token":
            return [self.emit(K_ANSWER, "开始输出结果", now, sid, turn, evidence=ev)]
        m = _ROUND_RE.match(ev)
        if m:
            n = min(99, int(m.group(1) or 0))
            return [self.emit(K_THINK, "第 %d 轮推理" % n, now, sid, turn, evidence=ev)]
        if ev == "task_finish":
            # 只是"输出结束"，同一任务可能还有后续轮次，不当作终态。
            return [self.emit(K_STATUS, "本轮输出结束", now, sid, turn,
                              evidence=ev, phase=P_ANSWERING)]
        return []

    def _reply_end(self, meta, sid, turn, now):
        status = str(meta.get("end_status") or "").strip().lower()
        if not status:
            return []
        if status in _END_OK:
            return [self.emit(K_STATUS, "本条回复处理结束", now, sid, turn,
                              evidence="reply_message_ended:" + status,
                              phase=P_ANSWERING)]
        if status in _END_FAIL:
            # 单条回复失败不等于整轮结束（可能重试），终态只认 session_end。
            return [self.emit(K_ERROR, "回复处理失败：" + status, now, sid, turn,
                              evidence="reply_message_ended:" + status)]
        if status in _END_CANCEL:
            return [self.emit(K_STATUS, "回复已停止", now, sid, turn,
                              evidence="reply_message_ended:" + status,
                              phase="cancelled", outcome="cancelled")]
        return [self.emit(K_STATUS, "回复处理结束：" + status, now, sid, turn,
                          evidence="reply_message_ended:" + status)]

    def _session_end(self, meta, sid, turn, now):
        if not sid:
            return []
        self._touch(sid, turn, open_=False, now=now)
        cancel = _s(meta.get("cancel_source"), 40)
        agent_err = str(meta.get("agent_error") or "0")
        ok = str(meta.get("session_success") or "").lower() in ("1", "true")
        if cancel:
            return [self.emit(K_STATUS, "本轮已取消", now, sid, turn,
                              evidence="on_message_session_end.cancel_source",
                              phase="cancelled", outcome="cancelled")]
        if agent_err not in ("", "0", "None", "false"):
            return [self.emit(K_ERROR, "本轮执行失败", now, sid, turn,
                              evidence="on_message_session_end.agent_error",
                              terminal=True)]
        if ok:
            return [self.emit(K_RESULT, "本轮已完成", now, sid, turn,
                              evidence="on_message_session_end.session_success",
                              terminal=True)]
        return [self.emit(K_STATUS, "本轮会话已结束", now, sid, turn,
                          evidence="on_message_session_end")]

    # ---------------------------------------------------------- SSE 事件
    def _sse(self, ev, data, now):
        if ev == "toolcall":
            name = _s(data.get("name"), 32)
            if not name:
                return []
            status = str(data.get("status") or "").lower()
            tcid = _s(data.get("tcid") or data.get("toolCallID"), 64)
            sid = self._sole_open()
            if not sid:
                # 无归属时不能先记去重键，否则等会话明确后同一条事件会被永久吞掉。
                return []
            ident = (tcid, status)
            if tcid:
                if ident in self._tool_seen:
                    return []
                self._tool_seen[ident] = None
            st = self._sess.get(sid) or {}
            turn = st.get("turn", "")
            if status in ("success", "succeeded", "ok", "completed", "done"):
                return [self.emit(K_TOOL_DONE, "工具 %s 完成" % name, now, sid, turn,
                                  tool=name, evidence="toolcall")]
            if status in ("failed", "failure", "error"):
                return [self.emit(K_TOOL_DONE, "工具 %s 执行失败" % name, now, sid, turn,
                                  tool=name, evidence="toolcall")]
            return [self.emit(K_TOOL, "工具 %s 执行中" % name, now, sid, turn,
                              tool=name, evidence="toolcall")]
        name = _s(data.get("name"), 40)
        if not name:
            return []
        status = str(data.get("status") or "").lower()
        cid = _s(data.get("command_id"), 64)
        sid = self._sole_open()
        if not sid:
            return []
        if cid:
            ident = ("cmd", cid, status)
            if ident in self._tool_seen:
                return []
            self._tool_seen[ident] = None
        st = self._sess.get(sid) or {}
        turn = st.get("turn", "")
        if status in ("success", "succeeded", "ok", "completed", "done"):
            return [self.emit(K_TOOL_DONE, "命令 %s 完成" % name, now, sid, turn,
                              tool=name, evidence="command")]
        if status in ("failed", "failure", "error"):
            return [self.emit(K_TOOL_DONE, "命令 %s 执行失败" % name, now, sid, turn,
                              tool=name, evidence="command")]
        return [self.emit(K_TOOL, "下发命令 %s" % name, now, sid, turn,
                          tool=name, evidence="command")]
