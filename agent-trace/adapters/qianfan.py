# -*- coding: utf-8 -*-
"""百度搭子（DuMate / 千帆桌面应用）适配器 —— A 级：结构化事件流。

为什么单独写一个：
  DuMate 不是普通的 Electron 聊天壳。它真实身份是「千帆桌面应用
  (qianfan-desktop-app) + 内嵌 opencode 智能体」——主进程
  dumate-opencode.exe 在本地起了一个 opencode 服务，把每个动作都写成
  事件流。所以 %APPDATA%\\DuMate 这类目录根本不存在，靠通用 Chromium
  探针永远抓不到东西（旧版就只退化成「窗口标题」兜底）。

真实数据源（实测）：
  %APPDATA%\\qianfan-desktop-app\\log\\desktop-main\\AgtEvent.log
  每行 = `时间\\tDEBUG\\t[AgtEvent]\\tevent: {JSON}`，含：

    session.created / session.updated    会话标题（= 用户下达的任务）
    session.status                       busy / idle
    message.part.updated  type=tool      工具名 + input + running/completed + 耗时
    message.part.updated  type=text      回答正文
    message.part.reasoning-summary       思考摘要
    todo.updated                         计划 / 待办
    file.edited / type=file-export       产物落盘
    session.turn.completed               本轮结束

只读、只增量 tail，不碰对方任何文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict

from adapters.base import Adapter, TailReader
from core.model import (K_ANSWER, K_ASK, K_EDIT, K_ERROR, K_PLAN, K_RESULT, K_STATUS,
                        K_THINK, K_TOOL, K_TOOL_DONE, K_USER, P_ASKING, P_THINKING, short)

class _Recent(OrderedDict):
    LIMIT = 512
    TTL = 600.0

    def __setitem__(self, key, value):
        super().__setitem__(key, (time.monotonic(), value))
        self.move_to_end(key)
        while len(self) > self.LIMIT:
            self.popitem(last=False)

    def get(self, key, default=None):
        item = super().get(key)
        if item is None:
            return default
        if time.monotonic() - item[0] >= self.TTL:
            self.pop(key, None)
            return default
        return item[1]

    def prune(self, now):
        while self:
            key = next(iter(self))
            if now - super().__getitem__(key)[0] < self.TTL:
                break
            self.popitem(last=False)


_EDIT_TOOLS = ("write", "edit", "patch", "multiedit", "multiwrite", "create")
_FILE_TOOLS = ("read", "write", "edit", "patch", "multiedit", "multiwrite",
               "create", "delete", "move", "rename")


class BaiduDaziAdapter(Adapter):
    key = "baidu_dazi"
    zh = "百度搭子"
    en = "Baidu Dazi (DuMate)"
    tier = "A"
    T_ACTIVE, T_QUIET, T_AWAY = 0.4, 2.5, 20.0

    # 相对每个数据根的候选路径
    LOG_CANDIDATES = (
        ("log", "desktop-main", "AgtEvent.log"),
        ("AgtEvent.log",),
    )

    def __init__(self, info, log=None):
        Adapter.__init__(self, info, log)
        self.tail = None
        self._tools = _Recent()
        self._texts = _Recent()
        self._thinks = _Recent()
        self._titles = _Recent()
        self._busy = _Recent()
        self._busy.TTL = float("inf")
        self._todos = _Recent()
        self._edited = _Recent()
        self._questions = OrderedDict()
        self._resolved_questions = _Recent()
        self._resolved_questions.TTL = float("inf")

    QUESTION_LIMIT = 128
    QUESTION_REFRESH = 5.0
    QUESTION_TEXT = "等待你在百度搭子中确认选择"

    def _waiting(self, sid):
        return any(key[0] == sid for key in self._questions)

    def _question_ask(self, key, now, ts=None):
        self._questions[key]["last"] = now
        return self._emit(K_ASK, self.QUESTION_TEXT, now if ts is None else ts,
                          session=key[0], requestID=key[1], tool="question")

    def _question_resolve(self, key, ts):
        pending = self._questions.get(key)
        if pending and ts < pending["ts"]:
            return []
        self._resolved_questions[key] = True
        if pending is None:
            return []
        del self._questions[key]
        self.phase = P_THINKING
        return [self._emit(K_STATUS, "选择已结束，继续处理", ts, session=key[0],
                           requestID=key[1], tool="question", phase=P_THINKING)]

    def _question_event(self, et, pr, ts, now):
        sid = pr.get("sessionID")
        rid = pr.get("id" if et == "question.asked" else "requestID")
        if not all(isinstance(v, str) and v for v in (sid, rid)):
            return []
        if ts < self._busy.get(sid, {}).get("ts", 0):
            return []
        key = (sid, rid)
        if et == "question.replied":
            return self._question_resolve(key, ts)
        if key in self._questions or self._resolved_questions.get(key):
            return []
        tool = pr.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        self._questions[key] = {"ts": ts, "last": now,
                                "message": tool.get("messageID"),
                                "call": tool.get("callID")}
        while len(self._questions) > self.QUESTION_LIMIT:
            old, _ = self._questions.popitem(last=False)
            self._resolved_questions[old] = True
        return [self._question_ask(key, now, ts)]

    def _prune(self):
        now = time.monotonic()
        for cache in (self._tools, self._texts, self._thinks, self._titles,
                      self._busy, self._todos, self._edited):
            cache.prune(now)

    def _clear_session(self, sid, keep_state=False, clear_questions=True):
        if clear_questions:
            for key in list(self._questions):
                if key[0] == sid:
                    del self._questions[key]
                    self._resolved_questions[key] = True
        for cache in (self._tools, self._texts, self._thinks, self._edited):
            for key in list(cache):
                if key[0] == sid:
                    cache.pop(key, None)
        self._todos.pop(sid, None)
        if not keep_state:
            self._titles.pop(sid, None)
            self._busy.pop(sid, None)

    def _closed(self, sid):
        return self._busy.get(sid, {}).get("state") == "closed"

    def _open_session(self, sid, ts, user=""):
        state = self._busy.get(sid, {})
        if ts < state.get("ts", 0):
            return False  # 旧日志里的 busy / 用户消息不能重开已完成轮次
        if user:
            if user == state.get("user"):
                return False
        elif state.get("state") == "busy":
            return False
        self._clear_session(sid, keep_state=True, clear_questions=bool(user))
        self._busy[sid] = {"state": "busy", "ts": ts,
                           "user": user or state.get("user", "")}
        return True

    def _finish_session(self, sid, ts):
        state = self._busy.get(sid, {})
        if state.get("state") == "closed" or ts < state.get("ts", 0):
            return []
        title = self._titles.get(sid) or "本轮完成"
        self._clear_session(sid, keep_state=True)
        # 与 busy 共用原有容量上限；关闭标记不随 part 缓存 TTL 消失。
        self._busy[sid] = dict(state, state="closed", ts=ts)
        return [self._emit(K_RESULT, title, ts, session=sid, terminal=True)]

    # ---------------------------------------------------------- 挂载
    def setup(self):
        path = self._find_log()
        if not path:
            return False
        try:
            self.tail = TailReader(path)
        except Exception as e:
            self.log("[%s] 打开事件流失败：%s" % (self.key, e))
            return False
        self.enabled = True
        self.source = os.path.basename(path)
        self.log("[%s] 事件流=%s" % (self.key, path))
        return True

    def _find_log(self):
        cands = []
        for r in self.roots:
            if not r or not os.path.isdir(r):
                continue
            for rel in self.LOG_CANDIDATES:
                p = os.path.join(r, *rel)
                if os.path.isfile(p):
                    cands.append(p)
        appdata = os.environ.get("APPDATA") or ""
        if appdata:
            p = os.path.join(appdata, "qianfan-desktop-app", "log",
                             "desktop-main", "AgtEvent.log")
            if os.path.isfile(p):
                cands.append(p)
        if not cands:
            return ""
        uniq = []
        seen = set()
        for p in cands:
            k = p.lower()
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        # 多份时取最新的那份（轮转/多用户）
        try:
            return max(uniq, key=lambda x: os.path.getmtime(x))
        except OSError:
            return uniq[0]

    # ---------------------------------------------------------- 轮询
    def tick(self, now):
        self._prune()
        out = []
        try:
            lines = self.tail.read_lines() if self.tail else []
        except Exception:
            lines = []
        for line in lines:
            try:
                out.extend(self._line(line, now))
            except Exception:
                continue          # 单行解析失败不影响后续
        # 最后刷新，避免同批工具活动盖掉尚未提交的选择。
        for key, pending in self._questions.items():
            if now - pending["last"] >= self.QUESTION_REFRESH:
                out.append(self._question_ask(key, now))
        return self._take(out)

    def replay(self, n=25):
        Adapter.replay(self, n)
        if self.tail:
            try:
                self.tail.rewind(200 * 1024)
            except Exception:
                pass

    # ---------------------------------------------------------- 解析
    def _line(self, line, now):
        i = line.find("event: ")
        if i < 0:
            return []
        try:
            ev = json.loads(line[i + 7:])
        except Exception:
            return []
        if not isinstance(ev, dict):
            return []
        ts = self._line_ts(line, now)
        et = ev.get("type") or ""
        pr = ev.get("properties") or {}
        if not isinstance(pr, dict):
            return []

        info = pr.get("info") if isinstance(pr.get("info"), dict) else {}
        sid = pr.get("sessionID") or info.get("sessionID") or info.get("id") or ""
        if et == "message.updated":
            # 只有带身份的用户消息能开新轮；assistant 归档更新不是开轮证据。
            sid = pr.get("sessionID") or info.get("sessionID") or ""
            mid = info.get("id") or ""
            if (info.get("role") == "user" and sid and mid
                    and self._open_session(sid, ts, user=mid)):
                return [self._emit(K_USER, "收到新的提问", ts, session=sid)]
            return []
        if self._closed(sid) and et not in (
                "session.status", "session.turn.completed", "session.deleted"):
            return []

        if et in ("question.asked", "question.replied"):
            return self._question_event(et, pr, ts, now)

        if et == "message.part.updated":
            part = pr.get("part") or {}
            if isinstance(part, dict):
                return self._part(part, ts)

        elif et in ("session.created", "session.updated"):
            info = pr.get("info") or {}
            if not isinstance(info, dict):
                return []
            sid = info.get("id") or ""
            title = short(str(info.get("title") or ""), 200)
            if sid and title:
                self._titles[sid] = title
            if info.get("is_subagent"):
                return []              # 子代理的会话不占状态栏
            if title:
                self.title = title
            if et == "session.created" and title:
                return [self._emit(K_USER, title, ts, session=sid)]

        elif et == "session.status":
            sid = pr.get("sessionID") or ""
            st = (pr.get("status") or {}).get("type") if isinstance(pr.get("status"), dict) else ""
            if st == "busy":
                if self._open_session(sid, ts):
                    self.phase = P_THINKING
                    return [self._emit(K_STATUS, "开始处理", ts, session=sid,
                                       phase=P_THINKING)]
            elif st in ("idle", "done", "completed"):
                # busy -> idle 与 session.turn.completed 共用关闭屏障。
                if self._busy.get(sid, {}).get("state") == "busy" or self._waiting(sid):
                    return self._finish_session(sid, ts)
            return []

        elif et == "message.part.reasoning-summary":
            pid = (pr.get("sessionID") or "", pr.get("partID") or "")
            s = short(str(pr.get("summary") or ""), 200)
            if s and self._thinks.get(pid) != s:
                self._thinks[pid] = s
                return [self._emit(K_THINK, s, ts, session=pr.get("sessionID") or "")]

        elif et == "session.turn.completed":
            sid = pr.get("sessionID") or ""
            return self._finish_session(sid, ts)

        elif et == "session.deleted":
            info = pr.get("info") or {}
            sid = pr.get("sessionID") or (info.get("id") if isinstance(info, dict) else "") or ""
            self._clear_session(sid)

        elif et == "session.compacted":
            return [self._emit(K_STATUS, "上下文压缩", ts,
                               session=pr.get("sessionID") or "")]

        elif et == "todo.updated":
            return self._todos_step(pr, ts)

        elif et == "file.edited":
            fp = pr.get("file") or ""
            if fp:
                return self._file_step(fp, ts, pr.get("sessionID") or "")
        return []

    # -- 工具 / 文本 / 产物 -------------------------------------
    def _part(self, part, ts):
        pt = part.get("type") or ""
        sid = part.get("sessionID") or ""
        if self._closed(sid):
            return []
        pid = (sid, part.get("id") or "")

        if pt == "tool":
            st = part.get("state") or {}
            if not isinstance(st, dict):
                return []
            status = st.get("status") or ""
            tool = (part.get("tool") or "").lower()
            if tool == "question" and status in ("completed", "error"):
                for key, pending in self._questions.items():
                    if (key[0] == sid and pending["message"] and pending["call"]
                            and pending["message"] == part.get("messageID")
                            and pending["call"] == part.get("callID")
                            and ts >= max(pending["ts"], self._busy.get(sid, {}).get("ts", 0))):
                        self._tools[pid] = status
                        return self._question_resolve(key, ts)
            if status == "running":
                if self._tools.get(pid) == "running":
                    return []           # 同一个 part 的流式重复
                self._tools[pid] = "running"
                text = self._tool_text(tool, st)
                if not text:
                    return []
                return [self._emit(K_TOOL, text, ts, session=sid, tool=tool)]
            if status == "completed":
                if self._tools.get(pid) == "completed":
                    return []
                self._tools[pid] = "completed"
                dur = self._dur(st)
                title = (st.get("title") or "").strip() or self._tool_text(tool, st)
                out = []
                if tool in _EDIT_TOOLS and title:
                    out.append(self._emit(K_EDIT, title, ts, session=sid, tool=tool))
                out.append(self._emit(K_TOOL_DONE, self._with_dur(title, dur),
                                      ts, session=sid, tool=tool))
                return out
            if status == "error":
                if self._tools.get(pid) == "error":
                    return []
                self._tools[pid] = "error"
                msg = str(st.get("error") or st.get("output") or "工具出错")
                return [self._emit(K_ERROR, short(msg, 120), ts, session=sid,
                                   tool=tool, terminal=False)]
            return []                   # pending 不播报

        if pt == "text":
            if self._texts.get(pid):
                return []
            text = (part.get("text") or "").strip()
            ended = bool((part.get("time") or {}).get("end"))
            if not text or not ended:    # 等这段文字写完再播，避免一个字一个字刷
                return []
            self._texts[pid] = True
            if part.get("phase") == "commentary":
                return [self._emit(K_THINK, text, ts, session=sid)]
            return [self._emit(K_ANSWER, text, ts, session=sid)]

        if pt == "file-export":
            files = part.get("files") or []
            names = []
            for f in files[:5]:
                if isinstance(f, dict):
                    n = os.path.basename(str(f.get("path") or f.get("filename") or ""))
                    if n:
                        names.append(n)
            if names:
                sig = tuple(names)
                if self._texts.get(pid) == sig:
                    return []
                self._texts[pid] = sig
                return [self._emit(K_EDIT, "导出 " + "、".join(names[:3]), ts, session=sid)]
        return []

    def _todos_step(self, pr, ts):
        todos = pr.get("todos") or []
        sid = pr.get("sessionID") or ""
        if self._closed(sid) or not isinstance(todos, list) or not todos:
            return []
        sig = hashlib.sha256(json.dumps(
            [(t.get("subject"), t.get("status")) for t in todos if isinstance(t, dict)],
            ensure_ascii=False).encode("utf-8")).digest()
        if sig == self._todos.get(sid):
            return []
        self._todos[sid] = sig
        done = sum(1 for t in todos
                   if isinstance(t, dict) and (t.get("status") or "") in
                   ("completed", "done", "finished"))
        pend = [str(t.get("subject") or t.get("content") or "")
                for t in todos
                if isinstance(t, dict) and (t.get("status") or "") in
                ("pending", "in_progress", "running")]
        txt = "计划 %d 项（完成 %d）" % (len(todos), done)
        if pend and pend[0]:
            txt += " · 下一步：" + short(pend[0], 50)
        return [self._emit(K_PLAN, txt, ts, session=sid)]

    def _file_step(self, fp, ts, sid=""):
        if self._closed(sid):
            return []
        # 同一个文件 30 秒内只播一次（编辑器保存会连发）。
        key = (sid, fp.lower())
        last = self._edited.get(key)
        if last is not None and 0 <= ts - last < 30:
            return []
        self._edited[key] = ts
        return [self._emit(K_EDIT, os.path.basename(fp), ts, session=sid, file=fp)]

    # -- 小工具 -------------------------------------------------
    def _emit(self, kind, text, ts, **kw):
        waiting = self._waiting(kw.get("session", ""))
        if waiting:
            kw["phase"] = P_ASKING
        s = self.emit(kind, text, ts, **kw)
        self._note(kind, ts)
        if waiting:
            self.phase = P_ASKING
        elif kw.get("phase"):
            self.phase = kw["phase"]
        return s

    @staticmethod
    def _dur(st):
        t = st.get("time") or {}
        try:
            a, b = float(t.get("start") or 0), float(t.get("end") or 0)
            if a and b and b > a:
                return (b - a) / 1000.0
        except (TypeError, ValueError):
            pass
        return 0.0

    @staticmethod
    def _with_dur(text, dur):
        if not text:
            return ""
        return "%s (%.2fs)" % (text, dur) if dur else text

    @staticmethod
    def _tool_text(tool, st):
        inp = st.get("input") or {}
        if not isinstance(inp, dict):
            inp = {}
        title = (st.get("title") or "").strip()
        if tool == "bash":
            cmd = str(inp.get("command") or inp.get("cmd") or "").strip()
            desc = str(inp.get("description") or "").strip()
            s = (desc + " · " + cmd) if desc and cmd else (desc or cmd)
            return short(s or title, 120)
        if tool in _FILE_TOOLS:
            fp = str(inp.get("filePath") or inp.get("path") or
                     inp.get("filename") or "").strip()
            if fp:
                return "%s %s" % (tool, os.path.basename(fp))
        if tool in ("grep", "glob", "search", "list", "ls"):
            pat = str(inp.get("pattern") or inp.get("query") or
                      inp.get("path") or "").strip()
            if pat:
                return "%s %s" % (tool, short(pat, 60))
        if tool == "task":
            d = str(inp.get("description") or inp.get("prompt") or "").strip()
            return short("子任务 · " + d if d else title, 120)
        if tool == "todowrite":
            return title or "更新计划"
        if tool == "compaction":
            return title or "压缩上下文"
        return short(title or tool, 120)

    @staticmethod
    def _line_ts(line, now):
        """从行首 2026-10-28T10:52:00.067 取时间。"""
        if len(line) > 23 and line[4] == "-" and line[10] == "T":
            try:
                t = time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S")
                base = time.mktime(t)
                frac = 0.0
                if line[19:20] == ".":
                    frac = float("0" + line[19:23])
                return base + frac
            except (ValueError, OverflowError):
                pass
        return now
