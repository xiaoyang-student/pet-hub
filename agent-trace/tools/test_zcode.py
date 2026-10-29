# -*- coding: utf-8 -*-
"""Deterministic ZCode fixtures; no installed app, network or private chats.

Run: python tools/test_zcode.py
SQLite fixtures use WAL and the verified CLI indexes. All writes are confined
to TemporaryDirectory; the adapter itself must always open with mode=ro.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.base import Adapter
from adapters.zcode import ZCodeAdapter
from core.engine import ADAPTERS

BASE = 1_800_000_000.0
MARKER = "PETHUB-ZCODE-OK"
SCHEMA = """
CREATE TABLE session(id TEXT PRIMARY KEY,last_outcome TEXT);
CREATE TABLE turn_usage(session_id TEXT,turn_id TEXT,user_message_id TEXT,
    status TEXT,started_at INTEGER,completed_at INTEGER,duration_ms INTEGER,
    PRIMARY KEY(session_id,turn_id));
CREATE INDEX turn_usage_started_idx ON turn_usage(started_at);
CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,sequence INTEGER,
    time_created INTEGER,time_updated INTEGER,data TEXT);
CREATE INDEX message_session_sequence_idx
    ON message(session_id,sequence,time_created,id);
CREATE INDEX message_session_time_created_id_idx
    ON message(session_id,time_created,id);
CREATE TABLE part(id TEXT PRIMARY KEY,message_id TEXT,session_id TEXT,
    sequence INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT);
CREATE INDEX part_message_sequence_idx
    ON part(message_id,sequence,time_created,id);
CREATE INDEX part_message_id_id_idx ON part(message_id,id);
CREATE TABLE session_input(id TEXT PRIMARY KEY,session_id TEXT,
    promoted_message_id TEXT,status TEXT);
CREATE INDEX session_input_session_status_idx ON session_input(session_id,status);
"""
# Exact native CLI tool_usage schema/indexes, verified read-only against the GUI
# database. Keep SCHEMA without this optional table to exercise older databases.
NATIVE_SCHEMA = """
CREATE TABLE tool_usage (
    id text primary key,
    session_id text not null references session(id) on delete cascade,
    turn_id text,
    trace_id text,
    tool_call_id text not null,
    tool_name text not null,
    side_effect_scope text,
    read_only integer check(read_only in (0, 1)),
    destructive integer check(destructive in (0, 1)),
    approval_status text,
    status text not null check(status in ('running', 'completed', 'error', 'cancelled')),
    started_at integer not null,
    first_output_at integer,
    completed_at integer,
    duration_ms integer,
    time_to_first_output_ms integer,
    exit_code integer,
    output_bytes integer not null default 0,
    stdout_bytes integer not null default 0,
    stderr_bytes integer not null default 0,
    truncated integer not null default 0 check(truncated in (0, 1)),
    retry_count integer not null default 0,
    retryable integer not null default 0 check(retryable in (0, 1)),
    cancelled_by_user integer not null default 0 check(cancelled_by_user in (0, 1)),
    error_type text,
    error_code text,
    error_message text
);
CREATE UNIQUE INDEX tool_usage_session_tool_call_idx ON tool_usage(session_id,tool_call_id);
CREATE INDEX tool_usage_started_tool_idx ON tool_usage(started_at,tool_name);
CREATE INDEX tool_usage_session_turn_idx ON tool_usage(session_id,turn_id);
"""


def event(name, sid="s1", tid="t1", ts=None, **extra):
    obj = {"timestamp": datetime.fromtimestamp(ts or BASE + 1, timezone.utc).isoformat(),
           "event": name, "sessionId": sid, "turnId": tid,
           "status": "started" if name == "turn.started" else "completed"}
    obj.update(extra)
    return obj


class Fixture:
    def __init__(self, db=True, initial=(), empty_log=False, native=False):
        self.tmp = tempfile.TemporaryDirectory(prefix="pethub-zcode-test-")
        self.root = Path(self.tmp.name)
        self.cli = self.root / "cli"
        self.logs = self.cli / "log"
        self.logs.mkdir(parents=True)
        (self.cli / "db").mkdir()
        self.path = self.logs / "zcode-2027-01-15.jsonl"
        self.con = None
        if db:
            self.con = sqlite3.connect(self.cli / "db" / "db.sqlite")
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.executescript(SCHEMA)
            if native:
                self.con.executescript(NATIVE_SCHEMA)
        if not empty_log:
            self.path.write_text("".join(json.dumps(e) + "\n" for e in initial), encoding="utf-8")
        self.ad = ZCodeAdapter({"roots": [str(self.root)], "exes": []})
        self.now = BASE
        with patch("adapters.zcode.time.time", return_value=BASE):
            assert self.ad.setup()

    def message(self, mid, sid, seq, data):
        ms = int((BASE + 1) * 1000) + seq
        self.con.execute("INSERT INTO message VALUES(?,?,?,?,?,?)",
                         (mid, sid, seq, ms, ms, json.dumps(data)))

    def part(self, pid, mid, sid, seq, data):
        ms = int((BASE + 1) * 1000) + seq
        self.con.execute("INSERT INTO part VALUES(?,?,?,?,?,?,?)",
                         (pid, mid, sid, seq, ms, ms, json.dumps(data)))

    def user(self, sid="s1", tid="t1", seq=0, status="running", text=None):
        uid = sid + ":" + tid + ":user"
        self.con.execute("INSERT OR REPLACE INTO session VALUES(?,?)", (sid, "completed"))
        self.con.execute("INSERT INTO turn_usage VALUES(?,?,?,?,?,?,?)",
                         (sid, tid, uid, status, int((BASE + 1) * 1000), None, 2897))
        self.con.execute("INSERT INTO session_input VALUES(?,?,?,?)", (uid, sid, uid, "promoted"))
        self.message(uid, sid, seq, {"role": "user", "time": {"created": int(BASE * 1000)},
                     "anchor": {"turnId": tid},
                     "semantics": {"origin": "real_user", "kind": "user_prompt", "uiVisibility": "visible"}})
        self.part(uid + ":p", uid, sid, 0, {"type": "text", "text": text or "Reply " + MARKER})
        self.con.commit()
        return uid

    def answer(self, sid="s1", tid="t1", seq=2, text=MARKER, parent=None,
               anchor=None, mid=None, finish="stop"):
        mid = mid or sid + ":" + tid + ":answer"
        self.message(mid, sid, seq, {"role": "assistant", "parentID": parent or sid + ":" + tid + ":user",
                     "anchor": {"turnId": anchor or tid}, "finish": finish,
                     "time": {"completed": int((BASE + 2) * 1000)},
                     "semantics": {"kind": "assistant_response", "uiVisibility": "visible"}})
        self.part(mid + ":start", mid, sid, 0, {"type": "step-start"})
        self.part(mid + ":text", mid, sid, 1, {"type": "text", "text": text})
        self.part(mid + ":finish", mid, sid, 2, {"type": "step-finish", "reason": "stop"})
        self.con.commit()
        return mid

    def tool_usage(self, cid="call-native", sid="s1", tid="t1", name="AskUserQuestion",
                   status="running", approval="requested", completed=None):
        self.con.execute("INSERT OR IGNORE INTO session VALUES(?,?)", (sid, "running"))
        self.con.execute("""INSERT INTO tool_usage
            (id,session_id,turn_id,tool_call_id,tool_name,status,approval_status,
             started_at,completed_at,error_message) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sid + ":" + cid, sid, tid, cid, name, status, approval,
             int((BASE + 1) * 1000), completed, "PRIVATE-TOOL-ERROR"))

    def resolve_tool(self, cid="call-native", sid="s1", tid="t1", status="completed",
                     approval="requested", completed=None):
        self.con.execute("""UPDATE tool_usage SET status=?,approval_status=?,completed_at=?
            WHERE session_id=? AND turn_id=? AND tool_call_id=?""",
            (status, approval, completed, sid, tid, cid))
        self.con.commit()

    def feed(self, *events, path=None):
        self.now += 1.1
        with (path or self.path).open("a", encoding="utf-8") as f:
            for obj in events:
                obj = dict(obj)
                obj["timestamp"] = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
                f.write(json.dumps(obj) + "\n")
        return self.ad.tick(self.now)

    def tick(self, seconds=1.1):
        self.now += seconds
        return self.ad.tick(self.now)

    def close(self):
        if self.con:
            self.con.close()
        self.tmp.cleanup()


class ZCodeTests(unittest.TestCase):
    def fixture(self, **kw):
        f = Fixture(**kw)
        self.addCleanup(f.close)
        return f

    def test_mapping_and_no_generic_inheritance(self):
        module, name = ADAPTERS["zcode"]
        self.assertIs(getattr(importlib.import_module(module), name), ZCodeAdapter)
        self.assertEqual(ZCodeAdapter.__bases__, (Adapter,))

    def test_user_answer_result_and_synthetic_runtime_filter(self):
        f = self.fixture()
        f.user()
        f.message("runtime", "s1", 1, {"role": "user", "synthetic": True,
                  "anchor": {"turnId": "t1"}, "semantics": {"kind": "system_reminder", "uiVisibility": "hidden"}})
        f.part("runtime-part", "runtime", "s1", 0, {"type": "text", "text": "PRIVATE-RUNTIME"})
        f.con.commit()
        start = f.feed(event("turn.started"))
        self.assertEqual([s.kind for s in start], ["user"])
        f.answer()
        answer = f.tick()
        self.assertEqual([s.kind for s in answer], ["answer"])
        self.assertFalse(any(s.kind == "result" for s in start + answer))
        f.con.execute("UPDATE turn_usage SET status='completed',completed_at=?", (int(f.now * 1000),))
        f.con.commit()
        end = f.feed(event("session.event.persistence.completed", context={"sessionEventType": "turn_complete"}),
                     event("turn.completed", durationMs=2897))
        self.assertEqual([s.kind for s in end], ["result"])
        self.assertEqual(end[0].meta["evidence"], "turn.completed")
        self.assertEqual(end[0].meta["phase"], "done")
        self.assertEqual(end[0].meta["duration_ms"], 2897)
        self.assertTrue(all((s.session, s.turn) == ("s1", "t1") for s in start + answer + end))
        self.assertNotIn("PRIVATE-RUNTIME", str(start + answer + end))
        self.assertEqual(f.feed(event("turn.completed")), [])

    def test_completed_in_one_poll_preserves_content_order(self):
        f = self.fixture()
        f.user(status="completed")
        f.answer()
        steps = f.feed(event("turn.started"), event("turn.completed"))
        self.assertEqual([s.kind for s in steps], ["user", "answer", "result"])

    def test_multiple_sessions_and_turn_linkage(self):
        f = self.fixture()
        f.user("s1", "shared", 0, "completed")
        f.answer("s1", "shared", 2, "answer-one")
        f.user("s1", "second", 3, "completed")
        f.answer("s1", "second", 4, "answer-two")
        f.user("s2", "shared", 0, "completed")
        f.answer("s2", "shared", 2, "answer-three")
        # Valid parent, wrong explicit turn: do not pick a background response.
        f.answer("s1", "shared", 5, "BACKGROUND", anchor="title-job", mid="title")
        steps = f.feed(*(event("turn.completed", s, t) for s, t in
                         (("s1", "shared"), ("s1", "second"), ("s2", "shared"))))
        answers = {(s.session, s.turn): s.text for s in steps if s.kind == "answer"}
        self.assertEqual(answers, {("s1", "shared"): "answer-one", ("s1", "second"): "answer-two",
                                   ("s2", "shared"): "answer-three"})
        self.assertEqual(sum(s.kind == "result" for s in steps), 3)
        self.assertEqual(f.ad.cur_session, "s2")

    def test_no_done_from_model_or_database_or_generic_completed(self):
        f = self.fixture()
        f.user(status="completed")
        f.answer()
        names = ("model.request.completed", "model.sdk.generate.completed", "memory.completed",
                 "title.completed", "turn.phase.completed", "session.persistence.completed",
                 "zcode_protocol.startup.completed")
        steps = f.feed(event("turn.started"), *(event(n) for n in names),
                       event("session.event.persistence.completed", context={"sessionEventType": "message_complete"}),
                       event("turn.completed", status="failed"), event("turn.completed", sid=""),
                       event("turn.completed", tid=""))
        self.assertFalse(any(s.kind == "result" for s in steps + f.tick(20)))

    def test_persistence_needs_matching_successful_usage(self):
        f = self.fixture()
        f.user(status="failed")
        f.answer()
        persisted = event("session.event.persistence.completed", context={"sessionEventType": "turn_complete"})
        self.assertFalse(any(s.kind == "result" for s in f.feed(persisted) + f.tick(5)))
        f.con.execute("UPDATE turn_usage SET status='completed'")
        f.con.commit()
        self.assertEqual([s.kind for s in f.tick()], ["result"])
        self.assertEqual(f.feed(persisted), [])

    def test_existing_history_skipped_new_log_first_line_read(self):
        f = self.fixture(initial=[event("turn.completed", sid="history", ts=BASE - 10)])
        f.user(status="completed")
        f.answer()
        self.assertEqual(f.tick(), [])
        new = f.logs / "zcode-next-day.jsonl"
        steps = f.feed(event("turn.completed"), path=new)
        self.assertEqual([s.kind for s in steps], ["user", "answer", "result"])
        self.assertNotIn("history", str(steps))

    def test_log_directory_and_file_created_after_setup(self):
        f = self.fixture(db=False, empty_log=True)
        f.logs.rmdir()
        self.assertEqual(f.tick(), [])
        f.logs.mkdir()
        start = f.feed(event("turn.started"))
        self.assertEqual([s.kind for s in start], ["status"])
        f.feed(event("turn.completed"))
        self.assertEqual([s.kind for s in f.tick(4)], ["result"])

    def test_truncation_rotation_and_same_size_rewrite(self):
        f = self.fixture(db=False)
        f.feed(event("turn.completed", tid="t1"))
        self.assertEqual([s.kind for s in f.tick(4)], ["result"])
        # TailReader detects anchor changes even when replacement grows.
        old = event("turn.completed", tid="t1", ts=f.now - 4)
        new = event("turn.completed", tid="t2", ts=f.now + 0.5)
        f.path.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
        f.tick()
        self.assertEqual([(s.kind, s.turn) for s in f.tick(4)], [("result", "t2")])
        # Rotate the pathname, keep a copy of the prior events in another log.
        rotated = f.logs / "zcode-rotated.jsonl"
        os.replace(f.path, rotated)
        f.feed(event("turn.completed", tid="t3"))
        self.assertEqual([(s.kind, s.turn) for s in f.tick(4)], [("result", "t3")])
        # Roll back to pre-attachment history; never replay it as live activity.
        f.path.write_text(json.dumps(event("turn.completed", tid="old", ts=BASE - 1)) + "\n", encoding="utf-8")
        self.assertEqual(f.tick(4), [])
        # Same-size, same-inode overwrite after truncate is also read from zero.
        one = json.dumps(event("turn.started", tid="t4", ts=f.now + 0.5)) + "\n"
        f.path.write_text(one, encoding="utf-8")
        self.assertEqual([s.turn for s in f.tick()], ["t4"])
        f.path.write_text(one.replace('"t4"', '"t5"'), encoding="utf-8")
        # 空闲轮询不许重开文件，只能靠 stat 发现变化；同尺寸重写必须让
        # mtime 真的前进（真实写盘会，测试里两次写可能落在同一时钟粒度）。
        st = f.path.stat()
        os.utime(f.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertEqual([s.turn for s in f.tick()], ["t5"])

    def test_partial_and_oversized_lines(self):
        f = self.fixture(db=False)
        raw = json.dumps(event("turn.started", ts=BASE + 1))
        f.path.write_text(raw[:40], encoding="utf-8")
        self.assertEqual(f.tick(), [])
        with f.path.open("a", encoding="utf-8") as stream:
            stream.write(raw[40:] + "\n" + "x" * (ZCodeAdapter.MAX_JSON + 1) + "\n")
        self.assertEqual([s.kind for s in f.tick()], ["status"])
        self.assertEqual(f.tick(), [])

    def test_streaming_text_and_typed_tools_never_end_turn(self):
        f = self.fixture()
        f.user()
        mid = f.answer(text="partial", finish=None)
        first = f.feed(event("turn.started"))
        self.assertEqual([s.kind for s in first], ["user", "answer"])
        f.con.execute("UPDATE part SET data=? WHERE id=?",
                      (json.dumps({"type": "text", "text": "final answer"}), mid + ":text"))
        f.part("tool", mid, "s1", 3, {"type": "tool", "tool": "Read", "state": {
            "status": "running", "input": {"header": "DO-NOT-EMIT"}}})
        f.con.commit()
        steps = f.tick()
        self.assertEqual([s.kind for s in steps], ["answer", "tool"])
        f.con.execute("UPDATE part SET data=? WHERE id='tool'", (json.dumps({"type": "tool", "tool": "Read",
                      "state": {"status": "completed", "output": "DO-NOT-EMIT"}}),))
        f.con.commit()
        steps += f.tick()
        self.assertEqual(steps[-1].kind, "tool_done")
        self.assertFalse(any(s.kind == "result" for s in steps))
        self.assertNotIn("DO-NOT-EMIT", str(steps))

    def test_readonly_wal_and_idle_no_database_requeries(self):
        f = self.fixture()
        f.user()
        connect = sqlite3.connect
        calls = []

        def check_uri(database, *args, **kw):
            calls.append(database)
            self.assertTrue(database.endswith("?mode=ro"))
            self.assertNotIn("immutable", database)
            return connect(database, *args, **kw)

        with patch("adapters.zcode.sqlite3.connect", side_effect=check_uri):
            self.assertEqual([s.kind for s in f.feed(event("turn.started"))], ["user"])
            for _ in range(20):
                self.assertEqual(f.tick(), [])
            self.assertEqual(len(calls), 1)
            f.answer()
            self.assertEqual([s.kind for s in f.tick()], ["answer"])
            self.assertEqual(len(calls), 2)
        f.feed(event("turn.completed"))
        with patch("adapters.zcode.sqlite3.connect", side_effect=AssertionError("idle database read")):
            for _ in range(20):
                self.assertEqual(f.tick(), [])

    def test_wal_uncommitted_then_commit_and_database_lock_retry(self):
        f = self.fixture()
        f.user()
        f.feed(event("turn.started"))
        f.con.execute("BEGIN")
        f.message("later", "s1", 2, {"role": "assistant", "parentID": "s1:t1:user", "anchor": {"turnId": "t1"}})
        f.part("later-part", "later", "s1", 0, {"type": "text", "text": MARKER})
        self.assertEqual(f.tick(), [])
        f.con.commit()
        with patch("adapters.zcode.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(f.tick(), [])
        self.assertEqual([s.kind for s in f.tick()], ["answer"])

    def test_missing_indexes_falls_back_to_lifecycle_only(self):
        f = self.fixture()
        f.user(status="completed")
        f.answer()
        f.con.execute("DROP INDEX message_session_sequence_idx")
        f.con.commit()
        steps = f.feed(event("turn.started"), event("turn.completed")) + f.tick(4)
        self.assertEqual([s.kind for s in steps], ["status", "result"])
        self.assertIsNone(f.ad._indexes)

    def test_delayed_database_content_before_result(self):
        f = self.fixture()
        self.assertEqual([s.kind for s in f.feed(event("turn.completed"))], ["status"])
        f.user(status="completed")
        f.answer()
        self.assertEqual([s.kind for s in f.tick()], ["user", "answer", "result"])

    def test_large_turn_incremental_pages_and_bounded_work(self):
        f = self.fixture()
        f.user()
        # More than one page of messages AND parts, with no terminal lifecycle.
        for i in range(36):
            mid = "answer-%03d" % i
            f.message(mid, "s1", i + 1, {"role": "assistant", "parentID": "s1:t1:user"})
            for j in range(20):
                f.part(mid + ":%03d" % j, mid, "s1", j, {"type": "text", "text": "chunk-%d-%d" % (i, j)})
        f.con.commit()
        steps = f.feed(event("turn.started"))
        for _ in range(120):
            steps += f.tick(0.1)
            self.assertGreaterEqual(f.ad._queries, 0)
            if f.ad._turns[("s1", "t1")].db_sig is not None:
                break
        self.assertEqual(sum(s.kind == "answer" for s in steps), 36 * 20)
        self.assertEqual(len({(s.meta.get("part_id"), s.text) for s in steps}), len(steps))
        self.assertFalse(any(s.kind == "result" for s in steps))
        self.assertEqual(f.tick(), [])
        turn = f.ad._turns[("s1", "t1")]
        self.assertLessEqual(len(turn.messages), f.ad.MESSAGES_PER_TURN)
        for m in turn.messages.values():
            self.assertLessEqual(len(m.watch), f.ad.WATCH_PARTS)
            self.assertLessEqual(len(m.seen), f.ad.SEEN_PARTS)

    def test_cache_limits_and_retired_history_deduplication(self):
        f = self.fixture(db=False)
        f.ad.MAX_TURNS = 12
        f.ad.TURNS_PER_TICK = 16
        first = []
        for i in range(30):
            first += f.feed(event("turn.completed", tid="t%d" % i))
            first += f.tick(4)
        self.assertEqual(sum(s.kind == "result" for s in first), 30)
        self.assertLessEqual(len(f.ad._turns), 12)
        self.assertLessEqual(len(f.ad._readers), f.ad.MAX_LOGS)
        f.path.write_text(json.dumps(event("turn.completed", tid="t0", ts=BASE + 1.1)) + "\n", encoding="utf-8")
        self.assertEqual(f.tick(4), [])

    def test_concurrent_sessions_get_fair_database_budget(self):
        f = self.fixture()
        for i in range(5):
            f.user("s%d" % i)
            f.answer("s%d" % i)
        # Prime schema discovery, then leave room for only one session's
        # database work per tick. A changing WAL must not starve later turns.
        f.ad._queries = f.ad.QUERY_BUDGET
        self.assertTrue(f.ad._schema(f.con, f.ad._db_signature()))
        f.ad.QUERY_BUDGET = 7
        steps = f.feed(*(event("turn.started", "s%d" % i) for i in range(5)))
        for i in range(10):
            f.con.execute("UPDATE session SET last_outcome=?", (str(i),))
            f.con.commit()
            steps += f.tick()
        self.assertEqual({s.session for s in steps if s.kind == "answer"},
                         {"s%d" % i for i in range(5)})

    def test_database_errors_do_not_drop_start_or_completion(self):
        f = self.fixture()
        with patch("adapters.zcode.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual([s.kind for s in f.feed(event("turn.started"))], ["status"])
            self.assertEqual(f.feed(event("turn.completed")), [])
            self.assertEqual([s.kind for s in f.tick(4)], ["result"])
            self.assertEqual(f.tick(), [])

    def test_safe_schema_recovers_after_index_added(self):
        f = self.fixture()
        f.user()
        f.answer()
        f.con.execute("DROP INDEX message_session_sequence_idx")
        f.con.commit()
        self.assertEqual([s.kind for s in f.feed(event("turn.started"))], ["status"])
        f.con.execute("CREATE INDEX message_session_sequence_idx ON message(session_id,sequence,time_created,id)")
        f.con.commit()
        self.assertEqual([s.kind for s in f.tick()], ["user", "answer"])

    def test_log_reader_count_and_turn_ttl_are_bounded(self):
        f = self.fixture(db=False)
        f.feed(event("turn.started"))
        for i in range(24):
            p = f.logs / ("zcode-extra-%02d.jsonl" % i)
            p.write_text("{}\n", encoding="utf-8")
        self.assertEqual(f.tick(), [])
        self.assertEqual(len(f.ad._readers), f.ad.MAX_LOGS)
        self.assertEqual(f.tick(f.ad.TURN_TTL + 1), [])
        self.assertEqual(len(f.ad._turns), 0)  # Expiry is never a result.

    def test_native_pending_without_message_anchor_and_idle_heartbeat(self):
        f = self.fixture(native=True)
        f.tool_usage()
        # Native GUI rows can exist without usable turn_usage/message linkage.
        f.message("unanchored", "s1", 1, {"role": "assistant", "parentID": "runtime"})
        f.part("native-part", "unanchored", "s1", 0, {
            "type": "tool", "tool": "AskUserQuestion", "callID": "call-native",
            "state": {"status": "running", "input": {"questions": [
                {"question": "PRIVATE-QUESTION", "options": ["A", "B", "C", "D"]}]},
                "output": "PRIVATE-OUTPUT"}})
        f.con.commit()
        steps = f.feed(event("turn.started"))
        self.assertEqual([s.kind for s in steps], ["status", "ask"])
        self.assertEqual([s.meta["phase"] for s in steps], ["asking", "asking"])
        ask = steps[-1]
        self.assertEqual((ask.session, ask.turn), ("s1", "t1"))
        self.assertEqual(ask.text, "等待你在 ZCode 中回答问题")
        self.assertEqual(ask.meta["phase"], "asking")
        self.assertEqual(ask.meta["tool_call_ids"], ["call-native"])
        self.assertEqual(f.ad.phase, "asking")
        with patch("adapters.zcode.sqlite3.connect", side_effect=AssertionError("idle DB poll")):
            self.assertEqual(f.tick(4.9), [])
            self.assertEqual([s.kind for s in f.tick(0.1)], ["ask"])
            for _ in range(4):
                self.assertEqual(f.tick(4), [])
                beat = f.tick(1)
                self.assertEqual([s.kind for s in beat], ["ask"])
                self.assertEqual(beat[0].text, ask.text)
                steps += beat
        self.assertNotIn("PRIVATE", str(steps))
        self.assertFalse(any(s.kind == "result" for s in steps))

    def test_native_resolution_is_thinking_and_stale_parts_cannot_reopen(self):
        cases = [("completed", "requested", None), ("error", "requested", None),
                 ("cancelled", "requested", None), ("running", "approved", None),
                 ("running", "rejected", None), ("running", "requested", 123),
                 ("running", "none", None), ("completed", "none", 123)]
        for status, approval, completed in cases:
            with self.subTest(status=status, approval=approval, completed=completed):
                f = self.fixture(native=True)
                f.user()
                mid = f.answer(text="ongoing")
                f.tool_usage()
                f.part("ask-part", mid, "s1", 4, {
                    "type": "tool", "tool": "AskUserQuestion", "callID": "call-native",
                    "state": {"status": "running", "input": {"questions": "PRIVATE-INPUT"}}})
                f.con.commit()
                steps = f.feed(event("turn.started"))
                self.assertEqual([s.kind for s in steps], ["user", "answer", "ask"])
                f.resolve_tool(status=status, approval=approval, completed=completed)
                resumed = f.tick(0.1)
                self.assertEqual([s.kind for s in resumed], ["status"])
                self.assertEqual(resumed[0].text, f.ad.RESUME_TEXT)
                self.assertEqual(resumed[0].meta["phase"], "thinking")
                self.assertEqual(f.ad.phase, "thinking")
                self.assertFalse(f.ad._turns[("s1", "t1")].done)
                # Simulate stale DB/part evidence after explicit resolution.
                f.resolve_tool(status="running")
                self.assertEqual(f.tick(6), [])
                self.assertNotIn("PRIVATE", str(steps + resumed))

    def test_native_multi_call_session_turn_and_missing_row_isolation(self):
        f = self.fixture(native=True)
        for cid, sid, tid in [("a", "s1", "t1"), ("b", "s1", "t1"),
                              ("a", "s2", "t1"), ("c", "s1", "t2"),
                              ("hidden", "untracked", "t1")]:
            f.tool_usage(cid, sid, tid)
        f.con.commit()
        steps = f.feed(*(event("turn.started", sid, tid) for sid, tid in
                         [("s1", "t1"), ("s2", "t1"), ("s1", "t2")]))
        asks = {(s.session, s.turn): set(s.meta["tool_call_ids"])
                for s in steps if s.kind == "ask"}
        self.assertEqual(asks, {("s1", "t1"): {"a", "b"}, ("s2", "t1"): {"a"},
                               ("s1", "t2"): {"c"}})
        f.resolve_tool("a")
        steps = f.tick(0.1)
        self.assertEqual([(s.kind, s.session, s.turn) for s in steps], [("ask", "s1", "t1")])
        self.assertEqual(steps[0].meta["tool_call_ids"], ["b"])
        f.resolve_tool("c", tid="t2", status="error")
        steps = f.tick(0.1)
        self.assertEqual([(s.kind, s.session, s.turn) for s in steps], [("status", "s1", "t2")])
        # Same call in a different turn or session cannot resolve b. A deleted
        # row (or incomplete approval metadata) is also not a positive answer.
        f.con.execute("UPDATE tool_usage SET turn_id='other',status='completed' WHERE tool_call_id='b'")
        f.resolve_tool("a", sid="s2", status="running", approval=None)
        beats = f.tick(5)
        self.assertEqual({(s.session, s.turn) for s in beats}, {("s1", "t1"), ("s2", "t1")})
        self.assertTrue(all(s.kind == "ask" for s in beats))
        f.con.execute("UPDATE tool_usage SET turn_id='t1' WHERE tool_call_id='b'")
        f.con.commit()
        self.assertEqual([(s.kind, s.session) for s in f.tick(0.1)], [("status", "s1")])

    def test_native_only_requested_unfinished_exact_tool_not_permissions(self):
        f = self.fixture(native=True)
        f.user()
        mid = f.answer()
        for i, name in enumerate(("Bash", "Read", "askuserquestion", "AskUserQuestion.extra",
                                  "mcp__AskUserQuestion", "AskUserQuestion ")):
            f.tool_usage("permission-%d" % i, name=name)
        f.tool_usage("approved", approval="approved")
        f.tool_usage("unspecified", approval=None)
        f.tool_usage("ended", completed=123)
        f.tool_usage("failed", status="error")
        f.tool_usage("finished", status="completed")
        f.tool_usage("cancelled", status="cancelled")
        f.part("unverified", mid, "s1", 4, {
            "type": "tool", "tool": "AskUserQuestion", "callID": "not-in-metadata",
            "state": {"status": "running", "input": {"questions": "PRIVATE-QUESTION"}}})
        f.con.commit()
        steps = f.feed(event("turn.started")) + f.tick(10)
        self.assertEqual([s.kind for s in steps], ["user", "answer", "tool"])
        self.assertNotIn("PRIVATE", str(steps))

    def test_native_status_is_last_after_same_turn_activity_only(self):
        f = self.fixture(native=True)
        f.user()
        f.tool_usage()
        f.con.commit()
        steps = f.feed(event("turn.started"))
        self.assertEqual([s.kind for s in steps], ["user", "ask"])
        self.assertEqual([s.meta["phase"] for s in steps], ["asking", "asking"])
        # Neither another session with the same turn ID nor another turn in
        # this session may inherit or prematurely refresh the pending wait.
        f.user("s2")
        other_session = f.answer("s2")
        f.user("s1", "t2", seq=3)
        other_turn = f.answer("s1", "t2", seq=4)
        steps = f.feed(event("turn.started", "s2"), event("turn.started", "s1", "t2"))
        self.assertEqual([s.kind for s in steps], ["user", "answer", "user", "answer"])
        self.assertEqual([s.meta["phase"] for s in steps],
                         ["received", "answering", "received", "answering"])
        mid = f.answer(seq=5, text="ongoing")
        f.part("reason", mid, "s1", 3, {"type": "reasoning", "text": "checking"})
        f.part("other-tool", mid, "s1", 4, {"type": "tool", "tool": "Read",
               "callID": "different-call", "state": {"status": "running", "input": "PRIVATE"}})
        for other in (other_session, other_turn):
            f.con.execute("UPDATE part SET data=? WHERE id=?", (json.dumps({
                "type": "text", "text": "other activity"}), other + ":text"))
        f.con.commit()
        steps = f.tick(0.1)
        same = [s for s in steps if (s.session, s.turn) == ("s1", "t1")]
        others = [s for s in steps if (s.session, s.turn) != ("s1", "t1")]
        self.assertEqual([s.kind for s in same], ["answer", "think", "tool", "ask"])
        self.assertEqual([s.meta["phase"] for s in same], ["asking"] * 4)
        self.assertEqual({(s.session, s.turn) for s in others}, {("s2", "t1"), ("s1", "t2")})
        self.assertEqual([(s.kind, s.meta["phase"]) for s in others], [("answer", "answering")] * 2)
        self.assertIs(steps[-1], same[-1])
        self.assertEqual(f.ad.phase, "asking")
        self.assertNotIn("PRIVATE", str(steps))
        f.con.execute("UPDATE part SET data=? WHERE id='other-tool'", (json.dumps({
            "type": "tool", "tool": "Read", "state": {"status": "completed", "output": "PRIVATE"}}),))
        f.con.commit()
        steps = f.tick(0.1)
        self.assertEqual([s.kind for s in steps], ["tool_done", "ask"])
        self.assertEqual([s.meta["phase"] for s in steps], ["asking", "asking"])
        self.assertNotIn("PRIVATE", str(steps))
        f.con.execute("UPDATE part SET data=? WHERE id=?", (json.dumps({
            "type": "text", "text": "continuing"}), mid + ":text"))
        f.resolve_tool()
        steps = f.tick(0.1)
        self.assertEqual([s.kind for s in steps], ["answer", "status"])
        self.assertEqual([s.meta["phase"] for s in steps], ["answering", "thinking"])
        # Completion wins even with a still-unanswered native call and content
        # arriving in the same tick; neither content nor result stays asking.
        f.tool_usage("second")
        f.con.commit()
        self.assertEqual([s.kind for s in f.tick(0.1)], ["ask"])
        f.answer(seq=6, mid="final", text="finished")
        steps = f.feed(event("turn.completed"))
        self.assertEqual([s.kind for s in steps], ["answer", "result"])
        self.assertEqual([s.meta["phase"] for s in steps], ["answering", "done"])

    def test_native_terminal_clears_and_late_evidence_never_reopens(self):
        for direct in (True, False):
            with self.subTest(direct=direct):
                f = self.fixture(native=True)
                f.user()
                f.tool_usage()
                f.con.commit()
                self.assertEqual(f.feed(event("turn.started"))[-1].kind, "ask")
                end = (event("turn.completed") if direct else event(
                    "session.event.persistence.completed", context={"sessionEventType": "turn_complete"}))
                self.assertEqual(f.feed(end), [])
                turn = f.ad._turns[("s1", "t1")]
                self.assertEqual(len(turn.native_calls), 0)
                f.tool_usage("late")
                f.con.commit()
                steps = f.tick(5)
                self.assertEqual([s.kind for s in steps], ["result"] if direct else [])
                if not direct:
                    f.con.execute("UPDATE turn_usage SET status='completed'")
                    f.con.commit()
                    self.assertEqual([s.kind for s in f.tick()], ["result"])
                self.assertEqual(f.feed(event("turn.started")), [])
                f.resolve_tool("late", status="error")
                self.assertEqual(f.tick(10), [])
                self.assertTrue(turn.done)
                self.assertFalse(turn.native_calls)
        # A first observation already carrying terminal evidence must not ask.
        f = self.fixture(native=True)
        f.tool_usage()
        f.con.commit()
        steps = f.feed(event("turn.started"), event("turn.completed")) + f.tick(4)
        self.assertEqual([s.kind for s in steps], ["status", "result"])

    def test_native_attach_retirement_and_ttl_fences(self):
        old = event("turn.started", ts=BASE - 1)
        f = self.fixture(native=True, initial=[old])
        f.tool_usage()
        f.con.commit()
        with patch("adapters.zcode.sqlite3.connect", side_effect=AssertionError("historical DB poll")):
            self.assertEqual(f.tick(1), [])
        f.ad.MAX_TURNS = 1
        f.feed(event("turn.started"))
        live_ts = f.now
        f.tool_usage("new", tid="t2")
        f.con.commit()
        self.assertEqual(f.feed(event("turn.started", tid="t2"))[-1].turn, "t2")
        self.assertNotIn(("s1", "t1"), f.ad._turns)
        f.path.write_text(json.dumps(event("turn.started", ts=live_ts)) + "\n", encoding="utf-8")
        self.assertEqual(f.tick(0.1), [])
        self.assertNotIn(("s1", "t1"), f.ad._turns)
        self.assertEqual(f.tick(f.ad.TURN_TTL + 1), [])
        self.assertFalse(f.ad._turns)
        with patch("adapters.zcode.time.time", return_value=f.now):
            self.assertTrue(f.ad.setup())
        self.assertEqual(f.tick(10), [])

    def test_native_optional_schema_indexes_and_schema_change_recovery(self):
        cases = ["DROP TABLE tool_usage",
                 "ALTER TABLE tool_usage RENAME COLUMN approval_status TO old_approval",
                 "DROP INDEX tool_usage_session_tool_call_idx",
                 "DROP INDEX tool_usage_session_turn_idx",
                 """DROP INDEX tool_usage_session_turn_idx;
                    CREATE INDEX tool_usage_session_turn_idx ON tool_usage(session_id,turn_id)
                    WHERE status='running'""",
                 """DROP INDEX tool_usage_session_turn_idx;
                    CREATE INDEX tool_usage_session_turn_idx ON tool_usage(session_id,turn_id,started_at)""",
                 """DROP INDEX tool_usage_session_tool_call_idx;
                    CREATE INDEX tool_usage_session_tool_call_idx ON tool_usage(session_id,tool_call_id)""",
                 """DROP INDEX tool_usage_session_tool_call_idx;
                    CREATE UNIQUE INDEX tool_usage_session_tool_call_idx
                    ON tool_usage(session_id,tool_call_id COLLATE NOCASE)""",
                 """DROP INDEX tool_usage_session_turn_idx;
                    CREATE INDEX tool_usage_session_turn_idx
                    ON tool_usage(session_id,turn_id COLLATE NOCASE)""",
                 "ALTER TABLE tool_usage ADD COLUMN rowid INTEGER",
                 "DROP TABLE tool_usage;" + NATIVE_SCHEMA.replace(
                     ");\nCREATE UNIQUE INDEX", ") WITHOUT ROWID;\nCREATE UNIQUE INDEX", 1)]
        for ddl in cases:
            with self.subTest(ddl=ddl):
                f = self.fixture(native=True)
                f.user()
                f.answer()
                f.tool_usage()
                f.con.commit()
                f.con.executescript(ddl)
                steps = f.feed(event("turn.started"))
                self.assertEqual([s.kind for s in steps], ["user", "answer"])
                self.assertNotIn("tool_usage", f.ad._indexes)
                with patch("adapters.zcode.sqlite3.connect", side_effect=AssertionError("idle DB poll")):
                    self.assertEqual(f.tick(10), [])
        f = self.fixture(native=True)
        f.tool_usage()
        f.con.executescript("DROP INDEX tool_usage_session_turn_idx")
        self.assertEqual([s.kind for s in f.feed(event("turn.started"))], ["status"])
        f.con.execute("CREATE INDEX tool_usage_session_turn_idx ON tool_usage(session_id,turn_id)")
        f.con.commit()
        self.assertEqual([s.kind for s in f.tick()], ["ask"])

    def test_native_recent_tail_and_pending_refresh_are_indexed_bounded(self):
        f = self.fixture(native=True)
        f.tool_usage()
        f.con.commit()
        f.feed(event("turn.started"))
        for i in range(2000):
            f.tool_usage("ordinary-%d" % i, name="Read")
            f.tool_usage("foreign-%d" % i, sid="untracked")
        f.resolve_tool(status="error")
        queries = []
        original = f.ad._q

        def record(con, sql, args=()):
            rows = original(con, sql, args)
            if "FROM tool_usage" in sql:
                queries.append((sql, args, len(rows)))
            return rows

        with patch.object(f.ad, "_q", side_effect=record):
            steps = f.tick(0.1)
        self.assertEqual([s.kind for s in steps], ["status"])
        self.assertEqual(len(queries), 2)
        self.assertGreaterEqual(f.ad._queries, 0)
        for sql, args, count in queries:
            self.assertLessEqual(count, f.ad.NATIVE_CALLS)
            self.assertEqual(args[:2], ("s1", "t1"))
            self.assertEqual(args[-1], f.ad.NATIVE_CALLS)
            self.assertNotIn("error_message", sql)
            self.assertNotIn("data", sql)
            plan = " ".join(r[3] for r in f.con.execute("EXPLAIN QUERY PLAN " + sql, args))
            self.assertIn("SEARCH tool_usage USING INDEX", plan)
            self.assertNotIn("SCAN", plan)
            self.assertNotIn("TEMP B-TREE", plan)
            self.assertIn("session_id=?", plan)
            if "tool_call_id COLLATE BINARY IN" in sql:
                self.assertIn("tool_call_id=?", plan)
                self.assertEqual(args[2:-1], ("call-native",))
            else:
                self.assertIn("turn_id=?", plan)
            # Each native query itself remains well below the shared VM budget,
            # even with thousands of unrelated or older rows.
            instructions = [0]

            def limited():
                instructions[0] += 100
                return int(instructions[0] > 5000)

            f.con.set_progress_handler(limited, 100)
            try:
                f.con.execute(sql, args).fetchall()
            finally:
                f.con.set_progress_handler(None, 0)
            self.assertLessEqual(instructions[0], 5000)

    def test_native_bounded_state_and_evicted_resolutions_never_reopen(self):
        f = self.fixture(native=True)
        f.ad.NATIVE_CALLS = 4
        for i in range(12):
            f.tool_usage("call-%d" % i)
        f.con.commit()
        steps = f.feed(event("turn.started"))
        turn = f.ad._turns[("s1", "t1")]
        self.assertEqual(len(turn.native_calls), 4)
        self.assertEqual(set(steps[-1].meta["tool_call_ids"]), {"call-%d" % i for i in range(8, 12)})
        f.con.execute("UPDATE tool_usage SET status='completed'")
        f.con.commit()
        self.assertEqual([s.kind for s in f.tick(0.1)], ["status"])
        for batch in range(3):
            for i in range(4):
                f.tool_usage("resolved-%d-%d" % (batch, i), status="completed")
            f.con.commit()
            self.assertEqual(f.tick(0.1), [])
            self.assertLessEqual(len(turn.native_calls), f.ad.NATIVE_CALLS)
        f.con.execute("DELETE FROM tool_usage WHERE tool_call_id LIKE 'resolved-%'")
        f.con.execute("UPDATE tool_usage SET status='running'")
        f.con.commit()
        self.assertEqual(f.tick(10), [])  # Retired rowids cannot reopen asking.

    def test_native_query_budget_interrupt_preserves_pending_until_retry(self):
        f = self.fixture(native=True)
        f.tool_usage("old")
        f.con.commit()
        f.feed(event("turn.started"))
        turn = f.ad._turns[("s1", "t1")]
        before = dict(turn.native_calls)
        sig = turn.native_sig
        for i in range(f.ad.NATIVE_CALLS):
            f.tool_usage("padding-%d" % i, name="Read")
        f.tool_usage("new")
        f.resolve_tool("old")
        f.ad.QUERY_BUDGET = 2  # schema_version + recent tail; no pending lookup.
        steps = f.tick(5)
        self.assertEqual([s.kind for s in steps], ["ask"])
        self.assertEqual(steps[0].meta["tool_call_ids"], ["old"])
        self.assertEqual(dict(turn.native_calls), before)
        self.assertEqual(turn.native_sig, sig)
        self.assertEqual(f.ad._queries, 0)
        f.ad.QUERY_BUDGET = 96
        steps = f.tick(0.1)
        self.assertEqual([s.kind for s in steps], ["ask"])
        self.assertEqual(steps[0].meta["tool_call_ids"], ["new"])
        f.resolve_tool("new", status="error")
        with patch("adapters.zcode.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual([s.kind for s in f.tick(5)], ["ask"])
        self.assertEqual([s.kind for s in f.tick(0.1)], ["status"])

    def test_native_metadata_respects_shared_time_and_instruction_budget(self):
        for budget in ("DB_SECONDS", "DB_INSTRUCTIONS"):
            with self.subTest(budget=budget):
                f = self.fixture(native=True)
                # Cross the real 1000-op progress interval in a single native
                # statement. With 32 rows SQLite 3.50 executes each query below
                # that interval; their combined work need not invoke a callback.
                # Enlarge only this fixture, not the production limits/budgets.
                f.ad.NATIVE_CALLS = 64
                for i in range(f.ad.NATIVE_CALLS):
                    f.tool_usage("pending-%d" % i)
                f.con.commit()
                f.feed(event("turn.started"))
                turn = f.ad._turns[("s1", "t1")]
                before, sig = dict(turn.native_calls), turn.native_sig
                self.assertEqual(len(before), f.ad.NATIVE_CALLS)
                for i in range(f.ad.NATIVE_CALLS):
                    f.tool_usage("padding-%d" % i, name="Read")
                f.con.execute("UPDATE tool_usage SET status='completed' WHERE tool_name='AskUserQuestion'")
                f.con.commit()
                native_reads, interrupted = [], []
                original = f.ad._q

                def record(con, sql, args=()):
                    if "FROM tool_usage" in sql:
                        native_reads.append(sql)
                    try:
                        return original(con, sql, args)
                    except sqlite3.OperationalError as exc:
                        interrupted.append((sql, exc.sqlite_errorcode))
                        raise

                setattr(f.ad, budget, 0)
                with patch.object(f.ad, "_q", side_effect=record), patch(
                        "adapters.zcode.time.monotonic", side_effect=[0.0, 1.0]):
                    steps = f.tick(5)
                self.assertTrue(native_reads)  # Interrupt native SQL, not schema discovery.
                self.assertEqual(interrupted, [(native_reads[-1], sqlite3.SQLITE_INTERRUPT)])
                self.assertEqual([s.kind for s in steps], ["ask"])
                self.assertEqual(set(steps[0].meta["tool_call_ids"]), set(before))
                self.assertEqual(dict(turn.native_calls), before)
                self.assertEqual(turn.native_sig, sig)
                self.assertGreaterEqual(f.ad._queries, 0)
                setattr(f.ad, budget, getattr(ZCodeAdapter, budget))
                steps = f.tick(0.1)
                self.assertEqual([s.kind for s in steps], ["status"])
                self.assertEqual(steps[0].meta["phase"], "thinking")
                self.assertFalse(any(waiting for _, waiting in turn.native_calls.values()))

    def test_native_metadata_polled_once_while_message_pages_drain(self):
        f = self.fixture(native=True)
        f.user()
        f.tool_usage()
        for i in range(40):
            f.answer(seq=i + 1, mid="answer-%d" % i)
        native_reads = []
        original = f.ad._q

        def record(con, sql, args=()):
            if "FROM tool_usage" in sql:
                native_reads.append(sql)
            return original(con, sql, args)

        with patch.object(f.ad, "_q", side_effect=record):
            steps = f.feed(event("turn.started"))
            for _ in range(40):
                steps += f.tick(0.1)
                if f.ad._turns[("s1", "t1")].db_sig is not None:
                    break
        self.assertEqual(sum(s.kind == "answer" for s in steps), 40)
        self.assertEqual(len(native_reads), 1)
        self.assertEqual(steps[-1].kind, "ask")

    def test_native_fair_budget_and_heartbeat_outside_active_batch(self):
        f = self.fixture(native=True)
        for i in range(12):
            f.tool_usage("shared-call", sid="s%d" % i)
        f.con.commit()
        f.ad._queries = f.ad.QUERY_BUDGET
        self.assertTrue(f.ad._schema(f.con, f.ad._db_signature()))
        f.ad.QUERY_BUDGET = 4
        f.ad.TURNS_PER_TICK = 2
        steps = f.feed(*(event("turn.started", "s%d" % i) for i in range(12)))
        for _ in range(24):
            steps += f.tick(0.1)
            self.assertGreaterEqual(f.ad._queries, 0)
        self.assertEqual({s.session for s in steps if s.kind == "ask"}, {"s%d" % i for i in range(12)})
        with patch("adapters.zcode.sqlite3.connect", side_effect=AssertionError("idle DB poll")):
            steps = f.tick(5)
        self.assertEqual(len(steps), 12)
        self.assertTrue(all(s.kind == "ask" for s in steps))
        self.assertEqual({s.session for s in steps}, {"s%d" % i for i in range(12)})

    def test_default_cli_root_when_discovery_only_has_gui(self):
        f = self.fixture()
        home = f.root / "home"
        home.mkdir()
        actual = home / ".zcode"
        actual.mkdir()
        (actual / "cli").mkdir()
        ad = ZCodeAdapter({"roots": [str(f.root / "gui")]})
        with patch("adapters.zcode.Path.home", return_value=home):
            self.assertTrue(ad.setup())
        self.assertEqual(ad.cli, actual / "cli")


if __name__ == "__main__":
    unittest.main(verbosity=2)
