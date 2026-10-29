# -*- coding: utf-8 -*-
"""浏览器壳类应用适配器（B/C 级）。

豆包、百度搭子(DuMate)、ChatGPT 这类是 Electron/Chromium 壳，
对话内容主要落在：
  1) <用户数据>/User Data/<Profile>/Local Storage/leveldb/*.log  （Chromium 存储）
  2) 应用自己的会话目录（例如 ~/.doubao/chats）
  3) 应用自己的库/日志（走 StructuredAdapter 的自动识表）
再加上窗口标题（真实、几乎零开销）作为“当前在做什么”的佐证。

说明：这一级读到的文本无法 100% 区分「用户说的」还是「AI 答的」，
因此统一标记为 chat 文本，并在文档里如实标注精度等级。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import time
from ctypes import wintypes
from datetime import datetime, timedelta
from itertools import islice

from adapters.base import Adapter, TailReader, extract_strings
from adapters.structured import StructuredAdapter
from core.model import (K_ANSWER, K_ERROR, K_RESULT, K_STATUS,
                        K_TOOL, K_USER, P_IDLE, P_THINKING, short)

# ---------------------------------------------------------------- 原始字节尾读
class RawTail:
    """二进制文件的增量读（leveldb 的 .log 是追加写的）。"""

    def __init__(self, path, skip_to_end=True):
        self.path = path
        self.off = 0
        self._sig = None
        self._anchor = b""
        self.generation = 0
        try:
            st = os.stat(path)
            self.off = st.st_size if skip_to_end else 0
            self._sig = self._signature(st)
            self._set_anchor()
        except OSError:
            pass

    @staticmethod
    def _signature(st):
        birth = getattr(st, "st_birthtime_ns", 0) if os.name == "nt" else st.st_ctime_ns
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, birth)

    def _set_anchor(self):
        with open(self.path, "rb") as f:
            f.seek(max(0, self.off - 128))
            self._anchor = f.read(min(128, self.off))

    def rewind(self, back_bytes=60000):
        try:
            self.off = max(0, os.path.getsize(self.path) - back_bytes)
            self._set_anchor()
        except OSError:
            return

    def read(self, maxb=120000):
        try:
            st = os.stat(self.path)
            sig = self._signature(st)
            if sig == self._sig and st.st_size == self.off:
                return b""
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                sig = self._signature(st)
                reset = (self._sig is None or st.st_size < self.off
                         or sig[:2] != self._sig[:2] or st.st_size < self._sig[2])
                if not reset and self.off and self._anchor and sig != self._sig:
                    f.seek(max(0, self.off - len(self._anchor)))
                    reset = f.read(len(self._anchor)) != self._anchor
                if reset:
                    self.off = 0
                    self.generation += 1
                f.seek(self.off)
                data = f.read(min(max(0, st.st_size - self.off), maxb))
                self.off += len(data)
                f.seek(max(0, self.off - 128))
                self._anchor = f.read(min(128, self.off))
            self._sig = sig
            return data
        except OSError:
            self._sig = None
            return b""


# ---------------------------------------------------------------- 窗口标题
_u32 = ctypes.WinDLL("user32")
_u32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_u32.EnumWindows.restype = wintypes.BOOL
_u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_u32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.IsWindowVisible.argtypes = [wintypes.HWND]

_WND_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def top_windows(pids, limit=6, with_ids=False):
    """取指定进程的可见顶层窗口标题。ctypes 直调，开销可忽略。"""
    if not pids:
        return []
    want = set(pids)
    got = []

    def cb(hwnd, _):
        try:
            if not _u32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in want:
                return True
            n = _u32.GetWindowTextLengthW(hwnd)
            if n <= 0 or n > 512:
                return True
            buf = ctypes.create_unicode_buffer(n + 2)
            _u32.GetWindowTextW(hwnd, buf, n + 2)
            t = buf.value.strip()
            item = (int(hwnd), t) if with_ids else t
            if t and item not in got:
                got.append(item)
        except Exception:
            pass
        return len(got) < limit

    try:
        _u32.EnumWindows(_WND_ENUM_PROC(cb), 0)
    except Exception:
        pass
    return got


# ---------------------------------------------------------------- 主体
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORDS = re.compile(r"[A-Za-z]{2,}")
_NOISE = ("http://", "https://", "<?xml", "function(", "var ", ".css", ".png",
          ".woff", "AAAA", "data:image", "Mozilla/", "META:", "chrome://",
          "file:///", "base64")
# 一长串没有空格、大小写数字混杂的，基本是 token / base64，不是人话
_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{28,}")


class ChromiumChatAdapter(StructuredAdapter):
    """StructuredAdapter 之上，再加 Chromium 存储与窗口标题。"""

    key = "?"
    tier = "B"
    LEVELDB_SUBDIRS = ("User Data", "session", "")
    USE_WINDOWS = True

    def __init__(self, info, log=None):
        StructuredAdapter.__init__(self, info, log)
        self.raws = []
        self.seen = set()
        self.seen_order = []
        self.title = ""
        self._title_at = 0
        self._window_titles = {}
        self._raw_at = 0.0
        self._raw_known = {}
        self._raw_cache = {}

    def setup(self):
        StructuredAdapter.setup(self)
        self._discover_raws(first=not self._raw_known)
        if not self.enabled:
            self.enabled = bool(self.raws) or self.USE_WINDOWS
        if self.enabled:
            srcs = [os.path.basename(t.path) for t in self.raws[:2]]
            if self.probes or self.tails:
                srcs = [os.path.basename(p) for p in
                        ([x.db.path for x in self.probes] +
                         [t.path for t in self.tails])[:2]] + srcs
            self.source = ",".join(srcs[:3]) or "window"
            self.log("[%s] 库/日志=%d 存储=%d" % (self.key, len(self.tails), len(self.raws)))
        return self.enabled

    def _discover_raws(self, first=False):
        self._raw_at = time.monotonic() + 5.0
        cands = []
        for r in self.roots:
            if not r or not os.path.isdir(r):
                continue
            for cur, dirs, files in os.walk(r):
                low = cur.lower()
                if ("local storage" in low and low.rstrip("\\").endswith("leveldb")) \
                        or ("session storage" in low) \
                        or low.rstrip("\\").endswith("leveldb"):
                    for fn in files:
                        if fn.lower().endswith(".log"):
                            p = os.path.join(cur, fn)
                            try:
                                cands.append((os.path.getmtime(p), p))
                            except OSError:
                                pass
                if len(cands) > 40:
                    break
                # 限制遍历深度，避免把整个 User Data 翻一遍
                if cur.count(os.sep) - r.count(os.sep) > 5:
                    dirs[:] = []
        cands.sort(reverse=True)
        if first:
            self._raw_known.update((p, True) for _, p in cands)
        self.raws = []
        for _, p in cands[:2]:
            rd = self._raw_cache.pop(p, None)
            if rd is None:
                rd = RawTail(p, skip_to_end=p in self._raw_known)
            self._raw_cache[p] = rd
            self._raw_known[p] = True
            self.raws.append(rd)
        while len(self._raw_cache) > 16:
            self._raw_cache.pop(next(iter(self._raw_cache)))
        while len(self._raw_known) > 128:
            self._raw_known.pop(next(iter(self._raw_known)))

    def tick(self, now):
        if time.monotonic() >= self._raw_at:
            self._discover_raws()
        out = StructuredAdapter.tick(self, now)
        for rt in self.raws:
            data = rt.read()
            if not data:
                continue
            for s in extract_strings(data, min_len=10, limit=30):
                if self._bad(s):
                    continue
                h = hashlib.md5(s.encode("utf-8", "ignore")).hexdigest()[:12]
                if h in self.seen:
                    continue
                self._remember(h)
                st = self.emit(K_ANSWER, s, now)
                self._note(K_ANSWER, now)
                out.append(st)
        if self.USE_WINDOWS and now - self._title_at > 5:
            self._title_at = now
            current = {}
            for item in self._win_titles():
                wid, t = item if isinstance(item, tuple) else (item, item)
                if t:
                    current[wid] = t
                    if t != self._window_titles.get(wid):
                        self.title = t
                        out.append(self.emit(K_STATUS, "窗口: " + t, now,
                                             evidence="window_title", window=str(wid)))
            self._window_titles = current
        return self._take(out)

    def replay(self, n=25):
        StructuredAdapter.replay(self, n)
        for rt in self.raws:
            try:
                rt.rewind(60000)
            except Exception:
                pass

    def emit(self, kind, text, now, session="", turn="", seq=0, **meta):
        if meta.get("evidence") not in ("codex_log", "codex_rollout"):
            # 壳层文本和标题不能证明角色、工具调用或整轮完成。
            kind = K_STATUS
            meta.setdefault("evidence", "unverified_storage")
            meta["phase"] = P_IDLE
            self.phase = P_IDLE
        if "phase" in meta:
            self.phase = meta["phase"]
        return Adapter.emit(self, kind, text, now, session, turn, seq, **meta)

    def _note(self, kind, now):
        pass

    def _remember(self, h):
        self.seen.add(h)
        self.seen_order.append(h)
        if len(self.seen_order) > 400:      # 有界，防止内存无限长
            for old in self.seen_order[:200]:
                self.seen.discard(old)
            self.seen_order = self.seen_order[200:]

    @staticmethod
    def _bad(s):
        """只保留“像人说的话或真实内容”的字符串，挡掉存储里的键名和配置。"""
        if len(s) > 400 or len(s) < 6:
            return True
        if s[0] in "{[\"'" or s.endswith((",", ":", "{")):
            return True
        for n in _NOISE:
            if n in s:
                return True
        if s.count("_") >= 3 or s.count(":") >= 3:
            return True
        if re.match(r"^[A-Za-z0-9_\-\.]+$", s):     # 纯键名，如 desktop_update_prompt
            return True
        if _TOKEN.search(s):                        # token / base64
            return True
        if len(s.split()) <= 1 and len(s) > 30:     # 一整坨没空格
            return True
        # 中文成句 / 英文四个词以上，才算内容
        if not (_CJK.search(s) or len(_WORDS.findall(s)) >= 4):
            return True
        return False

    def _win_titles(self):
        try:
            from core import discover
            pids = []
            for exe in self.info.get("exes", []):
                pids.extend(discover.process_pids_by_name(exe))
            return top_windows(pids, with_ids=True)
        except Exception:
            return []

    def status(self, now=None):
        st = StructuredAdapter.status(self, now)
        if not st.title:
            st.title = short(self.title or self.zh, 60)
        return st


# ================================================================
# 四款壳类应用的具体探针
# ================================================================
class DoubaoAdapter(ChromiumChatAdapter):
    key = "doubao"
    zh = "豆包"
    en = "Doubao"
    tier = "B"
    DB_SUBDIRS = ("chats", "data", "")
    LOG_SUBDIRS = ("chats", "logs", "")


class BaiduDaziAdapter(ChromiumChatAdapter):
    key = "baidu_dazi"
    zh = "百度搭子"
    en = "Baidu Dazi"
    tier = "C"
    DB_SUBDIRS = ("User Data", "data", "")
    LOG_SUBDIRS = ("logs", "log", "")


# ================================================================
# Codex 专用解析（仅显式选用；不代表普通 ChatGPT 对话）
# ================================================================
# sessions/YYYY/MM/DD/rollout-*.jsonl 是会话/轮次的结构化证据。
# logs_N.sqlite 的 tracing span 只作为有限精度的状态佐证；RPC 名称、
# 日志级别或模块名均不能证明工具执行、用户输入或整轮终态。
_CODEX_BAD = ("login", "auth", "remote_control", "websocket", "message_processor",
              "http_client", "custom_ca", "outgoing_message", "feedback_tags",
              "transport", "update", "experiment")
_CODEX_GOOD = ("codex_core::session", "codex_core::tools", "codex_core::exec",
               "codex_core::rollout", "codex_core::task", "codex_core::client",
               "codex_core_plugins", "codex_mcp", "codex_code_mode",
               "codex_app_server_protocol")
# 这些是 UI 启动/轮询时刷出来的 RPC，不是用户任务步骤
_CODEX_LOWVALUE = ("thread/list", "account/read", "config/", "skills/list",
                   "hooks/list", "plugins_for_config", "experimentalFeature",
                   "externalAgentConfig", "config/batchWrite", "thread/archive",
                   "thread/name/set", "op.dispatch.shutdown")


def codex_brief(body, target=""):
    """把一条 rust tracing span 压成人能看的“在干嘛”。

    返回 (摘要, 操作名, 是否硬凑)：只展示日志体里的 otel.name / codex.op /
    rpc.method；从模块名硬凑出来的一律丢弃。摘要不证明执行或终态。
    """
    b = str(body or "")
    name = ""
    m = re.search(r'otel\.name="([^"]+)"', b)
    if m:
        name = m.group(1)
    if not name:
        m = re.search(r'codex\.op="([^"]+)"', b)
        if m:
            name = "turn/" + m.group(1)
    if not name:
        m = re.search(r'rpc\.method="([^"]+)"', b)
        if m:
            name = m.group(1)
    if not name:
        return "", "", True
    tid = ""
    m = re.search(r'thread_id=([0-9a-fA-F-]{8})', b)
    if m:
        tid = m.group(1)
    return (name + (" #" + tid if tid else "")), name, False


def codex_kind(name, level):
    # A span/RPC name (even tool/exec or ERROR) proves neither execution nor
    # a user turn's outcome. Structured rollout records are authoritative.
    return K_STATUS


def _codex_id(value):
    return value if isinstance(value, str) and 0 < len(value) <= 200 else ""


def _codex_time(value):
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0.0


class CodexRolloutReader:
    """Bounded JSONL tail, with session/turn state independent of other files.

    Verified desktop variant: session_meta.id, task_started.turn_id,
    item_completed.item.type=UserMessage, turn_aborted.reason=interrupted.
    Older event_msg user_message/agent_message and explicit terminal records
    are supported too. response_item messages may be injected context, so only
    actual call variants are used there; an answer is never a terminal.
    """

    MAX_BYTES = 256 << 10
    MAX_TURNS = 128
    CALLS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}

    def __init__(self, path, start_at_end=True, since=0.0):
        self.path = path
        self.since = since
        self.session = self.turn = ""
        self.states = {}
        self.tail = TailReader(path, start_at_end=start_at_end,
                               max_step=self.MAX_BYTES, max_line=self.MAX_BYTES)
        self._header()
        if start_at_end:
            # Recover IDs/terminal fences only up to the mounted cursor. Reading
            # beyond it could silently consume a concurrently appended event.
            try:
                end = self.tail.off
                offset = max(0, end - self.MAX_BYTES)
                with open(path, "rb") as f:
                    f.seek(max(0, offset - 1))
                    partial = bool(offset and f.read(1) != b"\n")
                    data = f.read(end - offset)
                lines = data.split(b"\n")[:-1]
                for line in lines[1 if partial else 0:]:
                    self._parse(line.decode("utf-8", "ignore"), emit=False)
            except OSError:
                pass

    def _header(self):
        try:
            with open(self.path, "rb") as f:
                line = f.readline(self.MAX_BYTES + 1)
            if len(line) <= self.MAX_BYTES:
                record = json.loads(line)
                if isinstance(record, dict) and record.get("type") == "session_meta":
                    payload = record.get("payload")
                    if isinstance(payload, dict):
                        self.session = _codex_id(payload.get("id"))
        except (OSError, ValueError, UnicodeError):
            pass

    def _state(self, turn):
        key = (self.session, turn)
        if key not in self.states:
            self.states[key] = {"terminal": "", "seen": set()}
        while len(self.states) > self.MAX_TURNS:
            self.states.pop(next(iter(self.states)))
        return self.states[key]

    def _parse(self, line, emit=True):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return []
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            return []
        payload = record["payload"]
        typ, sub = record.get("type"), payload.get("type")
        if typ == "session_meta":
            sid = _codex_id(payload.get("id"))
            if sid != self.session:
                self.session, self.turn = sid, ""
            return []
        if not self.session:
            return []
        sid = _codex_id(payload.get("thread_id"))
        if sid and sid != self.session:
            return []
        tid = _codex_id(payload.get("turn_id"))
        if typ == "turn_context" or (typ == "event_msg" and sub == "task_started"):
            self.turn = tid
        turn = tid or self.turn
        if not turn:
            return []  # Do not invent a turn or attach it to another session.
        state = self._state(turn)
        if typ == "turn_context":
            return []
        kind, text, terminal, phase = None, "", "", None
        dedup = None
        if typ == "event_msg":
            if sub == "task_started":
                kind, text, phase = K_STATUS, "Codex turn started (terminal not yet observed)", P_THINKING
                dedup = "started"
            elif sub == "user_message":
                kind, text, dedup = K_USER, payload.get("message", ""), "user"
            elif sub == "item_completed" and isinstance(payload.get("item"), dict):
                item = payload["item"]
                if item.get("type") == "UserMessage":
                    content = item.get("content", [])
                    text = " ".join(x.get("text", "") for x in content
                                    if isinstance(x, dict) and isinstance(x.get("text"), str)) \
                        if isinstance(content, list) else ""
                    kind, dedup = K_USER, "user"
            elif sub == "agent_message":
                kind, text = K_ANSWER, payload.get("message", "")
            elif sub == "task_complete":
                kind, text, terminal = K_RESULT, "Codex turn completed", "completed"
            elif sub == "turn_aborted":
                kind, text, terminal, phase = K_STATUS, "Codex turn cancelled", "cancelled", P_IDLE
                reason = payload.get("reason")
                if isinstance(reason, str):
                    text += ": " + short(reason, 80)
            elif sub == "error":
                kind = K_ERROR
                message = payload.get("message") or payload.get("error")
                text = message if isinstance(message, str) else "Codex reported an error"
                terminal = "" if payload.get("will_retry") is True else "error"
        elif typ == "response_item" and isinstance(sub, str) and sub in self.CALLS:
            kind = K_TOOL
            text = "Codex tool call: " + short(payload.get("name") or sub, 100)
            # Arguments/output can contain secrets; never include them in tracing.
            cid = _codex_id(payload.get("call_id") or payload.get("id"))
            dedup = "call:" + (cid or hashlib.sha256(line.encode("utf-8")).hexdigest())
        if kind is None:
            return []
        previous = state["terminal"]
        if previous and (not terminal or terminal == previous or terminal == "completed"):
            return []
        if terminal:
            state["terminal"] = terminal
            if self.turn == turn:
                self.turn = ""
        if dedup:
            if dedup in state["seen"]:
                return []
            state["seen"].add(dedup)
            # Keep lifecycle/user keys; bounded call IDs are enough for a tail.
            if len(state["seen"]) > 512:
                state["seen"] = {dedup, "started", "user"}
        ts = _codex_time(record.get("timestamp"))
        if not emit or (self.since and ts < self.since):
            return []
        meta = {"session": self.session, "turn": turn, "record_type": sub,
                "terminal": bool(terminal), "accuracy": "structured"}
        if terminal:
            meta["outcome"] = terminal
        if phase:
            meta["phase"] = phase
        return [(kind, short(text, 200), ts, meta)]

    def poll(self):
        # Match TailReader's replacement/anchor checks so a rewritten file can
        # never inherit another session's attribution, even at equal size.
        sig = self.tail._signature()
        old = self.tail._sig
        reset = old is None or (sig and (sig[:2] != old[:2] or sig[2] < old[2]))
        if sig and old and sig != old and self.tail._anchor and not reset:
            try:
                with open(self.path, "rb") as f:
                    f.seek(max(0, self.tail.off - len(self.tail._anchor)))
                    reset = f.read(len(self.tail._anchor)) != self.tail._anchor
            except OSError:
                return []
            if not reset and sig[2] == old[2]:
                # Equal-size replacement may change only the session header,
                # leaving TailReader's end anchor byte-for-byte identical.
                previous_session, self.session = self.session, ""
                self._header()
                reset = previous_session != self.session
        if reset:
            self.session = self.turn = ""
            self.states.clear()
            self.tail._reset()
            self.tail._sig = None
            self._header()
        lines = self.tail.read_lines()
        out = []
        for line in lines:
            out.extend(self._parse(line))
        return out

    def covers(self, session, turn=""):
        return bool(session and self.session == session and
                    (not turn or (session, turn) in self.states))


class CodexRollouts:
    """Only recent dated rollout directories; bounded discovery and tail cache."""

    MAX_FILES, MAX_ENTRIES, DAYS = 8, 512, 3

    def __init__(self, roots):
        self.roots = list(dict.fromkeys(roots))[:4]
        self.started = time.time()
        self.readers = {}
        self.known = {}
        self.active = []
        self.next_scan = 0.0
        self.discover(first=True)

    def discover(self, first=False):
        self.next_scan = time.monotonic() + 5.0
        remaining = self.MAX_ENTRIES
        found = []
        for day in range(self.DAYS):
            date = datetime.fromtimestamp(time.time()) - timedelta(days=day)
            for root in self.roots:
                directory = os.path.join(root, "sessions", date.strftime("%Y"),
                                         date.strftime("%m"), date.strftime("%d"))
                if remaining <= 0:
                    break
                try:
                    with os.scandir(directory) as entries:
                        for entry in islice(entries, remaining):
                            remaining -= 1
                            if (not entry.name.startswith("rollout-") or
                                    not entry.name.endswith(".jsonl") or
                                    not entry.is_file(follow_symlinks=False)):
                                continue
                            st = entry.stat(follow_symlinks=False)
                            found.append((st.st_mtime, entry.path))
                except OSError:
                    continue
        self.active = []
        for modified, path in sorted(found, reverse=True)[:self.MAX_FILES]:
            reader = self.readers.pop(path, None)
            if reader is None:
                reader = CodexRolloutReader(
                    path, start_at_end=first or path in self.known or modified < self.started,
                    since=self.started)
            self.readers[path] = reader
            self.known[path] = True
            self.active.append(reader)
        while len(self.readers) > self.MAX_FILES * 2:
            self.readers.pop(next(iter(self.readers)))
        while len(self.known) > 128:
            self.known.pop(next(iter(self.known)))

    def poll(self):
        if time.monotonic() >= self.next_scan:
            self.discover()
        return [event for reader in self.active for event in reader.poll()]

    def covers(self, session, turn=""):
        return any(r.covers(session, turn) for r in self.readers.values())


class CodexLogReader:
    """Read-only incremental tracing fallback; never authoritative turn events."""

    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda *a: None)
        self.con = None
        self.last_id = 0
        self.cols = []
        self.ok = False
        self._identity = None
        self._anchor = None

    def _file_identity(self):
        st = os.stat(self.path)
        return (st.st_dev, st.st_ino, st.st_ctime_ns if os.name == "nt" else 0)

    @staticmethod
    def _fingerprint(row):
        return hashlib.sha256(repr(tuple(row)).encode("utf-8")).digest() if row else None

    def _cursor_row(self):
        return self.con.execute(
            "select id,ts,level,target,feedback_log_body from logs where id=?",
            (self.last_id,)).fetchone()

    def open(self, start_at_end=True):
        import sqlite3
        self.close()
        try:
            identity = self._file_identity()
            self.con = sqlite3.connect(
                "file:" + self.path.replace("\\", "/") + "?mode=ro", uri=True,
                timeout=0.1)
            self.con.execute("PRAGMA query_only=1")
            self.cols = [r[1] for r in self.con.execute("PRAGMA table_info(logs)")]
            need = {"id", "ts", "level", "target", "feedback_log_body"}
            if not need.issubset(set(self.cols)):
                self.close()
                return False
            row = self.con.execute("select max(id) from logs").fetchone()
            self.last_id = int(row[0] or 0) if start_at_end else 0
            self._anchor = self._fingerprint(self._cursor_row())
            self._identity = identity
            self.ok = True
            return True
        except Exception:
            self.close()
            return False

    def rewind(self, back=400):
        try:
            row = self.con.execute(
                "select id from logs order by id desc limit 1 offset ?", (back,)).fetchone()
            self.last_id = int(row[0]) if row else 0
            self._anchor = self._fingerprint(self._cursor_row())
        except Exception:
            pass

    def poll(self, limit=200):
        try:
            identity = self._file_identity()
            if not self.con or identity != self._identity:
                cursor, anchor, previous = self.last_id, self._anchor, self._identity
                if not self.open(start_at_end=False):
                    return []
                if identity == previous:
                    self.last_id, self._anchor = cursor, anchor
            maximum = int(self.con.execute("select max(id) from logs").fetchone()[0] or 0)
            anchor = self._fingerprint(self._cursor_row())
            if maximum < self.last_id or (anchor is not None and self._anchor is not None
                                          and anchor != self._anchor):
                self.last_id, self._anchor = 0, None
            rows = self.con.execute(
                "select id,ts,level,target,feedback_log_body from logs "
                "where id>? order by id limit ?", (self.last_id, limit)).fetchall()
        except Exception:
            self.close()
            return []
        out = []
        for rid, ts, level, target, body in rows:
            self._anchor = self._fingerprint((rid, ts, level, target, body))
            try:
                self.last_id = max(self.last_id, int(rid))
            except Exception:
                pass
            tg = str(target or "")
            low = tg.lower()
            if any(b in low for b in _CODEX_BAD):
                continue
            if not any(g in low for g in _CODEX_GOOD):
                continue
            brief, name, derived = codex_brief(body, tg)
            if not brief or derived:
                continue
            nlow = name.lower()
            if any(v in nlow for v in _CODEX_LOWVALUE):
                continue
            try:
                when = float(ts) if ts and float(ts) > 1e6 else 0.0
            except Exception:
                when = 0.0
            ids = {}
            for field, key in (("thread_id", "session"), ("turn_id", "turn")):
                match = re.search(r'\b' + field + r'=[\"\']?([\w-]{1,200})', str(body or ""))
                if match:
                    ids[key] = match.group(1)
            out.append((codex_kind(name, level), brief, when,
                        {"target": tg, "level": level, "accuracy": "limited",
                         "terminal": False, "phase": P_IDLE, **ids}))
        return out

    def close(self):
        try:
            if self.con:
                self.con.close()
        except Exception:
            pass
        self.con = None
        self.ok = False


class ChatGPTAdapter(ChromiumChatAdapter):
    key = "chatgpt"
    zh = "ChatGPT"
    en = "ChatGPT"
    tier = "B"
    DB_SUBDIRS = ("User Data", "data", "")
    LOG_SUBDIRS = ("logs", "log", "")

    def __init__(self, info, log=None):
        ChromiumChatAdapter.__init__(self, info, log)
        self._codex_roots = list(self.roots)
        self.roots = [r for r in self.roots if not any(
            part in (".codex", "codex") or part.startswith("openai.codex")
            for part in r.replace("\\", "/").lower().split("/"))]
        self.info = dict(self.info)
        if not self.info.get("codex_logs"):
            self.info["exes"] = [exe for exe in self.info.get("exes", [])
                                 if exe.lower() != "codex.exe"]
        self.codex = None
        self.rollouts = None
        self._codex_last = {}

    def setup(self):
        ChromiumChatAdapter.setup(self)
        self.tier = "B" if self.raws or self.probes or self.tails else "C"
        # Codex 日志不是普通 ChatGPT 对话，必须显式选用且独立标注来源。
        if self.info.get("codex_logs"):
            self._setup_codex()
            if self.rollouts is None:
                self.rollouts = CodexRollouts(self._codex_roots)
        if (self.codex and self.codex.ok) or (self.rollouts and self.rollouts.active):
            self.enabled = True
            self._codex_source()
        return self.enabled

    def _codex_source(self):
        if self.rollouts and self.rollouts.active:
            self.source = "Codex rollout (explicit terminals only; not ChatGPT)"
        else:
            self.source = "Codex tracing (limited: no authoritative terminal; not ChatGPT)"

    def _setup_codex(self):
        """~/.codex/logs_N.sqlite：Codex 引擎的真实 tracing 日志。"""
        import glob
        cands = []
        for r in self._codex_roots:
            if not r or not os.path.isdir(r):
                continue
            cands.extend(glob.glob(os.path.join(r, "logs_*.sqlite")))
        ranked = []
        for p in set(cands):
            try:
                ranked.append((os.path.getmtime(p), p))
            except OSError:
                continue
        for _, p in sorted(ranked, reverse=True)[:3]:
            rd = CodexLogReader(p, self.log)
            if rd.open():
                if self.codex:
                    self.codex.close()
                self.codex = rd
                for probe in self.probes:
                    if os.path.abspath(probe.db.path) == os.path.abspath(p):
                        probe.db.close()
                self.probes = [x for x in self.probes
                               if os.path.abspath(x.db.path) != os.path.abspath(p)]
                self.log("[chatgpt] Codex 引擎日志: %s" % os.path.basename(p))
                return True
        return False

    def tick(self, now):
        out = ChromiumChatAdapter.tick(self, now)
        clock = time.monotonic()
        for key, last in list(self._codex_last.items()):
            if clock - last >= 20.0:
                self._codex_last.pop(key, None)
        if self.rollouts:
            for kind, brief, ts, meta in self.rollouts.poll():
                self.cur_session, self.cur_turn = meta["session"], meta["turn"]
                out.append(self.emit(kind, brief, ts or now, evidence="codex_rollout",
                                     product="Codex", **meta))
            self._codex_source()
        if self.codex:
            for kind, brief, ts, meta in self.codex.poll():
                session, turn = meta.get("session", ""), meta.get("turn", "")
                if self.rollouts and (self.rollouts.covers(session, turn) or
                                      (not session and self.rollouts.active)):
                    continue
                when = ts or now
                key = (session, turn, brief)
                if key in self._codex_last:
                    continue
                self._codex_last[key] = clock
                while len(self._codex_last) > 512:
                    self._codex_last.pop(next(iter(self._codex_last)))
                out.append(self.emit(kind, brief, when, evidence="codex_log",
                                     product="Codex", **meta))
        return self._take(out)

    def replay(self, n=25):
        # Rollouts are never rewound across private sessions by generic replay.
        # tools/test_codex.py offers a bounded, explicit known-marker replay.
        ChromiumChatAdapter.replay(self, n)
        if self.codex and self.codex.ok and not (self.rollouts and self.rollouts.active):
            self.codex.rewind(n * 8)
