# -*- coding: utf-8 -*-
"""Codex fixtures (temporary files only); optional bounded known-test replay.

python tools/test_codex.py
python tools/test_codex.py --live-known <explicit-rollout-jsonl>
The optional replay is read-only, requires PETHUB-CODEX-OK, and prints only
session/turn IDs, counts and outcomes. It never opens SQLite or app settings.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.chat_apps import (ChatGPTAdapter, ChromiumChatAdapter, CodexLogReader,
                                CodexRolloutReader, CodexRollouts, codex_kind)
from core.model import K_ANSWER, K_ERROR, K_RESULT, K_STATUS, K_TOOL, K_USER, P_IDLE

BASE = 1_793_181_600.0
MARKER = "PETHUB-CODEX-OK"


def row(typ, sub=None, ts=BASE + 1, **payload):
    if sub is not None:
        payload["type"] = sub
    return {"timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "type": typ, "payload": payload}


def meta(sid="session-a"):
    return row("session_meta", id=sid)


def start(tid="turn-1", **kw):
    return row("event_msg", "task_started", turn_id=tid, **kw)


def terminal(sub="task_complete", tid="turn-1", **kw):
    return row("event_msg", sub, turn_id=tid, **kw)


class CodexFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pethub-codex-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "rollout-fixture.jsonl"

    def write(self, *records, path=None, append=False):
        with (path or self.path).open("a" if append else "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")

    def reader(self, *records):
        self.write(*records)
        return CodexRolloutReader(str(self.path), start_at_end=False)

    def test_verified_desktop_cancel_schema(self):
        rd = self.reader(meta(), start(),
                         row("response_item", "message", role="user", content=[{"text": "context"}]),
                         row("event_msg", "item_completed", turn_id="turn-1", item={
                             "type": "UserMessage", "id": "msg-1",
                             "content": [{"type": "text", "text": MARKER}]}),
                         terminal("turn_aborted", reason="interrupted"))
        events = rd.poll()
        self.assertEqual([x[0] for x in events], [K_STATUS, K_USER, K_STATUS])
        self.assertTrue(all(x[3]["session"] == "session-a" and x[3]["turn"] == "turn-1"
                            for x in events))
        self.assertEqual(events[-1][3]["outcome"], "cancelled")
        self.assertEqual(events[-1][3]["phase"], P_IDLE)
        self.assertEqual(rd.poll(), [])

    def test_completion_error_cancel_are_explicit_and_keyed(self):
        rd = self.reader(meta(), start(), terminal(), start("turn-2"),
                         terminal("error", "turn-2", message="Concurrency limit exceeded for user"),
                         start("turn-3"), terminal("turn_aborted", "turn-3", reason="interrupted"),
                         terminal(), terminal("task_complete", "turn-2"))
        events = rd.poll()
        ends = [e for e in events if e[3]["terminal"]]
        self.assertEqual([e[0] for e in ends], [K_RESULT, K_ERROR, K_STATUS])
        self.assertEqual([e[3]["turn"] for e in ends], ["turn-1", "turn-2", "turn-3"])
        self.assertIn("Concurrency limit", ends[1][1])

    def test_retry_error_is_not_terminal(self):
        rd = self.reader(meta(), start(), terminal("error", will_retry=True, message="retrying"), terminal())
        events = rd.poll()
        self.assertFalse(events[1][3]["terminal"])
        self.assertEqual(events[-1][0], K_RESULT)

    def test_sessions_with_same_turn_are_isolated(self):
        first = self.reader(meta("a"), start("same"), terminal(tid="same"))
        path = self.root / "rollout-b.jsonl"
        self.write(meta("b"), start("same"), terminal("turn_aborted", "same"), path=path)
        second = CodexRolloutReader(str(path), start_at_end=False)
        a, b = first.poll(), second.poll()
        self.assertEqual(a[-1][3]["outcome"], "completed")
        self.assertEqual(b[-1][3]["outcome"], "cancelled")
        self.assertEqual(a[-1][3]["session"], "a")
        self.assertEqual(b[-1][3]["session"], "b")
        self.assertFalse(first.covers("b", "same"))

    def test_late_terminal_does_not_clear_new_turn(self):
        rd = self.reader(meta(), start("old"), start("new"), terminal(tid="old"),
                         row("event_msg", "agent_message", message=MARKER), terminal(tid="new"))
        events = rd.poll()
        self.assertEqual(events[-2][3]["turn"], "new")
        self.assertEqual(events[-1][3]["turn"], "new")

    def test_actual_tool_calls_only_no_rpc_or_context_messages(self):
        rd = self.reader(meta(), start(),
                         row("response_item", "message", role="assistant", content=[{"text": "done"}]),
                         row("response_item", "message", role="user", content=[{"text": "injected"}]),
                         row("event_msg", "item_completed", item={"type": "CommandExecution"}),
                         row("response_item", "function_call", name="shell", call_id="call-1", arguments="SECRET"),
                         row("response_item", "function_call", name="shell", call_id="call-1"),
                         row("response_item", "custom_tool_call", name="apply_patch", call_id="call-2"),
                         row("response_item", "function_call_output", call_id="call-1", output="SECRET"),
                         row("event_msg", "agent_message", message=MARKER))
        events = rd.poll()
        self.assertEqual([e[0] for e in events], [K_STATUS, K_TOOL, K_TOOL, K_ANSWER])
        self.assertNotIn("SECRET", repr(events))
        self.assertFalse(any(e[3]["terminal"] for e in events))
        for rpc in ("thread/start", "turn/turn_input", "tool/exec", "shell/write", "interrupt", "thread/archive", "error"):
            self.assertEqual(codex_kind(rpc, "ERROR"), K_STATUS)

    def test_missing_or_conflicting_identity_never_invents_session_turn(self):
        rd = self.reader(start(), terminal(), meta(), terminal(tid=""),
                         row("response_item", "function_call", name="shell"),
                         start(), terminal(thread_id="other"))
        self.assertEqual([e[0] for e in rd.poll()], [K_STATUS])

    def test_stale_boot_no_replay_but_recovers_live_turn(self):
        self.write(meta(), start(), row("event_msg", "user_message", message=MARKER))
        rd = CodexRolloutReader(str(self.path))
        self.assertEqual(rd.poll(), [])
        self.write(terminal("turn_aborted", reason="interrupted"), append=True)
        events = rd.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][3]["session"], "session-a")
        self.assertEqual(events[0][3]["turn"], "turn-1")
        self.assertEqual(rd.poll(), [])

    def test_stale_terminal_fence_and_later_new_turn(self):
        self.write(meta(), start(), terminal())
        rd = CodexRolloutReader(str(self.path))
        self.write(terminal(), row("event_msg", "agent_message", message="late"),
                   start("next"), terminal(tid="next"), append=True)
        events = rd.poll()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e[3]["turn"] == "next" for e in events))

    def test_partial_malformed_and_oversized_lines(self):
        rd = self.reader(meta(), start())
        rd.poll()
        end = json.dumps(terminal()) + "\n"
        with self.path.open("ab") as f:
            f.write(b"bad json\n[]\n{\"payload\": []}\n")
            f.write(b"x" * (rd.MAX_BYTES + 1) + b"\n")
            f.write(end[:20].encode())
        self.assertEqual(rd.poll(), [])
        self.assertEqual(rd.poll(), [])
        with self.path.open("ab") as f:
            f.write(end[20:].encode())
        self.assertEqual(rd.poll()[0][0], K_RESULT)

    def test_truncation_and_equal_size_replacement_do_not_reuse_identity(self):
        rd = self.reader(meta("a"), start(), terminal())
        rd.poll()
        self.write(meta("b"), start(), terminal())
        os.utime(self.path, (BASE + 100, BASE + 100))
        events = rd.poll()
        self.assertEqual(events[-1][3]["session"], "b")
        self.write(terminal())
        self.assertEqual(rd.poll(), [])

    def test_turn_state_is_bounded(self):
        rd = self.reader(meta())
        rd.poll()
        for i in range(1000):
            rd._parse(json.dumps(start(str(i))))
            rd._parse(json.dumps(terminal(tid=str(i))))
        self.assertLessEqual(len(rd.states), rd.MAX_TURNS)

    def test_bounded_recent_discovery_and_new_file_incremental(self):
        day = self.root / "sessions" / datetime.fromtimestamp(BASE).strftime("%Y/%m/%d")
        day.mkdir(parents=True)
        for i in range(12):
            path = day / ("rollout-%02d.jsonl" % i)
            self.write(meta(str(i)), start(), terminal(), path=path)
            os.utime(path, (BASE - 10, BASE - 10))
        with patch("adapters.chat_apps.time.time", return_value=BASE):
            source = CodexRollouts([str(self.root)])
            self.assertLessEqual(len(source.active), source.MAX_FILES)
            self.assertEqual(source.poll(), [])
            fresh = day / "rollout-new.jsonl"
            self.write(meta("fresh"), start(ts=BASE + 2), terminal(ts=BASE + 2), path=fresh)
            os.utime(fresh, (BASE + 2, BASE + 2))
            source.discover()
            events = source.poll()
            self.assertEqual([e[0] for e in events], [K_STATUS, K_RESULT])
            self.assertTrue(all(e[3]["session"] == "fresh" for e in events))
            source.discover()
            self.assertEqual(source.poll(), [])
            self.assertLessEqual(len(source.readers), source.MAX_FILES * 2)

    def test_discovery_entry_budget_no_recursive_walk(self):
        count = [0]
        class Entries:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __iter__(self):
                for i in range(10000):
                    count[0] += 1
                    yield SimpleNamespace(name="irrelevant-%d" % i)
        with patch("adapters.chat_apps.os.scandir", return_value=Entries()), \
                patch("adapters.chat_apps.os.walk", side_effect=AssertionError("recursive walk")):
            source = CodexRollouts([str(self.root)])
        self.assertEqual(count[0], source.MAX_ENTRIES)
        self.assertEqual(source.active, [])

    def test_newly_discovered_old_records_are_not_replayed(self):
        rd = self.reader(meta(), start(ts=BASE - 20), terminal(ts=BASE - 10),
                         start("fresh", ts=BASE + 10), terminal(tid="fresh", ts=BASE + 10))
        rd.since = BASE
        self.assertTrue(all(e[3]["turn"] == "fresh" for e in rd.poll()))

    def adapter(self, opt_in=True):
        ad = ChatGPTAdapter({"roots": [], "exes": [], "codex_logs": opt_in})
        ad._discover_at = ad._raw_at = float("inf")
        ad.USE_WINDOWS = False
        return ad

    def test_opt_in_isolation_and_raw_sources_stay_coarse(self):
        ad = self.adapter(False)
        with patch.object(ChromiumChatAdapter, "setup", return_value=True), \
                patch.object(ad, "_setup_codex") as setup, \
                patch("adapters.chat_apps.CodexRollouts") as rollout:
            ad.setup()
        setup.assert_not_called()
        rollout.assert_not_called()
        self.assertIsNone(ad.rollouts)
        self.assertEqual(ad.emit(K_TOOL, "unverified", BASE).kind, K_STATUS)

    def test_rollout_preferred_trace_dedup_and_idle_cancel(self):
        rd = self.reader(meta(), start(), terminal("turn_aborted", reason="interrupted"))
        ad = self.adapter()
        source = Mock()
        source.active = [rd]
        source.poll.side_effect = rd.poll
        source.covers.side_effect = rd.covers
        ad.rollouts = source
        ad.codex = Mock()
        ad.codex.poll.return_value = [(K_STATUS, "turn/dispatch", BASE, {"session": "session-a", "turn": "turn-1"}),
                                      (K_STATUS, "thread/start", BASE, {})]
        events = ad.tick(BASE + 1)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.meta["evidence"] == "codex_rollout" for e in events))
        self.assertEqual(ad.phase, P_IDLE)
        self.assertEqual(ad.cur_session, "session-a")
        self.assertEqual(ad.cur_turn, "turn-1")

    def test_sqlite_fallback_rpc_is_limited_and_full_ids_attached(self):
        path = self.root / "logs_2.sqlite"
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.execute("create table logs(id integer primary key,ts real,level text,target text,feedback_log_body text)")
        con.commit()
        rd = CodexLogReader(str(path))
        self.addCleanup(rd.close)
        self.assertTrue(rd.open())
        for i, name in enumerate(("thread/start", "tool/exec", "turn/interrupt"), 1):
            con.execute("insert into logs values(?,?,?,?,?)", (i, BASE, "INFO", "codex_core::tools",
                        'otel.name="%s" thread_id="full-session-id" turn_id="full-turn-id"' % name))
        con.commit()
        events = rd.poll()
        self.assertEqual([e[0] for e in events], [K_STATUS] * 3)
        self.assertTrue(all(e[3]["session"] == "full-session-id" and e[3]["turn"] == "full-turn-id"
                            and e[3]["accuracy"] == "limited" and not e[3]["terminal"] for e in events))
        self.assertEqual(rd.poll(), [])
        with self.assertRaises(sqlite3.OperationalError):
            rd.con.execute("delete from logs")


def replay_known(path):
    """Explicit allowlisted test only; bounded read, no writes, no body output."""
    path = Path(path).resolve()
    if not path.name.startswith("rollout-") or path.suffix != ".jsonl":
        raise ValueError("Expected an explicit rollout JSONL path")
    before = path.stat()
    with path.open("rb") as f:
        data = f.read(CodexRolloutReader.MAX_BYTES + 1)
    if len(data) > CodexRolloutReader.MAX_BYTES:
        raise ValueError("Known-test file exceeds the bounded replay limit")
    records = [json.loads(line) for line in data.splitlines() if line.strip()]
    active, known = "", ""
    selected = []
    # Only the marker's turn is eligible, not any later activity in this file.
    for record in records:
        p = record.get("payload", {})
        if record.get("type") == "event_msg" and p.get("type") == "task_started":
            active = p.get("turn_id", "")
        if p.get("type") == "item_completed" and p.get("item", {}).get("type") == "UserMessage":
            if MARKER in json.dumps(p["item"], ensure_ascii=True):
                known = p.get("turn_id") or active
                break
        if p.get("type") == "user_message" and MARKER in str(p.get("message", "")):
            known = p.get("turn_id") or active
            break
    if not known:
        raise ValueError("No explicitly attributed known-test marker; refusing replay")
    rd = CodexRolloutReader(str(path), start_at_end=False)
    active = ""
    for record in records:
        p = record.get("payload", {})
        if record.get("type") == "session_meta":
            rd._parse(json.dumps(record))
        if record.get("type") == "event_msg" and p.get("type") == "task_started":
            active = p.get("turn_id", "")
        if active == known:
            selected.extend(rd._parse(json.dumps(record)))
    selected = [e for e in selected if e[3]["turn"] == known]
    ends = [e[3]["outcome"] for e in selected if e[3]["terminal"]]
    after = path.stat()
    result = {"session": rd.session, "turn": known, "counts": dict(Counter(e[0] for e in selected)),
              "terminal": ends[-1] if ends else "limited: no authoritative terminal",
              "persisted_error": any(e[0] == K_ERROR for e in selected),
              "stable_read": (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)}
    print(json.dumps(result, ensure_ascii=True))
    return result


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--live-known":
        replay_known(sys.argv[2])
    else:
        unittest.main()
