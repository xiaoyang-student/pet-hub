# -*- coding: utf-8 -*-
"""聚焦回归：真实本地 HTTP 传输与可控 sender 模拟分别标注。"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engine import Engine, IDLE_GRACE, MAX_SESSIONS, run
from core.model import PHASE_OF_KIND, Step
from core.sinks import APP_NAMES, PetHubSink


def event(kind="tool", session="S", app="qoder", text="执行任务", turn="T"):
    return Step(ts=1000.0, app=app, kind=kind, text=text, session=session,
                turn=turn, meta={"phase": PHASE_OF_KIND.get(kind, "")})


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


def apply_payload(rows, payload):
    platform = payload["platform"]
    if payload.get("type") == "sync":
        for key in list(rows):
            if key[0] == platform:
                del rows[key]
    else:
        rows[(platform, payload["task_id"])] = payload


class ControlledSender(PetHubSink):
    """模拟 sender：不经过 HTTP，只控制发送成败并记录投递顺序。"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.online = True
        self.raise_error = False
        self.attempts = []
        self.delivered = []
        self.rows = {}

    def _send(self, data):
        payload = json.loads(data.decode("utf-8"))
        self.attempts.append(payload)
        if self.raise_error:
            raise OSError("controlled disconnect")
        if not self.online:
            return False
        self.delivered.append(payload)
        apply_payload(self.rows, payload)
        return True


class HTTPReceiver:
    """真实 loopback HTTP 接收器；仅状态归约模拟桥，不启动 PetHub UI。"""
    def __init__(self, start=True):
        self.fail = False
        self.attempts = []
        self.delivered = []
        self.rows = {}
        self.thread = None
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                receiver.attempts.append(payload)
                if receiver.fail:
                    self.send_response(503)
                else:
                    receiver.delivered.append(payload)
                    apply_payload(receiver.rows, payload)
                    self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.server_bind()
        self.url = "http://127.0.0.1:%d/api/events" % self.server.server_port
        if start:
            self.start()

    def start(self):
        self.server.server_activate()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.thread:
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()


class FakeAdapter:
    def __init__(self, key="qoder"):
        self.key = key
        self.zh = APP_NAMES[key]
        self.enabled = True
        self._next = 0.0
        self.events = []
        self.cur_session = ""

    def tick(self, now):
        events, self.events = self.events, []
        return events

    def interval(self, now):
        return 60.0

    def reset_err(self):
        pass

    def on_error(self, error):
        raise AssertionError(error)


class ClockCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = patch("time.time", self.clock.time)
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, sink):
        engine = Engine()
        engine.sinks = [sink]
        engine.rescan_at = float("inf")
        engine.adapters["qoder"] = FakeAdapter()
        return engine

    def feed(self, engine, *events):
        adapter = engine.adapters["qoder"]
        adapter.events = list(events)
        adapter._next = self.clock.now
        return engine.poll(self.clock.now)

    def drain(self, sink):
        for _ in range(100):
            delay = sink.next_wake(self.clock.now)
            if delay is None:
                return
            self.clock.advance(delay)
            sink.tick()
        self.fail("pending queue did not drain")


class ControlledSenderTests(ClockCase):
    def test_running_done_never_replays_held_running(self):
        sink = ControlledSender(throttle=10)
        sink.push(event("user"))
        sink.push(event("tool", text="旧工具状态"))
        sink.push(event("result", text="完成"))
        self.drain(sink)
        self.assertEqual([p["status"] for p in sink.delivered], ["running", "done"])
        self.assertFalse(sink._held)

    def test_new_running_replaces_old_pending_before_flush(self):
        sink = ControlledSender(throttle=10)
        sink.push(event("user"))
        sink.push(event(text="旧状态"))
        self.clock.advance(10)
        sink.push(event(text="新状态"))
        self.drain(sink)
        self.assertEqual([p["message"] for p in sink.delivered], ["执行任务", "新状态"])

    def test_more_than_four_sessions_drain_without_new_events(self):
        sink = ControlledSender(throttle=1)
        sink.push(event("user", "first"))
        for i in range(11):
            sink.push(event(session=str(i)))
        self.assertEqual(len(sink._held), 11)
        self.clock.advance(1)
        sink._flush_held(self.clock.now)
        self.assertEqual(len(sink._held), 7)
        self.assertEqual(len(sink.delivered), 5)
        self.drain(sink)
        self.assertEqual(len(sink.rows), 12)
        self.assertEqual(sink.evicted, 0)

    def test_same_id_isolated_by_platform(self):
        sink = ControlledSender(throttle=10)
        sink.push(event("user", "first"))
        sink.push(event(session="same", app="qoder"))
        sink.push(event("think", "same", app="workbuddy"))
        self.assertIn(("Qoder", "same"), sink._held)
        self.assertIn(("WorkBuddy", "same"), sink._held)
        self.drain(sink)
        self.assertEqual(sink.rows[("Qoder", "same")]["status"], "running")
        self.assertEqual(sink.rows[("WorkBuddy", "same")]["status"], "thinking")

    def test_all_critical_kinds_bypass_throttle(self):
        sink = ControlledSender(throttle=1000)
        sink.push(event("user"))
        for kind in ("ask", "denied", "result", "error", "app_exit"):
            before = len(sink.delivered)
            sink.push(event(kind, session=kind, text=""))
            self.assertEqual(len(sink.delivered), before + 1, kind)
        self.assertFalse(sink._held)
        self.assertEqual(sink.delivered[1]["status"], "waiting")
        self.assertTrue(sink.delivered[1]["ask"])

    def test_asking_phase_bypasses_throttle_and_thinking_clears_waiting(self):
        for kind in ("think", "tool", "status"):
            for marker in (False, "false"):
                with self.subTest(kind=kind, terminal=marker):
                    sink = ControlledSender(throttle=10)
                    sink.push(event("user", app="baidu_dazi"))
                    asking = event(kind, app="baidu_dazi")
                    asking.meta.update(phase="asking", terminal=marker, ask=False)
                    sink.push(asking)
                    row = sink.rows[(APP_NAMES["baidu_dazi"], "S")]
                    self.assertEqual((row["status"], row["phase"]), ("waiting", "asking"))
                    self.assertIs(row["ask"], True)
                    self.assertFalse(sink._held)
                    self.clock.advance(5)
                    sink.push(asking)
                    self.assertEqual([p["status"] for p in sink.delivered],
                                     ["running", "waiting", "waiting"])
                    sink.push(event("think", app="baidu_dazi", text="继续思考"))
                    self.drain(sink)
                    resumed = sink.rows[(APP_NAMES["baidu_dazi"], "S")]
                    self.assertEqual((resumed["status"], resumed["phase"]),
                                     ("thinking", "thinking"))
                    self.assertFalse(resumed.get("ask", False))
                    self.assertEqual(asking.meta["phase"], "asking")

    def test_terminal_denied_error_and_cancelled_override_stale_asking(self):
        cases = (
            ("result", {}, "done", "done"),
            ("denied", {}, "error", "denied"),
            ("error", {}, "error", "error"),
            ("think", {"terminal": True}, "thinking", "idle"),
            ("tool", {"terminal": True}, "running", "idle"),
            ("status", {"terminal": True}, "running", "idle"),
            ("ask", {"terminal": True}, "running", "idle"),
            ("status", {"outcome": "cancelled"}, "running", "cancelled"),
            ("status", {"terminal": True, "outcome": "cancelled"}, "running", "cancelled"),
            ("result", {"outcome": "cancelled"}, "done", "cancelled"),
            ("error", {"outcome": "cancelled"}, "error", "cancelled"),
            ("status", {"phase": "denied"}, "running", "denied"),
            ("status", {"phase": "error"}, "running", "error"),
            ("status", {"phase": "cancelled"}, "running", "cancelled"),
        )
        for kind, meta, status, phase in cases:
            for ask in (False, True):
                with self.subTest(kind=kind, meta=meta, ask=ask):
                    sink = ControlledSender(throttle=0)
                    step = event(kind)
                    step.meta.update(phase="asking", ask=ask)
                    step.meta.update(meta)
                    sink.push(step)
                    row = sink.delivered[-1]
                    self.assertEqual((row["status"], row["phase"]), (status, phase))
                    self.assertFalse(row.get("ask", False))
                    self.assertIs(row["terminal"], kind == "result" or meta.get("terminal") is True)

    def test_late_asking_cannot_replace_pending_or_delivered_terminal(self):
        for online in (False, True):
            for kind in ("result", "status", "error"):
                with self.subTest(online=online, kind=kind):
                    sink = ControlledSender(throttle=0)
                    sink.online = online
                    terminal = event(kind)
                    terminal.meta.update(terminal=True)
                    sink.push(terminal)
                    for late_kind in ("think", "tool", "status"):
                        late = event(late_kind)
                        late.meta.update(phase="asking", ask=True)
                        sink.push(late)
                    sink.online = True
                    self.drain(sink)
                    self.assertEqual(len(sink.delivered), 1)
                    self.assertIs(sink.delivered[0]["terminal"], True)
                    self.assertFalse(sink.delivered[0].get("ask", False))

    def test_asking_and_terminal_are_session_scoped(self):
        sink = ControlledSender(throttle=0)
        for session in ("a", "b"):
            asking = event("think", session, app="baidu_dazi")
            asking.meta.update(phase="asking")
            sink.push(asking)
        sink.push(event("think", "a", app="baidu_dazi"))
        a = (APP_NAMES["baidu_dazi"], "a")
        b = (APP_NAMES["baidu_dazi"], "b")
        self.assertEqual(sink.rows[a]["status"], "thinking")
        self.assertFalse(sink.rows[a].get("ask", False))
        self.assertEqual(sink.rows[b]["status"], "waiting")
        self.assertIs(sink.rows[b]["ask"], True)
        sink.push(event("result", "a", app="baidu_dazi"))
        sink.push(asking)
        self.assertEqual(sink.rows[a]["status"], "done")
        self.assertEqual(sink.rows[b]["status"], "waiting")
        self.assertIs(sink.rows[b]["ask"], True)

    def test_backoff_is_bounded_and_retries_only_latest(self):
        sink = ControlledSender()
        sink.online = False
        sink.push(event("user"))
        for delay in (2, 4, 8, 16, 32, 60, 60, 60):
            self.assertEqual(sink.next_wake(self.clock.now), delay)
            count = len(sink.attempts)
            for _ in range(20):
                sink.tick()
                sink.push(event("result"))
            self.assertEqual(len(sink.attempts), count)
            self.assertLessEqual(sink._fail, 6)
            self.clock.advance(delay)
            sink.tick()
        sink.online = True
        self.drain(sink)
        self.assertEqual([p["status"] for p in sink.delivered], ["done"])
        self.assertEqual((sink._fail, sink._next_try), (0, 0))

    def test_sender_exception_retained_and_retried(self):
        sink = ControlledSender()
        sink.raise_error = True
        sink.push(event("ask"))
        self.assertEqual(len(sink._held), 1)
        sink.raise_error = False
        self.drain(sink)
        self.assertEqual(sink.delivered[0]["status"], "waiting")

    def test_failed_regular_state_retries_on_backoff_not_long_throttle(self):
        sink = ControlledSender(throttle=1000)
        sink.online = False
        sink.push(event("user"))
        sink.push(event("tool", text="latest"))
        self.assertEqual(sink.next_wake(self.clock.now), 2)
        sink.online = True
        self.clock.advance(2)
        sink.tick()
        self.assertEqual([p["message"] for p in sink.delivered], ["latest"])
        self.assertFalse(sink._held)

    def test_partial_batch_failure_keeps_all_unsent_sessions(self):
        sink = ControlledSender(throttle=1)
        sink.push(event("user", "first"))
        for i in range(7):
            sink.push(event(session=str(i)))
        send = sink._send

        def fail_second(data):
            if len(sink.delivered) == 2:
                return False
            return send(data)

        self.clock.advance(1)
        with patch.object(sink, "_send", side_effect=fail_second):
            sink.tick()
        self.assertEqual(len(sink._held), 6)
        self.assertNotIn(("Qoder", "0"), sink._held)
        self.assertEqual(sink.next_wake(self.clock.now), 2)
        self.drain(sink)
        self.assertEqual(len(sink.delivered), 8)
        self.assertEqual(len(sink.rows), 8)

    def test_exit_clears_only_target_and_precedes_restarted_session(self):
        sink = ControlledSender(throttle=10)
        sink.online = False
        sink.push(event("user", "old"))
        sink.push(event("ask", "same", "workbuddy"))
        sink.push(event("app_exit", text=""))
        sink.push(event("result", "new"))
        self.assertNotIn(("Qoder", "old"), sink._held)
        self.assertIn(("WorkBuddy", "same"), sink._held)
        self.assertIn(("Qoder", None), sink._held)
        sink.online = True
        self.drain(sink)
        self.assertNotIn(("Qoder", "old"), sink.rows)
        self.assertEqual(sink.rows[("Qoder", "new")]["status"], "done")
        self.assertEqual(sink.rows[("WorkBuddy", "same")]["status"], "waiting")
        sync_index = next(i for i, p in enumerate(sink.delivered) if p.get("type") == "sync")
        new_index = next(i for i, p in enumerate(sink.delivered) if p.get("task_id") == "new")
        self.assertLess(sync_index, new_index)

    def test_failed_done_superseded_by_next_turn(self):
        sink = ControlledSender(throttle=0)
        sink.online = False
        sink.push(event("result", turn="old"))
        sink.push(event("user", turn="new"))
        sink.online = True
        self.drain(sink)
        self.assertEqual([p["status"] for p in sink.delivered], ["running"])

    def test_capacity_preserves_critical_over_regular_flood(self):
        sink = ControlledSender(max_pending=3)
        sink.online = False
        sink.push(event("ask", "waiting"))
        sink.push(event("result", "done"))
        for i in range(5000):
            sink.push(event(session=str(i)))
            self.assertLessEqual(len(sink._held), 3)
        self.assertIn(("Qoder", "waiting"), sink._held)
        self.assertIn(("Qoder", "done"), sink._held)
        self.assertEqual(sink.critical_evicted, 0)
        self.assertEqual(len(sink.attempts), 1)
        self.assertGreater(sink.evicted, 0)
        sink.online = True
        self.drain(sink)
        self.assertEqual(sink.rows[("Qoder", "waiting")]["status"], "waiting")
        self.assertEqual(sink.rows[("Qoder", "done")]["status"], "done")

    def test_all_critical_overflow_warns_and_counts(self):
        sink = ControlledSender(max_pending=2)
        sink.online = False
        sink.push(event("ask", "a"))
        sink.push(event("result", "b"))
        sink.push(event("result", "a"))
        self.assertEqual(sink.evicted, 0)
        sink.push(event(session="ordinary"))
        self.assertNotIn(("Qoder", "ordinary"), sink._held)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for i in range(10):
                sink.push(event("error", str(i)))
        self.assertEqual(len(caught), 1)
        self.assertIn("critical state evicted", str(caught[0].message))
        self.assertEqual(sink.critical_evicted, 10)
        self.assertEqual(len(sink._held), 2)
        self.assertEqual(list(sink._held), [("Qoder", "8"), ("Qoder", "9")])

    def test_exit_barrier_survives_capacity_pressure(self):
        sink = ControlledSender(max_pending=2)
        sink.online = False
        sink.push(event("app_exit"))
        sink.push(event("user", "new"))
        sink.push(event("result", "other", app="workbuddy"))
        self.assertIn(("Qoder", None), sink._held)
        self.assertNotIn(("Qoder", "new"), sink._held)
        sink.online = True
        self.drain(sink)
        self.assertEqual(sink.delivered[0]["type"], "sync")

    def test_wire_task_id_is_also_queue_identity(self):
        sink = ControlledSender()
        sink.online = False
        sink.push(event("tool", "x" * 64 + "old"))
        sink.push(event("result", "x" * 64 + "new"))
        self.assertEqual(len(sink._held), 1)
        sink.online = True
        self.drain(sink)
        self.assertEqual([p["status"] for p in sink.delivered], ["done"])


class TerminalCases:
    """同一组契约分别验证独立 Sink 和 Engine -> Sink，不使用真实应用数据。"""
    USE_ENGINE = False
    LATE_KINDS = ("answer", "tool", "tool_done", "edit", "status", "think",
                  "user", "plan", "ask", "denied", "error", "stalled", "result")

    def setUp(self):
        super().setUp()
        self.sink = ControlledSender(throttle=0)
        self.eng = self.engine(self.sink) if self.USE_ENGINE else None
        self.barriers = [self.sink._terminal_turns]
        if self.eng:
            self.barriers.append(self.eng._terminal_turns)

    def push(self, *steps):
        if self.eng:
            return self.feed(self.eng, *steps)
        for step in steps:
            self.sink.push(step)

    def test_offline_done_survives_late_running_and_duplicate_done(self):
        self.sink.online = False
        self.push(event("result", text="original done"))
        pending = self.sink._held[("Qoder", "S")]
        for kind in self.LATE_KINDS:
            self.push(event(kind, text="late " + kind))
        self.assertIs(self.sink._held[("Qoder", "S")], pending)
        self.assertEqual(len(self.sink.attempts), 1)
        self.sink.online = True
        self.drain(self.sink)
        self.assertEqual([p["message"] for p in self.sink.delivered], ["original done"])
        self.assertEqual(pending["turn"], "T")
        self.assertIs(pending["terminal"], True)

    def test_sent_done_never_reopens_or_refreshes_duplicate(self):
        self.push(event("result", text="original done"))
        self.clock.advance(60)  # 超过普通文本去重窗口，终态仍应关闭。
        self.push(*(event(k, text="late " + k) for k in self.LATE_KINDS))
        explicit = event("status")
        explicit.meta["terminal"] = True
        self.push(explicit)
        self.assertEqual(len(self.sink.delivered), 1)
        self.assertFalse(self.sink._held)
        if self.eng:
            state = self.eng._sessions[("qoder", "S")]
            self.assertEqual((state["kind"], state["at"]), ("result", 1000))
            self.assertEqual(self.eng.adapters["qoder"].last_activity, 1000)

    def test_new_turn_survives_late_old_turn_even_while_offline(self):
        self.sink.online = False
        self.push(event("result", turn="old"), event("user", turn="new"),
                  event("answer", turn="old"), event("result", turn="old"))
        self.sink.online = True
        self.drain(self.sink)
        self.assertEqual([(p["turn"], p["status"]) for p in self.sink.delivered],
                         [("new", "running")])
        self.push(event("result", turn="new"), event("tool", turn="old"),
                  event("user", turn="third"))
        self.assertEqual(self.sink.delivered[-1]["turn"], "third")
        self.assertEqual(self.sink.delivered[-1]["status"], "running")

    def test_late_stale_turn_without_terminal_cannot_override_new_turn(self):
        """旧轮可能根本没等到终态（被取消/被截断），补写时不能顶掉新轮。"""
        self.push(event("tool", turn="old", text="旧轮在跑"),
                  event("user", turn="new", text="新一轮"),
                  event("tool", turn="old", text="旧轮补写"))
        self.assertEqual([(p["turn"], p["status"]) for p in self.sink.delivered],
                         [("old", "running"), ("new", "running")])
        if self.eng:
            state = self.eng._sessions[("qoder", "S")]
            self.assertEqual((state["turn"], state["text"]), ("new", "新一轮"))

    def test_stale_turn_terminal_is_recorded_but_not_projected(self):
        self.push(event("tool", turn="old"), event("user", turn="new"),
                  event("result", turn="old", text="旧轮完成"))
        self.assertEqual([(p["turn"], p["status"]) for p in self.sink.delivered],
                         [("old", "running"), ("new", "running")])
        self.push(event("tool", turn="old", text="旧轮又动"), event("result", turn="old"))
        self.assertEqual(len(self.sink.delivered), 2)
        self.assertFalse(self.sink._held)

    def test_tool_error_and_non_boolean_terminal_allow_recovery(self):
        for marker in (None, False, "false", "true", 1):
            with self.subTest(marker=marker):
                sid = repr(marker)
                failed = event("error", session=sid)
                if marker is not None:
                    failed.meta["terminal"] = marker
                before = len(self.sink.delivered)
                self.push(event("tool", session=sid), failed,
                          event("tool", session=sid), event("answer", session=sid),
                          event("result", session=sid))
                rows = self.sink.delivered[before:]
                self.assertEqual([p["status"] for p in rows],
                                 ["running", "error", "running", "running", "done"])
                self.assertIs(rows[1]["terminal"], False)
                self.assertIs(rows[-1]["terminal"], True)

    def test_explicit_terminal_bypasses_noise_and_throttle_then_closes(self):
        self.sink.throttle = 1000
        for kind, text in (("status", ""), ("answer", "heartbeat"), ("error", "")):
            with self.subTest(kind=kind):
                self.push(event("user", session=kind))
                terminal = event(kind, session=kind, text=text)
                terminal.meta.update(terminal=True, phase="idle", outcome="cancelled")
                self.push(terminal)
                self.assertEqual(self.sink.rows[("Qoder", kind)]["phase"], "cancelled")
                self.assertIs(self.sink.rows[("Qoder", kind)]["terminal"], True)
                before = len(self.sink.delivered)
                self.push(event("tool", session=kind), event("result", session=kind))
                self.drain(self.sink)
                self.assertEqual(len(self.sink.delivered), before)

    def test_session_and_app_isolation(self):
        self.push(event("result"), event("tool", session="other"),
                  event("answer", app="workbuddy"), event("tool"))
        self.assertEqual(len(self.sink.delivered), 3)
        self.assertEqual(self.sink.rows[("Qoder", "S")]["status"], "done")
        self.assertEqual(self.sink.rows[("Qoder", "other")]["status"], "running")
        self.assertEqual(self.sink.rows[("WorkBuddy", "S")]["status"], "running")

    def test_empty_turn_never_locks_new_request(self):
        for turn in ("", None, "  "):
            for kind in ("result", "status", "error"):
                with self.subTest(turn=turn, kind=kind):
                    terminal = event(kind, turn=turn)
                    terminal.meta["terminal"] = True
                    before = len(self.sink.delivered)
                    self.push(terminal, event("user", turn=turn), terminal,
                              event("tool", turn=turn))
                    self.assertEqual(len(self.sink.delivered), before + 4)
                    self.assertEqual(self.sink.delivered[-1]["status"], "running")
                    self.assertIsInstance(self.sink.delivered[-1]["turn"], str)
                    self.assertIs(self.sink.delivered[-1]["terminal"], False)
        self.assertTrue(all(not b.closed for b in self.barriers))

    def test_offline_empty_turn_done_can_be_replaced_by_new_request(self):
        self.sink.online = False
        self.push(event("result", turn=""), event("user", turn=""))
        self.sink.online = True
        self.drain(self.sink)
        self.assertEqual([p["status"] for p in self.sink.delivered], ["running"])
        self.assertEqual(self.sink.delivered[0]["turn"], "")

    def test_turn_string_is_preserved_and_numeric_identity_normalized(self):
        turn = "轮次-" + "x" * 100
        self.push(event("result", turn=turn), event("tool", turn=turn))
        self.assertEqual(self.sink.delivered[-1]["turn"], turn)
        self.push(event("result", turn=123), event("tool", turn="123"))
        self.assertEqual(self.sink.delivered[-1]["turn"], "123")
        self.assertEqual(len(self.sink.delivered), 2)

    def test_exit_clears_only_app_and_allows_same_identity_restart(self):
        self.sink.online = False
        self.push(event("result"), event("result", app="workbuddy"),
                  event("app_exit"), event("tool"), event("tool", app="workbuddy"))
        self.sink.online = True
        self.drain(self.sink)
        self.assertEqual(self.sink.rows[("Qoder", "S")]["status"], "running")
        self.assertEqual(self.sink.rows[("WorkBuddy", "S")]["status"], "done")
        sync = next(i for i, p in enumerate(self.sink.delivered) if p.get("type") == "sync")
        restart = next(i for i, p in enumerate(self.sink.delivered)
                       if p.get("platform") == "Qoder" and p.get("task_id") == "S")
        self.assertLess(sync, restart)
        self.push(event("result"), event("tool"))
        self.assertEqual(self.sink.rows[("Qoder", "S")]["status"], "done")

    def test_terminal_cache_bounded_and_evicted_turn_can_run(self):
        limit = self.barriers[0].LIMIT
        self.assertIn(limit, (512, 1024))
        for i in range(limit + 20):
            self.push(event("result", turn=str(i)))
            self.assertTrue(all(len(b.closed) <= limit for b in self.barriers))
        before = len(self.sink.delivered)
        self.push(event("tool", turn=str(limit + 19)))
        self.assertEqual(len(self.sink.delivered), before)
        self.push(event("tool", turn="0"))
        self.assertEqual(self.sink.delivered[-1]["status"], "running")

    def test_evicted_cache_still_preserves_pending_terminal(self):
        self.sink.online = False
        self.push(event("result", text="original done"))
        for i in range(self.barriers[0].LIMIT):
            self.push(event("result", session="other", turn=str(i)))
        self.assertEqual(len(self.sink._held), 2)
        self.assertTrue(all(not any(k[1] == "S" for k in b.closed)
                            for b in self.barriers))
        self.push(event("tool", text="late running"))
        self.assertEqual(self.sink._held[("Qoder", "S")]["message"], "original done")
        self.sink.online = True
        self.drain(self.sink)
        self.push(event("tool"), event("result"))
        delivered = [p for p in self.sink.delivered if p["task_id"] == "S"]
        self.assertEqual([p["message"] for p in delivered], ["original done"])

    def test_duplicate_does_not_extend_terminal_ttl(self):
        self.push(event("result"))
        ttl = self.barriers[0].TTL
        self.clock.advance(ttl - 1)
        self.push(event("result"), event("tool"))
        self.assertEqual(len(self.sink.delivered), 1)
        self.clock.advance(2)
        self.push(event("tool"))
        self.assertEqual(self.sink.delivered[-1]["status"], "running")
        self.assertTrue(all(not b.closed for b in self.barriers))

    def test_expired_cache_still_preserves_pending_done_and_rearms_on_send(self):
        self.sink.online = False
        self.push(event("result", text="original done"))
        self.clock.advance(self.barriers[0].TTL + 1)
        self.push(event("tool", text="late running"))
        self.assertEqual(self.sink._held[("Qoder", "S")]["message"], "original done")
        self.assertTrue(all(not b.closed for b in self.barriers))
        self.sink.online = True
        self.drain(self.sink)
        self.push(event("answer"), event("result"))
        self.assertEqual([p["message"] for p in self.sink.delivered], ["original done"])


class SinkTerminalTests(TerminalCases, ClockCase):
    pass


class EngineTerminalTests(TerminalCases, ClockCase):
    USE_ENGINE = True


class EngineTests(ClockCase):
    @patch("core.engine.discover.is_running_cached", return_value=True)
    def test_engine_fences_without_sink_and_never_stalls_closed_turn(self, alive):
        engine = Engine()
        engine.adapters["qoder"] = FakeAdapter()
        for kind in ("result", "status", "error"):
            terminal = event(kind, session=kind, text="")
            terminal.meta["terminal"] = True
            out = engine._clean([event("tool", session=kind), terminal,
                                 event("answer", session=kind)], self.clock.now)
            self.assertEqual([e.kind for e in out], ["tool", kind])
            state = engine._sessions[("qoder", kind)]
            self.assertFalse(engine._note_session(event("tool", session=kind), self.clock.now + 1))
            self.assertIs(engine._sessions[("qoder", kind)], state)
        self.clock.advance(IDLE_GRACE + 1)
        with patch.object(engine, "_emit_housekeeping") as emit:
            engine.reap_dead(self.clock.now)
        emit.assert_not_called()

    @patch("core.engine.discover.is_running_cached", return_value=False)
    def test_housekeeping_exit_resets_engine_and_sink_terminal_barriers(self, alive):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        self.feed(engine, event("result"))
        self.clock.advance(21)
        engine.reap_dead(self.clock.now)
        self.assertFalse(engine._terminal_turns.closed)
        self.assertFalse(sink._terminal_turns.closed)
        self.assertEqual(sink.delivered[-1]["type"], "sync")
        self.assertEqual(len(self.feed(engine, event("tool"))), 1)
        self.assertEqual(sink.delivered[-1]["status"], "running")

    def test_empty_critical_events_and_noise_bypass(self):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        kinds = ("ask", "denied", "result", "error", "stalled")
        steps = [event(k, k, text=" \t") for k in kinds]
        out = self.feed(engine, *steps, event(text=""), event(text="heartbeat"),
                        event("ask", "noise", text="main.js:42 permission"))
        self.assertEqual(len(out), 6)
        self.assertEqual(len(sink.rows), 6)
        self.assertEqual(engine.suppressed, 2)
        self.feed(engine, event("app_exit", text=""))
        self.assertFalse(sink.rows)
        self.assertFalse(engine._sessions)

    def test_dedupe_is_session_and_turn_scoped(self):
        engine = self.engine(ControlledSender())
        steps = [event(session="a", turn="1"), event(session="b", turn="1"),
                 event(session="a", turn="2"), event(session="a", turn="2")]
        out = self.feed(engine, *steps)
        self.assertEqual(len(out), 3)
        self.assertEqual(engine.suppressed, 1)

    def test_waiting_resume_is_not_suppressed_by_old_dedupe_key(self):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        self.feed(engine, event("tool"), event("ask"), event("tool"))
        self.assertEqual([p["status"] for p in sink.delivered],
                         ["running", "waiting", "running"])

    @patch("core.engine.discover.is_running_cached", return_value=True)
    def test_only_idle_active_sessions_stall(self, alive):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        self.feed(engine, event("tool", "old"), event("think", "fresh"),
                  *(event(k, k, text="") for k in ("ask", "result", "error", "denied")))
        self.clock.advance(100)
        self.feed(engine, event("tool", "fresh"))
        self.clock.advance(51)
        engine.reap_dead(self.clock.now)
        stalled = [p for p in sink.delivered if p.get("phase") == "stalled"]
        self.assertEqual([p["task_id"] for p in stalled], ["old"])
        for session, status in (("ask", "waiting"), ("result", "done"),
                                ("error", "error"), ("denied", "error")):
            self.assertEqual(sink.rows[("Qoder", session)]["status"], status)
        self.clock.advance(10)
        engine.reap_dead(self.clock.now)
        self.assertEqual(len(sink.delivered), 8)
        self.clock.advance(20)
        engine.reap_dead(self.clock.now)
        self.assertEqual(len(sink.delivered), 9)
        self.feed(engine, event("ask", "old"))
        self.clock.advance(IDLE_GRACE + 1)
        engine.reap_dead(self.clock.now)
        self.assertEqual(sink.rows[("Qoder", "old")]["status"], "waiting")
        self.assertEqual(sink.rows[("Qoder", "fresh")]["phase"], "stalled")

    @patch("core.engine.discover.is_running_cached", return_value=True)
    def test_duplicate_activity_refreshes_session_timer(self, alive):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        self.feed(engine, event())
        self.clock.advance(40)
        self.assertFalse(self.feed(engine, event()))
        self.clock.advance(130)
        engine.reap_dead(self.clock.now)
        self.assertFalse(any(p.get("phase") == "stalled" for p in sink.delivered))
        self.clock.advance(21)
        engine.reap_dead(self.clock.now)
        self.assertEqual(sink.delivered[-1]["phase"], "stalled")

    @patch("core.engine.discover.is_running_cached", return_value=True)
    def test_cancelled_and_unconfirmed_activity_do_not_stall(self, alive):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        cancelled = event("status", "cancelled")
        cancelled.meta.update(phase="idle", terminal=True, outcome="cancelled")
        observed = event("status", "observed")
        observed.meta.update(phase="idle")
        self.feed(engine, cancelled, observed)
        self.assertEqual(sink.rows[("Qoder", "cancelled")]["phase"], "cancelled")
        self.clock.advance(IDLE_GRACE + 1)
        engine.reap_dead(self.clock.now)
        self.assertEqual(len(sink.delivered), 2)
        self.assertFalse(self.feed(engine, event("tool", "cancelled")))
        self.feed(engine, event("tool", "cancelled", turn="next"))
        self.clock.advance(IDLE_GRACE + 1)
        engine.reap_dead(self.clock.now)
        self.assertEqual(sink.rows[("Qoder", "cancelled")]["phase"], "stalled")

    @patch("core.engine.discover.is_running_cached", return_value=True)
    def test_metadata_waiting_is_not_stalled(self, alive):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        step = event("status")
        step.meta["ask"] = True
        self.feed(engine, step)
        self.clock.advance(IDLE_GRACE + 1)
        engine.reap_dead(self.clock.now)
        self.assertEqual(len(sink.delivered), 1)

    @patch("core.engine.discover.is_running_cached", side_effect=lambda key: key != "qoder")
    def test_exit_cleans_only_target_session_and_dedupe_state(self, alive):
        sink = ControlledSender(throttle=0)
        engine = self.engine(sink)
        engine.adapters["workbuddy"] = FakeAdapter("workbuddy")
        engine.adapters["workbuddy"].events = [event("ask", app="workbuddy")]
        self.feed(engine, event("result", "done"), event("tool", "active"))
        self.clock.advance(21)
        engine.reap_dead(self.clock.now)
        self.assertTrue(all(k[0] != "qoder" for k in engine._sessions))
        self.assertTrue(all(k[0] != "qoder" for k in engine._seen))
        self.assertIn(("workbuddy", "S"), engine._sessions)
        self.assertEqual(list(sink.rows), [("WorkBuddy", "S")])
        count = len(sink.delivered)
        self.clock.advance(10)
        engine.reap_dead(self.clock.now)
        self.assertEqual(len(sink.delivered), count)
        self.feed(engine, event("tool", "active"))
        self.assertFalse(engine.adapters["qoder"].reaped)
        self.assertIn(("Qoder", "active"), sink.rows)

    def test_session_and_dedupe_cache_capacity(self):
        engine = Engine()
        steps = [event("tool", str(i)) for i in range(MAX_SESSIONS + 300)]
        engine._clean(steps, self.clock.now)
        self.assertEqual(len(engine._sessions), MAX_SESSIONS)
        self.assertEqual(len(engine._seen), 500)
        self.assertEqual(engine.session_evicted, 300)
        self.assertNotIn(("qoder", "0"), engine._sessions)
        engine._clean([event("app_exit", text="")], self.clock.now)
        self.assertFalse(engine._sessions)
        self.assertFalse(engine._seen)

    def test_idle_poll_ticks_and_legacy_sink_still_works(self):
        class LegacySink:
            def __init__(self):
                self.events = []

            def push(self, step):
                self.events.append(step)

        sink = ControlledSender(throttle=1)
        engine = self.engine(sink)
        legacy = LegacySink()
        engine.sinks.append(legacy)
        self.feed(engine, event("user"), event("tool", text="last"))
        self.clock.advance(1)
        self.assertFalse(engine.poll(self.clock.now))
        self.assertEqual(sink.delivered[-1]["message"], "last")
        self.assertEqual(len(legacy.events), 2)
        self.assertEqual(engine.dropped, 0)

    def test_tick_failure_does_not_block_other_sinks(self):
        class BrokenSink:
            def push(self, step):
                raise OSError("push failed")

            def tick(self):
                raise OSError("tick failed")

        sink = ControlledSender(throttle=1)
        engine = self.engine(sink)
        engine.sinks.insert(0, BrokenSink())
        self.feed(engine, event("user"), event())
        self.clock.advance(1)
        engine.poll(self.clock.now)
        self.assertFalse(sink._held)
        self.assertEqual(engine.dropped, 4)

    def test_next_wake_respects_retry_throttle_and_idle_cap(self):
        sink = ControlledSender(throttle=0.7)
        engine = Engine()
        engine.sinks = [sink]
        self.assertEqual(engine.next_wake(self.clock.now, cap=20), 20)
        sink.push(event("user"))
        sink.push(event())
        self.assertAlmostEqual(engine.next_wake(self.clock.now, cap=20), 0.71)
        sink.online = False
        sink.push(event("result"))
        self.assertAlmostEqual(engine.next_wake(self.clock.now, cap=20), 2.01)
        self.assertEqual(engine.next_wake(self.clock.now), 2.0)


class LocalHTTPTests(ClockCase):
    def test_http_asking_recovery_and_terminal_keep_sessions_isolated(self):
        with HTTPReceiver() as receiver:
            sink = PetHubSink(url=receiver.url, throttle=0, timeout=0.3)
            for kind in ("think", "tool", "status"):
                asking = event(kind, session=kind, app="baidu_dazi")
                asking.meta.update(phase="asking")
                sink.push(asking)
                self.clock.advance(5)
                sink.push(asking)
            self.assertEqual([p["status"] for p in receiver.delivered], ["waiting"] * 6)
            self.assertTrue(all(p["ask"] is True for p in receiver.delivered))
            sink.push(event("think", session="think", app="baidu_dazi"))
            row = receiver.rows[(APP_NAMES["baidu_dazi"], "think")]
            self.assertEqual((row["status"], row["phase"]), ("thinking", "thinking"))
            self.assertFalse(row.get("ask", False))
            done = event("result", session="think", app="baidu_dazi")
            done.meta.update(phase="asking", ask=True)
            sink.push(done)
            late = event("think", session="think", app="baidu_dazi")
            late.meta.update(phase="asking")
            sink.push(late)
            row = receiver.rows[(APP_NAMES["baidu_dazi"], "think")]
            self.assertEqual((row["status"], row["phase"]), ("done", "done"))
            self.assertFalse(row.get("ask", False))
            for session in ("tool", "status"):
                row = receiver.rows[(APP_NAMES["baidu_dazi"], session)]
                self.assertEqual(row["status"], "waiting")
                self.assertIs(row["ask"], True)
            self.assertEqual(len(receiver.delivered), 8)

    def test_http_engine_delivers_done_without_old_running_after_it(self):
        with HTTPReceiver() as receiver:
            sink = PetHubSink(url=receiver.url, throttle=10, timeout=0.3)
            engine = self.engine(sink)
            self.feed(engine, event("user"), event("tool"), event("result", text=""),
                      event("answer"), event("tool"), event("result"))
            self.clock.advance(20)
            self.feed(engine, event("status"), event("edit"))
            self.assertEqual([p["status"] for p in receiver.delivered], ["running", "done"])
            self.assertEqual(receiver.rows[("Qoder", "S")]["title"], "Qoder")
            self.assertEqual(receiver.delivered[-1]["turn"], "T")
            self.assertIs(receiver.delivered[-1]["terminal"], True)
            self.feed(engine, event("user", turn="next"), event("result", turn="next"))
            self.assertEqual(receiver.delivered[-1]["turn"], "next")

    def test_http_connection_refused_recovers_in_idle_main_loop(self):
        with HTTPReceiver(start=False) as receiver:
            sink = PetHubSink(url=receiver.url, timeout=0.3)
            engine = self.engine(sink)
            engine._reap_at = float("inf")
            self.feed(engine, event("user"), event("tool"), event("result", text=""))
            self.assertEqual(sink._fail, 1)
            self.assertEqual(len(sink._held), 1)
            self.assertFalse(receiver.delivered)
            self.feed(engine, event("answer"), event("tool"), event("edit"),
                      event("status"), event("result"))
            self.assertEqual(sink._held[("Qoder", "S")]["status"], "done")
            receiver.start()
            with patch("time.sleep", self.clock.sleep):
                run(engine, stop_after=2.1)
            self.assertEqual([p["status"] for p in receiver.delivered], ["done"])
            self.assertFalse(sink._held)
            self.assertLessEqual(len(self.clock.sleeps), 3)
            self.assertTrue(all(delay >= 1 for delay in self.clock.sleeps))

    def test_http_503_exit_retry_preserves_other_platform_and_restart(self):
        with HTTPReceiver() as receiver:
            sink = PetHubSink(url=receiver.url, throttle=10, timeout=0.3)
            sink.push(event("user", "old"))
            sink.push(event("tool", "old"))
            sink.push(event("ask", "same", "workbuddy"))
            receiver.fail = True
            sink.push(event("app_exit", text=""))
            sink.push(event("result", "new"))
            sink.push(event("result", "same", "workbuddy"))
            self.assertEqual(sink._fail, 1)
            receiver.fail = False
            self.drain(sink)
            self.assertNotIn(("Qoder", "old"), receiver.rows)
            self.assertEqual(receiver.rows[("Qoder", "new")]["status"], "done")
            self.assertEqual(receiver.rows[("WorkBuddy", "same")]["status"], "done")
            exits = [p for p in receiver.attempts if p.get("type") == "sync"]
            self.assertEqual(len(exits), 2)
            self.assertLess(next(i for i, p in enumerate(receiver.delivered)
                                 if p.get("type") == "sync"),
                            next(i for i, p in enumerate(receiver.delivered)
                                 if p.get("task_id") == "new"))

    def test_http_throttled_backlog_drains_in_idle_main_loop(self):
        with HTTPReceiver() as receiver:
            sink = PetHubSink(url=receiver.url, throttle=0.5, timeout=0.3)
            engine = self.engine(sink)
            engine._reap_at = float("inf")
            self.feed(engine, event("user", "first"),
                      *(event(session=str(i)) for i in range(11)))
            with patch("time.sleep", self.clock.sleep):
                run(engine, stop_after=2)
            self.assertEqual(len(receiver.rows), 12)
            self.assertEqual(len(receiver.delivered), 12)
            self.assertFalse(sink._held)
            self.assertTrue(all(delay >= 0.49 for delay in self.clock.sleeps))

    @patch("core.engine.discover.is_running_cached", return_value=True)
    def test_http_empty_waiting_done_error_denied_survive_reaping(self, alive):
        with HTTPReceiver() as receiver:
            sink = PetHubSink(url=receiver.url, throttle=0, timeout=0.3)
            engine = self.engine(sink)
            self.feed(engine, *(event(k, k, text="")
                                for k in ("ask", "result", "error", "denied")))
            self.clock.advance(IDLE_GRACE + 1)
            engine.reap_dead(self.clock.now)
            self.assertEqual(len(receiver.delivered), 4)
            self.assertEqual([receiver.rows[("Qoder", k)]["status"]
                              for k in ("ask", "result", "error", "denied")],
                             ["waiting", "done", "error", "error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
