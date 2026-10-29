# -*- coding: utf-8 -*-
"""千问办公（QwenWorkCN）适配器 —— 基于真实事件流的精准状态识别。

为什么不复用 StructuredAdapter：
  旧版 qwen_work.py 只是 StructuredAdapter 的子类，靠 SchemaProbe 去
  ~/.qwenworkcn 下面"猜"哪个库哪张表是会话流。早期在本机（QwenWorkCN 1.3.2 /
  cli 1.1.71）连 data/ 目录都没有；2026-10-29 复查时 data/agents.db 已经出现
  （agent_trajectory_log / messages 都有数据），但它只是落库结果，不是实时
  事件流，本适配器不读它。当时的实测结果是：
    * 它能找到的 host-agent-responses.db 里 desk_turns /
      desk_turn_events / desk_input_admissions 三张表**都是 0 行**；
    * 真正的事件流 logs/sessions/<proj>/<sess>/segments/<run>.jsonl
      因为文件名是一串时间戳、不含 session/message/log 之类关键字，
      被 scan_files(min_score=1) 全部过滤掉。
  于是适配器"挂载成功"却一条状态都产不出来。这里改成直接对接真实数据源。

真实数据源（本机实测）：
  ~/.qwenworkcn/logs/sessions/<project_id>/<session_uuid>/segments/<run_id>.jsonl
      每行一个 JSON，统一信封：
      {type, ts, seq, level, turn_id, loop_id?, tool_call_id?, request_id?, data}
      seq 单调递增 —— 天然的增量游标。
  ~/.qwenworkcn/logs/latest -> runs/<run_id>/          指向当前 run 的符号链接
  ~/.qwenworkcn/logs/runs/<run_id>/manifest.json       {run_id, started_at, pid,
      ppid, process_role, cli_version, node_version, cwd, project_id, log}

事件 -> 状态映射（每一条都来自软件自己写下的记录，零推断）：
  input.prompt.received / .submitted  已接题    data.text_preview / text_length
  turn.started                        开始本轮  data.model / prompt_length
  model.request.started               思考中
  model.response.completed            思考结束  data.stop_reason(tool_use|end_turn)
  tool.requested                      执行工具  data.tool_name / args
  tool.shell.started                  执行命令  data.command
  tool.execution.finished             工具返回  data.status(success|...)
  permission.requested                等你拍板  data.ask_message  ← 关键
  permission.resolved                 拍板完成  data.allowed / outcome / source
  turn.finished                       本轮结束  data.reason / num_turns / duration_ms
  session.phase.finished              阶段结束  data.success=false -> 出错

关于 permission 的两个实测事实（决定了本适配器的精度）：
  1. 75/75 条 permission.requested 都带 tool_call_id，与 permission.resolved
     精确配对，实测 0 条悬空；
  2. 由策略自动放行的那 6 次（source='policy'）**根本不产生 requested 事件**。
  => 不变量：只要出现 permission.requested，就一定是真的在问用户。零误报。

明确忽略的噪音事件（实测占比最高，且不是用户关心的"那一步"）：
  hook.started / hook.finished（单次会话 755+754 条）、
  attachment.generator.finished（267）、tool.read.diagnostic（378）、
  loop.iteration.*、model.request.preparation、cli.route.entered、
  session.config.loaded、session.phase.started。
"""
from __future__ import annotations

import glob
import json
import os
import time
from collections import OrderedDict

from adapters.base import Adapter
from core.model import (K_ASK, K_ANSWER, K_DENIED, K_ERROR, K_RESULT, K_STATUS,
                        K_THINK, K_TOOL, K_TOOL_DONE, K_USER,
                        P_ASKING, P_ANSWERING, P_DENIED, P_DONE, P_ERROR,
                        P_IDLE, P_RECEIVED, P_STALLED, P_THINKING, P_TOOL,
                        short)

# 段文件相对 roots 的位置
SEG_GLOB = os.path.join("logs", "sessions", "*", "*", "segments", "*.jsonl")
MANIFEST_GLOB = os.path.join("logs", "runs", "*", "manifest.json")
LATEST_LINK = os.path.join("logs", "latest")

# 冷启动回溯多少字节。目的不是重放历史，而是**在不刷屏的前提下**
# 重建当前状态：此刻是不是有一轮在跑、是不是正卡在一个授权弹窗上。
COLD_START_BYTES = 256 * 1024

ASK_REEMIT = 5.0        # 等待期间每 5 秒重发一次，让状态栏的"已等待"计时走动
RESCAN_SEC = 3.0        # 多久重扫一次目录，捕捉新开的会话 / 新 run
MAX_TRACKED = 4         # 同时跟几个段文件（多窗口场景）
STUB_BYTES = 2048       # 实测空桩只有 508 字节；超过这个才算真的产出过事件
ACTIVE_WINDOW = 1800.0  # 只把最近 30 分钟写过的东西当候选，与桥接 TTL 同口径
MAX_TURNS = 12          # 保留几个 turn 的状态，防止长期运行涨内存
SINGLE_READ = 1 << 20   # 单次最多读 1MB，避免大文件一口吞掉内存
MISSING_LIMIT = 20      # 连续多少次读不到文件就认定它被清理了
MAX_NATIVE_CALLS = 512
NATIVE_ASK = "等待你选择，请在千问办公中回答"

# 从 tool 参数里挑一个最能说明"在干什么"的字段
ARG_KEYS = ("command", "file_path", "path", "pattern", "query", "url",
            "description", "filePath", "notebook_path", "prompt")


def _fmt_dur(sec):
    """秒 -> 人类可读时长。"""
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return ""
    if sec < 0:
        return ""
    if sec < 60:
        return "%d 秒" % int(sec)
    if sec < 3600:
        return "%d 分 %02d 秒" % (int(sec) // 60, int(sec) % 60)
    return "%d 小时 %02d 分" % (int(sec) // 3600, (int(sec) % 3600) // 60)


def _arg_brief(args):
    """从 tool 参数里抽一句话摘要。args 可能是 dict，也可能是 JSON 字符串。"""
    if isinstance(args, str):
        s = args.strip()
        if s[:1] in "{[":
            try:
                args = json.loads(s)
            except Exception:
                return short(s, 90)
        else:
            return short(s, 90)
    if not isinstance(args, dict):
        return ""
    for k in ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return short(v.strip(), 90)
    try:
        return short(json.dumps(args, ensure_ascii=False), 90)
    except Exception:
        return ""


class _Turn:
    """一个会话（turn_id）的实时状态。每个字段都由真实事件推进。"""

    __slots__ = ("tid", "sid", "uid", "phase", "label", "pending", "tools", "model_in",
                 "last_ts", "ask_since", "ask_tool", "ask_msg", "nsteps",
                 "started", "turn_no", "ask_details", "closed",
                 "awaiting_start", "seen_seq", "native_calls")

    def __init__(self, tid, sid, uid):
        self.tid = tid
        self.sid = sid             # 段文件所属会话：状态栏按它分行，不按轮次
        self.uid = uid             # 本轮的不透明身份，用于旧轮迟到事件排序
        self.phase = P_IDLE
        self.label = ""            # 用户这句问话的预览，做任务标题用
        self.pending = {}          # tool_call_id -> 开始等待的时间
        self.ask_details = {}
        self.native_calls = OrderedDict()  # 保留已解决调用，防止迟到请求重新挂起
        self.tools = {}            # tool_call_id -> tool_name
        self.model_in = False      # 有一次 model.request 还没 completed
        self.last_ts = 0.0
        self.ask_since = 0.0       # 当前这次等待从什么时候开始
        self.ask_tool = ""
        self.ask_msg = ""
        self.nsteps = 0
        self.started = 0.0
        self.turn_no = 0
        self.closed = False
        self.awaiting_start = False
        self.seen_seq = OrderedDict()  # (segment source, seq)，跨轮保留、有界

    def begin(self, ts, uid, submitted=False):
        self.uid = uid
        self.turn_no += 1
        self.closed = False
        self.awaiting_start = submitted
        self.started, self.nsteps = ts, 0
        self.phase = P_RECEIVED
        self.pending.clear()
        self.ask_details.clear()
        self.native_calls.clear()
        self.tools.clear()
        self.ask_since, self.ask_tool, self.ask_msg = 0.0, "", ""
        self.model_in = False


class _Reader:
    """单个 segments/*.jsonl 的增量读取游标。

    与 base.TailReader 的区别：这里需要"冷启动静默重建状态"，
    所以读取与解析分离——read_lines() 只吐新增行，解析在适配器里。
    """

    def __init__(self, path, cold_bytes=0, from_start=False):
        self.path = path
        self.buf = b""
        self.missing = 0
        self._sig = None
        self._anchor = b""
        self._dropping = False
        self.last_event_at = 0.0    # 最近一次读到新行的时间；淘汰按它而不是 mtime
        try:
            st = os.stat(path)
            size = st.st_size
            self._sig = self._signature(st)
        except OSError:
            size = 0
        self.off = 0 if from_start else max(0, size - cold_bytes) if cold_bytes else size
        if self.off:
            try:
                with open(path, "rb") as f:
                    f.seek(max(0, self.off - 128))
                    self._anchor = f.read(min(128, self.off))
            except OSError:
                pass

    @staticmethod
    def _signature(st):
        birth = getattr(st, "st_birthtime_ns", 0) if os.name == "nt" else st.st_ctime_ns
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, birth)

    def read_lines(self):
        """返回新增的完整行；文件被轮转/截断则重置游标。"""
        try:
            st = os.stat(self.path)
        except OSError:
            self.missing += 1
            self._sig = None
            return []
        self.missing = 0
        sig = self._signature(st)
        if sig == self._sig and st.st_size == self.off:
            return []
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                sig = self._signature(st)
                reset = (self._sig is None or st.st_size < self.off
                         or sig[:2] != self._sig[:2] or st.st_size < self._sig[2])
                if not reset and self.off and self._anchor and sig != self._sig:
                    f.seek(max(0, self.off - len(self._anchor)))
                    reset = f.read(len(self._anchor)) != self._anchor
                if reset:
                    self.off, self.buf, self._dropping = 0, b"", False
                f.seek(self.off)
                data = f.read(min(max(0, st.st_size - self.off), SINGLE_READ))
                self.off += len(data)
                f.seek(max(0, self.off - 128))
                self._anchor = f.read(min(128, self.off))
        except OSError:
            return []
        self._sig = sig
        data = self.buf + data
        parts = data.split(b"\n")
        self.buf = parts.pop()
        if self._dropping:
            if parts:
                parts.pop(0)
                self._dropping = False
            else:
                self.buf = b""
        if len(self.buf) > SINGLE_READ:
            self.buf, self._dropping = b"", True
        out = []
        for raw in parts:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(raw.decode("utf-8", "replace"))
            except Exception:
                continue
        return out


class QwenWorkAdapter(Adapter):
    key = "qwen_work"
    zh = "千问办公"
    en = "Qwen Work"
    tier = "A"

    # 事件流很密，活跃时要比默认 0.5s 更快，否则"等你拍板"会迟到
    T_ACTIVE, T_QUIET, T_AWAY = 0.4, 1.5, 15.0

    def __init__(self, info, log=None):
        Adapter.__init__(self, info, log)
        self.readers = {}          # path -> _Reader
        self.turns = {}            # turn_id -> _Turn
        self.order = []            # turn_id 活跃顺序（新 -> 旧）
        self.project = ""          # 人类可读的项目名，做副标题
        self._rescan_at = 0.0
        self._ask_at = 0.0
        self._cold = True
        self._turn_seq = 0

    # ------------------------------------------------------------ 装配
    def _seg_files(self):
        out = []
        for r in self.roots:
            if not r or not os.path.isdir(r):
                continue
            try:
                out.extend(glob.glob(os.path.join(r, SEG_GLOB)))
            except Exception:
                continue
        seen, uniq = set(), []
        for p in out:
            k = os.path.normcase(os.path.abspath(p))
            if k in seen:
                continue
            seen.add(k)
            try:
                st = os.stat(p)
                uniq.append((st.st_mtime, st.st_size, p))
            except OSError:
                continue
        uniq.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return uniq

    def _open_paths(self, now=None):
        """已挂载、且值得继续跟的未结束段文件。

        正卡在授权弹窗上的会话可能几小时没有新写入，不能按窗口踢掉；
        没有待确认项、又长期没有动作的未结束轮次（应用崩溃留下的残局）
        只保留到活跃窗口结束，避免永久占名额。
        """
        now = time.time() if now is None else now
        open_sids = {t.sid for t in self.turns.values()
                     if not t.closed and t.sid
                     and (t.pending or now - (t.last_ts or 0.0) <= ACTIVE_WINDOW)}
        if not open_sids:
            return set()
        return {p for p in self.readers if self._session_of(p) in open_sids}

    def _choose(self, files, now=None):
        """挑要跟的段文件：只跟最近活跃的，空桩和旧会话不占名额。

        优先级：还有未结束轮次的已挂载文件 → 近期有产出的 reader
        → 近期写入的有内容文件 → 最新文件补位。最新文件常是刚建的空桩，
        满额时不能让它挤掉真实窗口，只在还有空位时收进来接下一轮。
        """
        if not files:
            return []
        now = time.time() if now is None else now
        by_path = {p: (mtime, size) for mtime, size, p in files}
        chosen, seen = [], set()

        def add(p):
            if p in by_path and p not in seen and len(chosen) < MAX_TRACKED:
                seen.add(p)
                chosen.append(p)

        def fresh(ts):
            return ts and now - ts <= ACTIVE_WINDOW

        for p in self._open_paths(now):
            add(p)

        active = []
        for p, rd in self.readers.items():
            if p not in by_path or p in seen:
                continue
            last = max(rd.last_event_at or 0.0, by_path[p][0])
            if fresh(last):
                active.append((last, p))
        active.sort(reverse=True)
        for _, p in active:
            add(p)

        for mtime, size, p in files:
            if size > STUB_BYTES and fresh(mtime):
                add(p)

        mtime0, _, p0 = files[0]
        if fresh(mtime0):
            add(p0)
        return chosen

    def _read_project_label(self):
        """从 latest/manifest.json 拿 cwd，取末级目录名做副标题。"""
        for r in self.roots:
            if not r:
                continue
            try:
                mf = ""
                link = os.path.join(r, LATEST_LINK)
                if os.path.exists(link):
                    tgt = os.path.realpath(link)
                    cand = os.path.join(tgt, "manifest.json")
                    if os.path.isfile(cand):
                        mf = cand
                if not mf:
                    cands = sorted(glob.glob(os.path.join(r, MANIFEST_GLOB)),
                                   key=os.path.getmtime, reverse=True)
                    mf = cands[0] if cands else ""
                if mf and os.path.isfile(mf):
                    with open(mf, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    cwd = str(d.get("cwd") or "")
                    if cwd:
                        return os.path.basename(cwd.rstrip("\\/")) or cwd
            except Exception:
                continue
        return ""

    def setup(self):
        files = self._seg_files()
        if not files:
            return False
        self.project = self._read_project_label()
        chosen = self._choose(files)
        sizes = {p: size for _, size, p in files}
        # 冷启动只回溯最有可能是"正在跑"的那个文件；空桩没有状态可重建。
        cold_path = next((p for p in chosen if sizes.get(p, 0) > STUB_BYTES),
                         chosen[0] if chosen else "")
        for p in chosen:
            if p not in self.readers:
                cold = COLD_START_BYTES if (self._cold and p == cold_path) else 0
                self.readers[p] = _Reader(p, cold)
        cold, self._cold = self._cold, False
        # 有段文件就保持挂载：当前都超过活跃窗口时先不读，靠 tick 里每 3 秒
        # 的 _rescan 接住下一个新会话；否则引擎要等 5 分钟重建才会发现它。
        self.enabled = True
        if not self.readers:
            self.source = "logs/sessions/*/segments/*.jsonl ×0"
            self.log("[%s] 段文件都不在活跃窗口内，等待新会话" % self.key)
        else:
            self.source = "logs/sessions/*/segments/*.jsonl ×%d" % len(self.readers)
            if cold:
                self._rebuild_state()      # 静默重建，不刷屏
            self.log("[%s] 已挂载，跟 %d 个段文件，项目=%s"
                     % (self.key, len(self.readers), self.project or "?"))
        return True

    # ------------------------------------------------------------ 冷启动
    def _rebuild_state(self):
        """把回溯到的历史事件吃进状态机，但**不产出任何步骤**。

        这样追踪器一启动就知道"现在有一轮在跑 / 正卡在一个授权弹窗上"，
        而不是要等下一条新事件才反应过来。旧实现从 EOF 起读，
        启动前发生的一切完全不可见（冷启动盲区）。
        """
        for rd in list(self.readers.values()):
            for line in rd.read_lines():
                self._feed(line, emit=False, source=rd.path)
        self._prune_turns()

    # ------------------------------------------------------------ 轮询
    def tick(self, now):
        out = []
        if now >= self._rescan_at:
            self._rescan_at = now + RESCAN_SEC
            if self._rescan():
                out.extend(self._snapshot_steps(now))
        for path, rd in list(self.readers.items()):
            if rd.missing > MISSING_LIMIT:      # 会话被清理了
                self.readers.pop(path, None)
                continue
            lines = rd.read_lines()
            if lines:
                rd.last_event_at = now
            for line in lines:
                out.extend(self._feed(line, emit=True, now=now, source=path))
        out.extend(self._reemit_asks(now))
        self._prune_turns()
        return self._take(out)

    def _rescan(self):
        """发现新的段文件就挂上。返回是否有变化。"""
        files = self._seg_files()
        if not files:
            return False
        chosen = self._choose(files)
        changed = False
        for p in chosen:
            if p not in self.readers:
                # 新 run 的文件是从头写的，直接从头读，能拿到完整的一轮
                self.readers[p] = _Reader(p, 0, from_start=True)
                changed = True
        keep = set(chosen)
        for p in list(self.readers):
            if p not in keep:
                self.readers.pop(p, None)
        if changed:
            lbl = self._read_project_label()
            if lbl:
                self.project = lbl
        return changed

    def _snapshot_steps(self, now):
        """新挂上一个会话时把它当前状态报一条，免得界面上凭空多一行空任务。"""
        out = []
        for t in self._active_turns():
            if t.phase == P_ASKING and t.pending:
                out.append(self._ask_step(t, now))
            elif not t.closed and t.phase not in (P_IDLE, P_DONE):
                s = self.emit(K_STATUS, self._title_of(t), now, session=t.sid,
                              turn=t.uid, phase=t.phase, reason="rescan")
                s.meta["phase"] = t.phase
                out.append(s)
        return out

    # ------------------------------------------------------------ 事件解析
    def _feed(self, line, emit=True, now=None, source=""):
        """解析一行事件，推进状态机。emit=False 时只更新状态、不产出步骤。"""
        try:
            o = json.loads(line)
        except Exception:
            return []
        if not isinstance(o, dict):
            return []
        typ = o.get("type")
        if not typ:
            return []
        d = o.get("data") if isinstance(o.get("data"), dict) else {}
        tid = str(o.get("turn_id") or "")
        nowf = float(now or time.time())
        try:
            ts = float(o.get("ts") or 0) or nowf
        except (TypeError, ValueError):
            ts = nowf
        if ts > 1e12:                      # 毫秒时间戳
            ts /= 1000.0
        tcid = str(o.get("tool_call_id") or "")
        sid = self._session_of(source)
        t = self.turns.get(tid)
        if t is None:
            t = self._turn(tid, sid)
        elif sid and t.sid != sid:
            t.sid = sid
        # 不同段可重用序号，且事件可能乱序，不能按全局高水位丢弃。
        seq = o.get("seq")
        if type(seq) is int:
            key = (source, seq)
            if key in t.seen_seq:
                return []
            t.seen_seq[key] = None
            while len(t.seen_seq) > 512:
                t.seen_seq.popitem(last=False)
        if t.closed and typ not in ("input.prompt.submitted", "turn.started"):
            return []
        self._turn(tid, sid)
        t.last_ts = ts
        out = []

        def add(kind, text, phase=None, **meta):
            """产出一步；phase 缺省时按 kind 推。静默模式只推进状态。"""
            if not t.turn_no:  # 冷启动可能从一轮中途开始读取
                t.turn_no = 1
            p = P_ASKING if t.pending else phase or self._phase(kind) or t.phase
            t.phase = p
            if not emit:
                return
            m = dict(meta)
            m["phase"] = p
            s = self.emit(kind, text, ts, session=t.sid, turn=t.uid, **m)
            s.meta["phase"] = p            # emit 里的 phase 是适配器级的，以 turn 级为准
            out.append(s)

        # ---- 用户提问 / 一轮开始 -------------------------------------
        if typ in ("input.prompt.received", "input.prompt.submitted"):
            prev = str(d.get("text_preview") or "").strip()
            if prev:
                t.label = short(prev, 48)
            if typ.endswith("submitted"):
                t.begin(ts, self._next_uid(), submitted=True)
                add(K_USER, t.label or "收到新的提问", phase=P_RECEIVED,
                    text_length=d.get("text_length"))

        elif typ == "turn.started":
            # submitted 与紧随的 started 属于同一轮，不能产生两个任务键。
            if t.closed or not t.awaiting_start:
                t.begin(ts, self._next_uid())
            t.awaiting_start = False
            if t.phase != P_ASKING:
                t.phase = P_THINKING
            add(K_STATUS, "%s｜第 %d 轮开始"
                % (t.label or self.project or "会话", t.turn_no),
                phase=P_RECEIVED, model=d.get("model"))

        # ---- 模型推理 ------------------------------------------------
        elif typ == "model.request.started":
            t.model_in = True
            add(K_THINK, "思考中…", phase=P_THINKING)

        elif typ == "model.response.completed":
            t.model_in = False
            stop = str(d.get("stop_reason") or "")
            if stop == "end_turn":
                add(K_ANSWER, "组织回答…", phase=P_ANSWERING, stop_reason=stop)
            else:
                # stop_reason=tool_use：模型要求调工具，紧接着就是 tool.requested
                t.phase = P_THINKING

        # ---- 工具执行 ------------------------------------------------
        elif typ == "tool.requested":
            name = str(d.get("tool_name") or "tool")
            native = (name == "AskUserQuestion"
                      and isinstance(o.get("tool_call_id"), str) and bool(tcid.strip()))
            if native and tcid in t.native_calls:
                return []
            if tcid:
                t.tools[tcid] = name
            t.nsteps += 1
            if native:
                # 原生问题的 permission.requested 直到用户回答后才落盘。
                self._wait_native(t, tcid, ts)
                add(K_ASK, NATIVE_ASK, phase=P_ASKING, tool=name, ask=True)
            else:
                brief = _arg_brief(d.get("args"))
                add(K_TOOL, "%s%s" % (name, (" ｜ " + brief) if brief else ""),
                    phase=P_TOOL, tool=name, args=brief)

        elif typ == "tool.shell.started":
            cmd = _arg_brief(d.get("command") or d.get("args"))
            add(K_TOOL, "执行命令%s" % ((" ｜ " + cmd) if cmd else ""),
                phase=P_TOOL, tool="shell")

        elif typ in ("tool.shell.finished", "tool.execution.finished"):
            st = str(d.get("status") or d.get("exit_status") or "")
            name = str(d.get("tool_name") or (t.tools.get(tcid, "") if tcid else ""))
            if tcid:
                t.tools.pop(tcid, None)
            native = typ == "tool.execution.finished" and tcid in t.native_calls
            if native:
                t.native_calls[tcid] = False
                t.pending.pop(tcid, None)
                t.ask_details.pop(tcid, None)
                self._sync_ask(t)
            ok = (not st) or st.lower() in ("success", "ok", "0", "completed")
            if ok:
                if typ == "tool.execution.finished":
                    chars = d.get("text_chars")
                    add(K_TOOL_DONE, "%s 完成%s"
                        % (name or "工具", (" ｜ %s 字" % chars) if chars else ""),
                        phase=P_THINKING if native else P_TOOL, tool=name)
                else:
                    t.phase = P_TOOL
            else:
                add(K_ERROR, "%s 失败（%s）" % (name or "工具", st),
                    phase=P_THINKING if native else P_ERROR,
                    tool=name, status=st, terminal=False)

        # ---- 授权：本项目最关心的一态 --------------------------------
        elif typ == "permission.requested":
            msg = str(d.get("ask_message") or "").strip()
            name = str(d.get("tool_name") or "")
            pending_id = tcid or ("#%s" % (o.get("seq") or ts))
            native = (tcid in t.native_calls or
                      (name == "AskUserQuestion" and
                       isinstance(o.get("tool_call_id"), str) and bool(tcid.strip())))
            if native:
                if tcid in t.native_calls and not t.native_calls[tcid]:
                    return []
                self._wait_native(t, tcid, ts)
                name, msg = "AskUserQuestion", NATIVE_ASK
            else:
                t.pending.setdefault(pending_id, ts)
                t.ask_details[pending_id] = (name, short(msg, 120))
                self._sync_ask(t)
            add(K_ASK, short(msg or ("%s 需要授权" % (name or "工具")), 120),
                phase=P_ASKING, tool=name, ask=True,
                suggestions=d.get("suggestion_count") or 0)

        elif typ == "permission.resolved":
            allowed = d.get("allowed")
            src = str(d.get("source") or "")
            name = str(d.get("tool_name") or t.ask_details.get(tcid, ("", ""))[0])
            native = tcid in t.native_calls
            if native:
                t.native_calls[tcid] = False
            if tcid:
                t.pending.pop(tcid, None)
                t.ask_details.pop(tcid, None)
            self._sync_ask(t)
            if native:
                add(K_STATUS, "已收到选择，继续处理" if allowed else "已取消选择，继续处理",
                    phase=P_THINKING, tool="AskUserQuestion", allowed=allowed,
                    duration_ms=d.get("duration_ms"))
            elif allowed:
                # source=policy 表示策略自动放行、根本没问过用户，不值得播报
                if src == "user":
                    add(K_STATUS, "已批准 %s" % (name or "该操作"),
                        phase=P_TOOL, allowed=True,
                        duration_ms=d.get("duration_ms"))
                else:
                    t.phase = P_TOOL if not t.pending else P_ASKING
            else:
                add(K_DENIED, "已拒绝授权 ｜ %s" % (name or "该操作"),
                    phase=P_DENIED, allowed=False,
                    behavior=str(d.get("decision_behavior") or ""))
            if not t.pending and t.phase == P_ASKING:
                t.phase = P_THINKING

        # ---- 一轮结束 ------------------------------------------------
        elif typ == "turn.finished":
            reason = str(d.get("reason") or "")
            dur = _fmt_dur((d.get("duration_ms") or 0) / 1000.0)
            n = d.get("num_turns")
            t.pending.clear()
            t.ask_details.clear()
            t.native_calls.clear()
            t.tools.clear()
            t.ask_since, t.ask_tool, t.ask_msg = 0.0, "", ""
            t.model_in, t.awaiting_start = False, False
            t.closed = True
            if reason and reason != "end_turn":
                add(K_ERROR, "本轮异常结束（%s）" % reason,
                    phase=P_ERROR, reason=reason, terminal=True)
            else:
                bits = [x for x in (("%s 步" % n) if n else "", dur) if x]
                add(K_RESULT, "本轮完成%s"
                    % ((" ｜ " + " · ".join(bits)) if bits else ""),
                    phase=P_DONE, reason=reason, num_turns=n,
                    duration_ms=d.get("duration_ms"), terminal=True)

        # ---- 阶段失败 ------------------------------------------------
        elif typ == "session.phase.finished":
            if d.get("success") is False:
                add(K_ERROR, "阶段失败 ｜ %s" % (d.get("phase") or ""),
                    phase=P_ERROR, stage=str(d.get("phase") or ""))

        # 其余（hook.* / attachment.* / loop.iteration.* / tool.read.diagnostic /
        # session.phase.started / cli.route.entered / model.request.preparation /
        # session.config.loaded）一律忽略：它们不是用户关心的"那一步"。
        if t.pending:
            t.phase = P_ASKING
        return out

    @staticmethod
    def _wait_native(t, tcid, ts):
        t.native_calls[tcid] = True
        while len(t.native_calls) > MAX_NATIVE_CALLS:
            old, _ = t.native_calls.popitem(last=False)
            t.pending.pop(old, None)
            t.ask_details.pop(old, None)
            t.tools.pop(old, None)
        t.pending.setdefault(tcid, ts)
        t.ask_details[tcid] = ("AskUserQuestion", NATIVE_ASK)
        QwenWorkAdapter._sync_ask(t)

    @staticmethod
    def _sync_ask(t):
        if t.pending:
            key = min(t.pending, key=t.pending.get)
            t.ask_since = t.pending[key]
            t.ask_tool, t.ask_msg = t.ask_details.get(key, ("", ""))
            t.phase = P_ASKING
        else:
            t.ask_since, t.ask_tool, t.ask_msg = 0.0, "", ""

    # ------------------------------------------------------------ 等待计时
    def _reemit_asks(self, now):
        """有未决授权时每 ASK_REEMIT 秒重发一次。

        两个作用：① 状态栏上的"已等待 X"会走动；② 桥那边任务保鲜，
        不会因为长时间没有新事件被 TTL 收走。
        """
        if not any(t.pending for t in self.turns.values()):
            self._ask_at = 0.0
            return []
        if now - self._ask_at < ASK_REEMIT:
            return []
        self._ask_at = now
        return [self._ask_step(t, now) for t in self._active_turns()
                if t.pending and t.phase == P_ASKING]

    def _ask_step(self, t, now):
        waited = _fmt_dur(now - (t.ask_since or now))
        txt = short(t.ask_msg or ("%s 需要授权" % (t.ask_tool or "工具")), 100)
        if waited:
            txt = "%s ｜ 已等待 %s" % (txt, waited)
        s = self.emit(K_ASK, txt, now, session=t.sid, turn=t.uid,
                      phase=P_ASKING, tool=t.ask_tool, ask=True,
                      pending=len(t.pending))
        s.meta["phase"] = P_ASKING
        return s

    # ------------------------------------------------------------ 辅助
    @staticmethod
    def _session_of(path):
        """段文件路径 .../<project>/<session>/segments/<run>.jsonl -> 会话 id。"""
        try:
            if os.path.basename(os.path.dirname(path)) == "segments":
                return os.path.basename(os.path.dirname(os.path.dirname(path)))
        except Exception:
            pass
        return ""

    def _next_uid(self):
        self._turn_seq += 1
        return str(self._turn_seq)

    def _turn(self, tid, sid=""):
        t = self.turns.get(tid)
        if t is None:
            t = _Turn(tid, sid, self._next_uid())
            self.turns[tid] = t
        elif sid:
            t.sid = sid
        try:
            self.order.remove(tid)
        except ValueError:
            pass
        self.order.insert(0, tid)
        if tid:
            self.cur_session = t.sid     # 收割事件要落到状态栏的同一行
        return t

    def _active_turns(self):
        return [self.turns[t] for t in self.order if t in self.turns]

    def _prune_turns(self):
        while len(self.order) > MAX_TURNS:
            self.turns.pop(self.order.pop(), None)

    def _title_of(self, t):
        head = t.label or self.project or "会话"
        return {
            P_RECEIVED: "%s｜已接题" % head,
            P_THINKING: "%s｜思考中" % head,
            P_TOOL: "%s｜执行工具" % head,
            P_ANSWERING: "%s｜组织回答" % head,
            P_ASKING: "%s｜等你拍板" % head,
            P_DENIED: "%s｜授权被拒" % head,
            P_DONE: "%s｜本轮完成" % head,
            P_STALLED: "%s｜疑似停滞" % head,
            P_ERROR: "%s｜出错" % head,
            P_IDLE: "%s｜空闲" % head,
        }.get(t.phase, "%s｜进行中" % head)

    # ------------------------------------------------------------ 自检回放
    def _mount_recent_for_replay(self):
        """自检回放不受活跃窗口限制：挑最近有内容的段文件，从尾部读。

        运行时只跟 30 分钟内的会话是对的，但 `--replay` 的语义是
        "拿最近的真实记录验证探针还能不能认"，窗口不应该把回放也挡掉。
        """
        files = [f for f in self._seg_files() if f[1] > STUB_BYTES]
        for _, _, p in files[:MAX_TRACKED]:
            if p not in self.readers:
                self.readers[p] = _Reader(p, 0)
        if self.readers:
            # 回放期间别让下一次 _rescan 按活跃窗口把它们清掉
            self._rescan_at = time.time() + 60.0

    def replay(self, n=25):
        Adapter.replay(self, n)
        if not self.readers:
            self._mount_recent_for_replay()
        for rd in self.readers.values():
            try:
                rd.off = max(0, rd.off - 120000)
                rd.buf = rd._anchor = b""
                rd._dropping = False
            except Exception:
                pass

    def _display_turn(self):
        """挑一个最值得显示的会话。

        优先级：正在等用户拍板 > 其它活跃态 > 最近碰过的（可能早已结束）。
        直接取 order[0] 会踩坑：冷启动回溯会把旧会话也塞进 turns，
        于是状态栏挑到一条 idle 的旧记录，显示"空闲"。
        """
        ts = self._active_turns()
        if not ts:
            return None
        for t in ts:
            if t.phase == P_ASKING and t.pending:
                return t
        for t in ts:
            if not t.closed and t.phase not in (P_IDLE, P_DONE):
                return t
        return ts[0]

    def status(self, now=None):
        now = now or time.time()
        st = Adapter.status(self, now)
        t = self._display_turn()
        if t:
            st.phase = t.phase
            st.title = short(self._title_of(t), 60)
            if t.phase == P_ASKING and t.ask_since:
                st.detail = short("%s ｜ 已等待 %s"
                                  % (t.ask_msg or t.ask_tool,
                                     _fmt_dur(now - t.ask_since)), 120)
        return st
