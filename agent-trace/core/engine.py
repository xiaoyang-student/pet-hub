# -*- coding: utf-8 -*-
"""追踪引擎。

省资源的几个关键点：
  * 单线程；睡眠时长 = min(下一个到期时间, SLEEP_CAP)。旧实现写的
    min(default=1.0, …) 把上限钉死在 1 秒，所谓"睡到下一个到期点、
    CPU ≈ 0"从未成立，实际是个固定 1 秒的轮询循环；
  * 每个适配器自己算下次轮询时间（活跃 0.4-0.5s / 安静 1.5-3s / 未运行 15-20s）；
  * 只读增量：SQLite 按序号取新行、文件按字节偏移取新内容，不整库扫；
  * 进程枚举结果全局缓存 10 秒，所有适配器与 reap 共用一份，
    不再每个适配器各扫一遍全机进程；
  * 任一适配器出错只影响它自己：滑动窗口计数，超限进入冷却，
    冷却到点自动重试——不永久停用，也绝不让进程崩；
  * 每 5 分钟重扫一次机器，新装的软件会自动挂上，无需重启。

一条底线：**没有"结束"的真实证据，就绝不报 done。**
长时间没有新动作只标记为 stalled（疑似停滞），由界面如实呈现。
旧实现在 150 秒无新动作时直接发 K_RESULT → done，把"Agent 正停在
等用户授权"显示成"已完成"，这是最坏的一种谎报：用户看到完成就不再去看了。
"""
from __future__ import annotations

import os
import re
import time
from collections import OrderedDict

from core import discover
from core.model import (Status, Step, K_APP_EXIT, K_ASK, K_DENIED, K_ERROR,
                        K_RESULT, K_STALLED)
from core.sinks import (_TurnBarrier, _TurnOrder, _is_terminal, _turn_id)

# ---------------------------------------------------------------- 噪音过滤
# 这些是应用内部的“心跳/鉴权/渲染线程”自语，不是任务步骤，
# 但它们占日志的绝大部分（实测 ChatGPT 一次空闲能刷几百条 auth reload）。
_NOISE_PATTERNS = (
    r"reloading\s+auth", r"reloaded\s+auth",
    r"waiting\s+to\s+resolve\s+remote\s+control",
    r"remote\s+control\s+requires",
    r"connection_id\s*=", r"request_id\s*=", r"connection\.id",
    r"heartbeat", r"keep-?alive", r"\bping\b",
    r"telemetry", r"crashpad", r"gpu-?process", r"gpu\s+process",
    r"fontconfig", r"dbus", r"xdg-?", r"dbus-?menu",
    r"\bupdate\s+check(?:er)?\b", r"checking\s+for\s+updates",
    r"electron", r"node_modules", r"stack\s+trace",
    r"\.js:\d+", r"\.ts:\d+", r"\bat\s+\w+\s+\(",
    # 宿主环境注入的内部提示/系统标签，不是用户真实的每一步
    r"system-reminder", r"data-role=", r"craft_mode", r"user_query",
    r"previous_tool_call", r"task-notification",
)
NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.I)

# 同一条内容多久内不再重复播报（秒）
DEDUPE_WINDOW = 45.0

# 这些类型**绝不过降噪**：它们是不可重放的状态变化，丢一条就会让
# 状态栏停在错误的状态上。尤其是 K_ASK —— ask_message 里常常带着
# 文件路径（main.js:42 这种），而 NOISE_RE 里恰好有 r"\.js:\d+"，
# 旧实现会把"等你拍板"当噪音静默吃掉，用户就此错过需要他决策的时刻。
_ALWAYS_PASS = frozenset((K_ASK, K_DENIED, K_RESULT, K_ERROR, K_APP_EXIT,
                          K_STALLED))

# 适配器注册表：key -> (模块, 类名)
ADAPTERS = {
    "workbuddy": ("adapters.workbuddy", "WorkBuddyAdapter"),
    "qoder": ("adapters.qoder", "QoderAdapter"),
    "zcode": ("adapters.zcode", "ZCodeAdapter"),
    "qwen_work": ("adapters.qwen_work", "QwenWorkAdapter"),
    "doubao_work": ("adapters.doubao_work", "DoubaoWorkAdapter"),
    "doubao": ("adapters.chat_apps", "DoubaoAdapter"),
    "baidu_dazi": ("adapters.qianfan", "BaiduDaziAdapter"),
    "chatgpt": ("adapters.chat_apps", "ChatGPTAdapter"),
}

RESCAN_EVERY = 300.0

# 主循环睡眠上限：睡 min(下一个到期时间, SLEEP_CAP)。
# 上限太小就退化成固定轮询（旧实现钉死在 1.0 秒），太大则新事件响应迟钝。
SLEEP_CAP = 2.0

# 幽灵任务收割：软件退出后，状态栏不该继续显示"还在跑"。
# 进程全消失且超过 REAP_GRACE 秒没有任何新步骤 -> 发 K_APP_EXIT 让桥接撤掉那一行。
REAP_EVERY = 10.0
REAP_GRACE = 20.0
# 进程还在、但这么久没有任何新动作 -> 标记 stalled（疑似停滞），**不是 done**。
# 我们没有任何"它结束了"的证据，报完成就是撒谎；报停滞才是如实呈现。
IDLE_GRACE = 150.0
STALLED_REMIND = 30.0     # 停滞期间多久重发一次，让状态栏的计时走动
MAX_SESSIONS = 1024
_ACTIVE_KINDS = frozenset(("user", "plan", "think", "tool", "tool_done",
                           "edit", "answer", "status", K_STALLED))


class Engine:
    def __init__(self, log=None, only=None, skip=None):
        self.log = log or (lambda *a: None)
        self.adapters = {}
        self.sinks = []
        self.only = set(only or [])
        self.skip = set(skip or [])
        self.rescan_at = 0.0
        self._reap_at = 0.0
        self.seq = 0
        self.dropped = 0
        self.suppressed = 0      # 被过滤/去重掉的噪音条数
        self._seen = OrderedDict()
        self._sessions = OrderedDict()
        self._terminal_turns = _TurnBarrier()
        self._turn_order = _TurnOrder()
        self.session_evicted = 0

    # ---------------------------------------------------------- 构建
    def build(self):
        found = discover.detect_all()
        for key, info in found.items():
            if key in self.adapters:
                continue
            if self.only and key not in self.only:
                continue
            if key in self.skip:
                continue
            if not info.get("installed"):
                continue
            mod_cls = ADAPTERS.get(key)
            if not mod_cls:
                continue
            try:
                mod = __import__(mod_cls[0], fromlist=[mod_cls[1]])
                cls = getattr(mod, mod_cls[1])
                # 进程名要在构造前就填好：ChatGPTAdapter 会复制 info，
                # 构造后再补 exes 它看不到，_alive() 永远为假。
                info["exes"] = self._exes(key)
                ad = cls(info, self.log)
                if ad.setup():
                    self.adapters[key] = ad
                    self.log("[engine] 已挂载 %s (%s)" % (info["zh"], ad.source))
                else:
                    self.log("[engine] %s 未找到数据源，跳过" % info["zh"])
            except Exception as e:
                self.log("[engine] %s 初始化失败(已忽略): %s" % (info["zh"], e))
        return self.adapters

    @staticmethod
    def _exes(key):
        for s in discover.SPECS:
            if s["key"] == key:
                return s["exes"]
        return []

    # ---------------------------------------------------------- 轮询
    def _due(self, now):
        return [a for a in self.adapters.values()
                if a.enabled and now >= getattr(a, "_next", 0)]

    def poll(self, now=None):
        """跑一轮，返回本轮新增 Step 列表。"""
        now = time.time() if now is None else now
        events = []
        for a in self._due(now):
            self.seq += 1
            try:
                got = a.tick(now) or []
            except Exception as e:          # 任何异常都就地吞掉
                a.on_error(e)
                # 出错也要排下一次轮询，否则主循环会立刻重撞同一个坏数据源。
                try:
                    a._next = now + a.interval(now)
                except Exception:
                    a._next = now + 5.0
                continue
            a.reset_err()
            if got:
                for s in got:
                    s.seq = self.seq
                    self.seq += 1
                events.extend(got)
            a._next = now + a.interval(now)
        events = self._clean(events, now)
        for e in events:
            for sk in self.sinks:
                try:
                    sk.push(e)
                except Exception:
                    self.dropped += 1
        # 空轮询也排空和重试，复用主循环而不增加后台轮询线程。
        for sk in self.sinks:
            try:
                tick = getattr(sk, "tick", None)
                if callable(tick):
                    tick()
            except Exception:
                self.dropped += 1
        return events

    # ---------------------------------------------------------- 降噪
    def _clean(self, events, now):
        """去掉应用内部自语 + 短窗口内重复播报，只留真正的步骤。

        _ALWAYS_PASS 和显式 terminal 不过文本降噪/短窗口去重，但所有
        同轮迟到事件（包括重复终态）都必须先遵守终态屏障。
        """
        out = []
        for e in events:
            always = e.kind in _ALWAYS_PASS or _is_terminal(e)
            txt = e.text or ""
            if not always and (not txt.strip() or NOISE_RE.search(txt)):
                self.suppressed += 1
                continue
            key = (e.app, e.session, e.turn, e.kind, txt[:120])
            last = self._seen.get(key)
            state = self._sessions.get(self._session_key(e))
            unchanged = state and state["kind"] == e.kind and state["turn"] == e.turn
            # 在改写会话/活动时间之前拦截，避免终态又被推断成 stalled。
            if not self._note_session(e, now):
                self.suppressed += 1
                continue
            if always:
                out.append(e)
                continue
            # 重复活动仍刷新活跃时间，但等待后的恢复不能被旧去重键吞掉。
            if unchanged and last is not None and now - last < DEDUPE_WINDOW:
                self.suppressed += 1
                continue
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > 500:
                self._seen.popitem(last=False)
            out.append(e)
        return out

    @staticmethod
    def _session_key(e):
        return e.app, (str(e.session or "") or e.app)[:64]

    def _forget_app(self, app):
        self._terminal_turns.forget_app(app)
        self._turn_order.forget_app(app)
        for cache in (self._sessions, self._seen):
            for key in list(cache):
                if key[0] == app:
                    del cache[key]
        a = self.adapters.get(app)
        if a is not None:
            a.last_activity = 0.0
            a.cur_session = ""
            a.reaped = True
            a.idle_marked = False

    def _note_session(self, e, now):
        if e.kind == K_APP_EXIT:
            self._forget_app(e.app)
            return True
        key = self._session_key(e)
        turn = _turn_id(e)
        if not self._turn_order.admit(key, turn):
            self._terminal_turns.accept((*key, turn), _is_terminal(e), now)
            return False
        if not self._terminal_turns.accept((*key, turn), _is_terminal(e), now):
            return False
        self._sessions[key] = {
            "kind": e.kind, "at": now, "text": (e.text or "")[:80],
            "turn": e.turn, "stalled_at": None,
            "waiting": bool((e.meta or {}).get("ask")),
            "inactive": _is_terminal(e) or
                        (e.meta or {}).get("phase") in ("idle", "observing", "cancelled"),
        }
        self._sessions.move_to_end(key)
        if len(self._sessions) > MAX_SESSIONS:
            # 淘汰只停止该会话的停滞推断，绝不伪造完成或运行状态。
            self._sessions.popitem(last=False)
            self.session_evicted += 1
        a = self.adapters.get(e.app)
        if a is not None:
            a.last_activity = now
            a.last_text = (e.text or "")[:80]
            a.cur_session = e.session or ""
            a.reaped = False
            a.idle_marked = False
        return True

    def next_wake(self, now, cap=None):
        """睡多久：到下一个适配器到期为止，但不超过 cap。

        旧签名是 next_wake(now, default=1.0) 且返回 min(default, …)，
        default 实际是**上限**而不是"没有适配器时的缺省值"，
        于是无论下一个到期点是 20 秒后还是 0.1 秒后，都固定睡 1 秒。
        """
        cap = SLEEP_CAP if cap is None else cap
        due = [getattr(a, "_next", 0) for a in self.adapters.values() if a.enabled]
        for sk in self.sinks:
            try:
                wake = getattr(sk, "next_wake", None)
                delay = wake(now) if callable(wake) else None
                if delay is not None:
                    due.append(now + delay)
            except Exception:
                pass
        if not due:
            return cap
        return max(0.02, min(cap, min(due) - now + 0.01))

    def statuses(self):
        now = time.time()
        out = []
        for a in self.adapters.values():
            try:
                out.append(a.status(now).to_dict())
            except Exception:
                out.append(Status(app=a.key).to_dict())
        return out

    def tick_housekeeping(self, now):
        if now >= self.rescan_at:
            self.rescan_at = now + RESCAN_EVERY
            try:
                self.build()
            except Exception:
                pass
        for a in self.adapters.values():
            if not hasattr(a, "_next"):
                a._next = now
            # 冷却到点的适配器自动复活。build() 对已存在的 key 直接 continue，
            # 所以旧实现里一旦 enabled=False 就永远回不来了。
            if not a.enabled:
                try:
                    if a.revive_if_due(now):
                        a._next = now
                except Exception:
                    pass
        self.reap_dead(now)

    def reap_dead(self, now):
        """幽灵任务收割：别让状态栏撒谎。

        两种情况：
          1) 软件进程全部退出 且 超过 REAP_GRACE 秒无新步骤
             -> 发 K_APP_EXIT，桥接直接撤掉那一行；
          2) 进程还在但超过 IDLE_GRACE 秒没有任何新动作
             -> 发 K_STALLED（疑似停滞），并把停滞时长显示在状态栏上。
                **绝不发 done**：我们手里没有任何"它结束了"的证据，
                而它很可能正停在等用户授权。旧实现在这里发 K_RESULT
                → done，等于告诉用户"完成了、别看了"，是最坏的一种谎报。
        """
        if now < self._reap_at:
            return
        self._reap_at = now + REAP_EVERY
        for key, a in list(self.adapters.items()):
            la = getattr(a, "last_activity", 0.0)
            if not la:
                continue                      # 从没产出过步骤，不归我们管
            try:
                alive = discover.is_running_cached(key)
            except Exception:
                alive = True
            if not alive:
                if getattr(a, "reaped", False):
                    continue
                if now - la < REAP_GRACE:
                    continue
                a.reaped = True
                a.idle_marked = False
                self._emit_housekeeping(a, K_APP_EXIT, "软件已退出", now)
                self.log("[engine] %s 进程已退出，撤销状态栏任务" % a.zh)
                continue

            for (app, session), state in self._sessions.items():
                if (app != key or state["kind"] not in _ACTIVE_KINDS
                        or state["waiting"] or state["inactive"]):
                    continue
                idle = now - state["at"]
                if idle <= IDLE_GRACE:
                    continue
                marked = state["stalled_at"]
                if marked is not None and now - marked < STALLED_REMIND:
                    continue
                state["stalled_at"] = now
                txt = state["text"].strip()
                msg = ("疑似停滞 %d 分钟" % int(idle // 60)) if idle >= 60 \
                    else ("疑似停滞 %d 秒" % int(idle))
                if txt:
                    msg = "%s ｜ 最后在动：%s" % (msg, txt)
                self._emit_housekeeping(a, K_STALLED, msg, now, session=session,
                                        turn=state["turn"], phase="stalled")
                if marked is None:
                    self.log("[engine] %s %d 秒无新动作，标记为疑似停滞（不是完成）"
                             % (a.zh, int(idle)))

    def _emit_housekeeping(self, a, kind, text, now, session=None, turn="", **meta):
        m = dict(meta)
        m.setdefault("phase", "stalled" if kind == K_STALLED else "")
        if session is None:
            session = str(getattr(a, "cur_session", "") or "")
        s = Step(ts=now, app=a.key, kind=kind, text=text,
                 session=session, turn=turn, meta=m)
        if kind == K_APP_EXIT:
            self._forget_app(a.key)
        for sk in self.sinks:
            try:
                sk.push(s)
            except Exception:
                self.dropped += 1


def run(engine, stop_after=None, on_event=None, status_every=0):
    """主循环。stop_after 秒后自动退出（便于自测）。"""
    t0 = time.time()
    last_status = 0.0
    while True:
        now = time.time()
        engine.tick_housekeeping(now)
        evs = engine.poll(now)
        if evs and on_event:
            try:
                on_event(evs)
            except Exception:
                pass
        if status_every and now - last_status >= status_every:
            last_status = now
            if on_event:
                on_event([])
        if stop_after and (time.time() - t0) >= stop_after:
            return
        time.sleep(engine.next_wake(time.time()))
