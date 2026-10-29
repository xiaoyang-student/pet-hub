# -*- coding: utf-8 -*-
"""适配器基类 + 几个通用的“只读数据源”工具。

稳定性约定（很重要）：
  * 任何读取都可能因为目标软件正在写而失败，一律吞掉异常、下次再试；
  * 不写入、不删除被监控软件的数据；SQLite 读取可能短暂持有读锁；
  * SQLite 使用 mode=ro；WAL 读取仍可能创建或维护 -wal/-shm sidecar。
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from core.model import (K_ANSWER, K_ASK, K_DENIED, K_EDIT, K_ERROR, K_RESULT,
                        K_STALLED, K_STATUS, K_THINK, K_TOOL, K_TOOL_DONE,
                        K_USER, P_ANSWERING, P_ASKING, P_DENIED, P_DONE,
                        P_EDITING, P_ERROR, P_IDLE, P_RECEIVED, P_STALLED,
                        P_THINKING, P_TOOL, PHASE_OF_KIND,
                        Status, Step, first_line, short)

# ---------------------------------------------------------------- 扫描
SKIP_DIRS = {"cache", "Cache", "Code Cache", "GPUCache", "Crashpad", "logs",
             "CachedData", "CachedExtensions", "blob_storage", "DawnGraphiteCache",
             "DawnWebGPUCache", "Shared Dictionary", "SharedStorage", "dictionaries",
             "CachedConfigurations", "CachedExtensionVSIXs", "CachedProfilesData",
             "backups", "crash", "certs", "dumps"}


# 明显是配置/缓存，不是会话内容，一律排除
BAD_NAMES = ("config", "cache", "prefs", "record", "credential", "setting",
             "umid", "opencode", "package", "lock", "manifest", "state",
             "migration", "identity", "dips", "cookie", "history-index",
             "shortcut", "builtin", "catalog", "license", "server", "content",
             "infra", "environment", "css", "filter", "adult", "router", "sdk",
             "prompt", "keywords", "dict", "locale", "themes",
             "first_party_sets", "heavy_ad_intervention", "trust_layer", "gecko2")


def name_score(fn):
    """文件名打分：越像“会话记录”分越高；配置文件直接淘汰。"""
    n = fn.lower()
    for b in BAD_NAMES:
        if b in n:
            return -100
    s = 0
    for k in ("session", "message", "chat", "conversation", "agent", "turn",
              "trajectory", "history", "trace", "task", "event", "log"):
        if k in n:
            s += 4
    for k in ("index", "tmp", "temp", "old", "backup"):
        if k in n:
            s -= 3
    return s


def scan_files(roots, exts=(".sqlite", ".db", ".jsonl", ".json", ".log"),
               max_depth=4, max_files=60, skip_index=True, min_score=None,
               min_size=256):
    """在若干根目录里找候选数据文件。

    排序规则 = 先按「文件名像不像会话记录」打分，再按修改时间，
    这样就不会把 public_config.json / opencode.json 这类配置文件当成会话。
    """
    found = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(root):
            depth = cur.count(os.sep) - base_depth
            if depth >= max_depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fn in files:
                if skip_index and fn.endswith(("-wal", "-shm", "-journal")):
                    continue
                if not fn.lower().endswith(tuple(exts)):
                    continue
                sc = name_score(fn)
                if min_score is not None and sc < min_score:
                    continue
                if sc <= -100:
                    continue
                p = os.path.join(cur, fn)
                key = os.path.normcase(os.path.abspath(p))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if min_size and st.st_size <= min_size:
                    continue
                found.append((sc, st.st_mtime, st.st_size, p))
            if len(found) > 600:
                break
    found.sort(reverse=True)
    return [f[3] for f in found[:max_files]]


def newest(roots, exts=(".sqlite", ".db", ".jsonl"), **kw):
    fs = scan_files(roots, exts, **kw)
    return fs[0] if fs else ""


# ---------------------------------------------------------------- 文件尾读
class TailReader:
    """有界尾读；超长行整行丢弃，部分行只在换行后交付。"""

    def __init__(self, path, encoding="utf-8", errors="ignore", max_step=1 << 20,
                 start_at_end=True, max_line=256 << 10, max_lines=256):
        self.path = path
        self.enc = encoding
        self.errors = errors
        self.max_step = max(1, min(int(max_step), 1 << 20))
        self.max_line = max(1, min(int(max_line), 1 << 20))
        self.max_lines = max(1, min(int(max_lines), 4096))
        self.off = 0
        self.buf = self._pending = self._anchor = b""
        self._discarding = False
        self._sig = None
        if start_at_end:
            self.open_new()

    @staticmethod
    def _stat_sig(st):
        # Windows stat/fstat can disagree on deprecated ctime semantics.
        stamp = getattr(st, "st_birthtime_ns", 0) if os.name == "nt" else st.st_ctime_ns
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, stamp)

    def _reset(self):
        self.off = 0
        self.buf = self._pending = self._anchor = b""
        self._discarding = False

    def _remember(self, f):
        f.seek(max(0, self.off - 64))
        self._anchor = f.read(min(64, self.off))

    def open_new(self):
        """首次挂载跳过历史；文件轮换不调用此方法。"""
        self._reset()
        try:
            with io.open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                self.off = st.st_size
                self._remember(f)
                self._discarding = bool(self._anchor and not self._anchor.endswith(b"\n"))
                self._sig = self._stat_sig(st)
        except OSError:
            self._sig = None

    def _signature(self):
        try:
            return self._stat_sig(os.stat(self.path))
        except OSError:
            return None

    def closed(self):
        return not os.path.exists(self.path)

    def rewind(self, back_bytes=24000):
        """回放最近字节；截在半行时丢弃该行。"""
        try:
            with io.open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                self._reset()
                self.off = max(0, st.st_size - max(0, back_bytes))
                self._remember(f)
                self._discarding = bool(self._anchor and not self._anchor.endswith(b"\n"))
                self._sig = self._stat_sig(st)
        except OSError:
            return

    def read_lines(self):
        sig = self._signature()
        if sig is None:
            self._sig = None
            return []
        if sig != self._sig or self.off < sig[2]:
            try:
                with io.open(self.path, "rb") as f:
                    sig = self._stat_sig(os.fstat(f.fileno()))
                    reset = (self._sig is None or sig[:2] != self._sig[:2]
                             or sig[2] < self._sig[2] or sig[2] < self.off)
                    if not reset and self._anchor and sig != self._sig:
                        f.seek(self.off - len(self._anchor))
                        reset = f.read(len(self._anchor)) != self._anchor
                    if reset:
                        self._reset()
                    self._sig = sig
                    if not self._pending:
                        f.seek(self.off)
                        data = f.read(min(self.max_step, max(0, sig[2] - self.off)))
                        self.off += len(data)
                        self._pending = data
                        self._remember(f)
            except OSError:
                return []
        data, self._pending = self._pending, b""
        out, start, lines = [], 0, 0
        while start < len(data) and lines < self.max_lines:
            end = data.find(b"\n", start)
            stop = len(data) if end < 0 else end
            length = len(self.buf) + stop - start
            if not self._discarding and length <= self.max_line:
                self.buf += data[start:stop]
            else:
                self.buf = b""
                self._discarding = True
            if end < 0:
                return out
            if not self._discarding and self.buf.strip():
                try:
                    out.append(self.buf.decode(self.enc, self.errors))
                except (UnicodeError, LookupError):
                    pass
            self.buf = b""
            self._discarding = False
            start = end + 1
            lines += 1
        self._pending = data[start:]
        return out


# ---------------------------------------------------------------- SQLite
class ReadOnlyDB:
    """只读 SQLite。打开失败一律返回 None，绝不抛到调用方。"""

    def __init__(self, path):
        self.path = path
        self.con = None
        self.last_error = None

    def ok(self):
        return self.con is not None

    def open(self):
        if self.con is not None:
            return True
        try:
            uri = Path(os.path.abspath(self.path)).as_uri() + "?mode=ro"
            self.con = sqlite3.connect(uri, uri=True, timeout=0.5,
                                       check_same_thread=False)
            self.con.execute("PRAGMA query_only=1")
            self.last_error = None
            return True
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.last_error = exc
            self.close()
            return False

    def close(self):
        try:
            if self.con:
                self.con.close()
        except Exception:
            pass
        self.con = None

    def q(self, sql, args=()):
        self.last_error = None
        if not self.con and not self.open():
            return []
        try:
            return self.con.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            self.last_error = exc
            self.close()
            return []
        except Exception as exc:
            self.last_error = exc
            return []

    def cols(self, table):
        name = '"' + table.replace('"', '""') + '"'
        return [r[1] for r in self.q("PRAGMA table_info(%s)" % name)]

    def has(self, table):
        try:
            r = self.con.execute(
                "select 1 from sqlite_master where type='table' and name=? limit 1",
                (table,)).fetchone()
            return bool(r)
        except Exception:
            return False


# ---------------------------------------------------------------- 二进制取串
_UTF8_RE = re.compile(rb"[\x20-\x7e\xc0-\xfe][\x20-\x7e\x80-\xfe]{7,}")
# UTF-16LE 的可打印串：低字节必须落在可打印 ASCII 区间，高字节为 0x00。
# 注意不要写成 [\x20-\x7e\x00-\xff] —— \x00-\xff 会把前面的限制整个吞掉，
# 变成"任意字节 + 0x00"，于是从二进制里抽出一堆垃圾当成正文。
_UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")


def extract_strings(data, min_len=8, limit=40):
    """从 Chromium LevelDB / 二进制存储里抽出可读字符串。

    Chromium 的 Local Storage 用 UTF-16LE 存 key/value，
    因此 UTF-8 与 UTF-16LE 两种都要试。
    """
    out = []
    seen = set()
    for m in _UTF16_RE.finditer(data[:400000]):
        try:
            s = m.group().decode("utf-16-le", "ignore")
        except Exception:
            continue
        s = s.strip()
        if len(s) >= min_len and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            return out
    for m in _UTF8_RE.finditer(data[:400000]):
        try:
            s = m.group().decode("utf-8", "ignore")
        except Exception:
            continue
        s = s.strip()
        if len(s) >= min_len and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 基类
class Adapter:
    """所有适配器的共同接口。"""

    key = "?"
    zh = "?"
    en = "?"
    tier = "?"
    # 轮询间隔（秒）：活跃 / 安静 / 无进程
    T_ACTIVE, T_QUIET, T_AWAY = 0.5, 3.0, 20.0

    def __init__(self, info, log=None):
        self.info = info or {}
        self.log = log or (lambda *a: None)
        self.roots = list(self.info.get("roots") or [])
        ip = self.info.get("install_path")
        if ip and os.path.isdir(ip):
            self.roots.append(ip)
        self.enabled = False
        self.source = ""
        self.err = 0
        self.last_event = 0.0
        self.last_beat = 0.0
        self.steps = 0
        self.phase = P_IDLE
        self.detail = ""
        self.title = ""
        self.cur_turn = ""
        # 当前会话 id。会话级 task_id 的适配器（如千问办公）必须维护它，
        # 引擎的收割事件（stalled / app_exit）靠它落到状态栏的同一行。
        self.cur_session = ""

    # -- 生命周期 ------------------------------------------------
    def setup(self):
        """定位数据源。成功返回 True。子类实现。"""
        return False

    def tick(self, now):
        """被轮询调用，返回 Step 列表。子类实现。"""
        return []

    # -- 引擎查询 ------------------------------------------------
    def status(self, now=None):
        return Status(app=self.key, phase=self.phase, title=short(self.title, 60),
                      detail=short(self.detail, 120), progress=-1, steps=self.steps,
                      active=self._alive(), updated=self.last_event or time.time())

    def interval(self, now):
        if not self.enabled:
            return self.T_AWAY * 3
        if not self._alive():
            return self.T_AWAY
        return self.T_ACTIVE if (now - self.last_event) < 6 else self.T_QUIET

    # -- 出错治理：滑动窗口计数 + 冷却后自动恢复 ----------------------
    # 旧实现是"连续 6 次出错 -> enabled=False 且永不恢复"，而 engine.build()
    # 对已存在的 key 直接 continue，于是一次几秒的磁盘抖动 / 目标软件正在
    # 压缩自己的库，就能让这款软件在追踪器的整个生命周期里彻底消失，
    # 且因为壳层用 CreateNoWindow 启动、日志无处可去，用户完全无感。
    ERR_WINDOW = 60.0        # 错误计数窗口（秒）
    ERR_LIMIT = 6            # 窗口内达到这个次数就进入冷却
    COOLDOWN = 120.0         # 冷却时长（秒），到点自动重试

    def on_error(self, e):
        now = time.time()
        self._errs = [t for t in getattr(self, "_errs", [])
                      if now - t < self.ERR_WINDOW]
        self._errs.append(now)
        self.err = len(self._errs)
        if self.err >= self.ERR_LIMIT:
            self.enabled = False
            self._cooldown_until = now + self.COOLDOWN
            n = self.err
            self._errs = []
            self.err = 0
            self.log("[%s] %d 秒内出错 %d 次，冷却 %d 秒后自动重试：%s"
                     % (self.key, int(self.ERR_WINDOW), n,
                        int(self.COOLDOWN), e))
        return self.err

    def reset_err(self):
        self.err = 0
        self._errs = []

    def revive_if_due(self, now):
        """冷却到点自动复活，由 engine 每轮调用。返回是否刚复活。"""
        if self.enabled:
            return False
        until = getattr(self, "_cooldown_until", 0.0)
        if not until or now < until:
            return False
        self._cooldown_until = 0.0
        try:
            ok = self.setup()
        except Exception:
            ok = False
        if ok:
            self.enabled = True
            self.log("[%s] 冷却结束，已重新挂载 (%s)" % (self.key, self.source))
            return True
        # setup 仍失败：再等一个冷却周期，别每轮都撞
        self._cooldown_until = now + self.COOLDOWN
        return False

    # -- 进程活跃 ------------------------------------------------------
    # 走 discover 的全局缓存。旧实现每个适配器各自缓存 10 秒，8 个适配器
    # 就是每 10 秒 8 次全量进程枚举，engine.reap_dead 还要再来一遍，
    # 实测约 16 次 / 10 秒，每次都要对上千个进程 OpenProcess。
    def _alive(self):
        from core import discover
        names = discover.process_names_cached()
        return any(e.lower() in names for e in self.info.get("exes", []))

    # -- 工具 ----------------------------------------------------
    def emit(self, kind, text, now, session="", turn="", seq=0, **meta):
        """产出一个步骤。

        顺手做两件以前要每个适配器自己记得做、且经常漏做的事：
          1. 推进 self.phase（漏调 _note 就会让状态永远停在 idle）；
          2. 把 phase 写进 meta，让 PetHubSink 能把细粒度状态带给状态栏。
        子类仍可用 meta['phase']=... 显式覆盖。
        """
        self.last_event = now
        self.steps += 1
        self.detail = short(text, 120)
        p = self._phase(kind)
        if p:
            self.phase = p
        m = dict(meta or {})
        m.setdefault("phase", self.phase)
        return Step(ts=now, app=self.key, kind=kind, text=short(text, 200),
                    session=session, turn=turn, seq=seq, meta=m)

    def _phase(self, kind):
        """步骤类型 -> 阶段。统一查 model.PHASE_OF_KIND，不再各处各写一份。"""
        return PHASE_OF_KIND.get(kind)

    def _note(self, kind, now):
        """兼容保留：emit() 已自动推进阶段，子类可以不再调用。"""
        p = self._phase(kind)
        if p and p != self.phase:
            self.phase = p

    def replay(self, n=25):
        """把游标往回退，让下一轮能重放最近的真实记录（仅自检使用）。"""
        self._replay_left = n

    _replay_left = 0

    def _take(self, items):
        """自检限流：回放时最多取 N 条。"""
        if not self._replay_left:
            return items
        keep, self._replay_left = items[: self._replay_left], max(
            0, self._replay_left - len(items))
        return keep

    def __repr__(self):
        return "<%s %s enabled=%s src=%s>" % (
            self.__class__.__name__, self.key, self.enabled, self.source)
