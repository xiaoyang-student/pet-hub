# -*- coding: utf-8 -*-
import datetime
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.base import ReadOnlyDB, TailReader
from adapters.chat_apps import (ChatGPTAdapter, ChromiumChatAdapter, CodexLogReader,
                                DoubaoAdapter, RawTail)
from adapters.doubao_work import DoubaoWorkAdapter
from adapters.qianfan import BaiduDaziAdapter, _Recent
from adapters.qoder import QoderAdapter
from adapters.workbuddy import WorkBuddyAdapter
from core.model import (K_ANSWER, K_ASK, K_EDIT, K_ERROR, K_RESULT, K_STATUS, K_THINK,
                        K_TOOL, K_TOOL_DONE, K_USER, P_DONE, P_ERROR, P_IDLE,
                        P_RECEIVED, P_THINKING)


class Samples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pethub-adapter-test-")
        self.root = self.tmp.name
        self.resources = []

    def tearDown(self):
        for resource in reversed(self.resources):
            close = getattr(resource, "close", None)
            if close:
                close()
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.root, name)

    def write(self, path, data, mode="wb"):
        with open(path, mode) as f:
            f.write(data)

    def db(self, name, schema):
        path = self.path(name)
        con = sqlite3.connect(path)
        self.resources.append(con)
        con.execute(schema)
        con.commit()
        return path, con


class QoderTests(Samples):
    def adapter(self):
        path, con = self.db("buffer.sqlite", "create table active_turn_projection_items "
                            "(session_id,turn_id,item_type,item_json,created_at,ordinal,parent_tool_use_id)")
        mainpath, main = self.db("main.sqlite", "create table chat_session_messages "
                                 "(session_id,turn_id,sequence,payload_json,role,created_at)")
        self.insert(con, 100, "seed")
        ad = QoderAdapter({"roots": [], "exes": []})
        ad.buf, ad.main = ReadOnlyDB(path), ReadOnlyDB(mainpath)
        self.resources.extend((ad.buf, ad.main))
        ad._seed()
        return ad, con, main

    @staticmethod
    def insert(con, ordinal, text, stamp=1800000000000, turn="turn"):
        payload = json.dumps({"type": "assistant", "message": {"content": text}})
        con.execute("insert into active_turn_projection_items values (?,?,?,?,?,?,?)",
                    ("session", turn, "assistant", payload, stamp, ordinal, ""))
        con.commit()

    def test_seed_idle_does_not_replay_and_main_is_polled(self):
        ad, con, main = self.adapter()
        for _ in range(3):
            self.assertEqual(ad.tick(time.time()), [])
        main.execute("insert into chat_session_messages values (?,?,?,?,?,?)",
                     ("s", "t", 1, json.dumps({"parts": [{"type": "text", "text": "main new"}]}),
                      "assistant", 1800000000001))
        main.commit()
        steps = ad.tick(time.time())
        self.assertEqual([s.text for s in steps], ["main new"])
        self.assertEqual(ad.tick(time.time()), [])

    def test_buffer_noise_does_not_block_main(self):
        ad, con, main = self.adapter()
        con.execute("insert into active_turn_projection_items values (?,?,?,?,?,?,?)",
                    ("s", "t", "noise", "{}", 1800000000001, 101, ""))
        con.commit()
        main.execute("insert into chat_session_messages values (?,?,?,?,?,?)",
                     ("s", "t", 1, json.dumps({"parts": [{"type": "hook", "hook": {"event": "Other"}}]}),
                      "assistant", 1800000000001))
        main.commit()
        self.assertEqual([s.kind for s in ad.tick(time.time())], [K_STATUS])

    def test_rollback_and_same_cursor_new_generation(self):
        ad, con, main = self.adapter()
        con.execute("delete from active_turn_projection_items")
        self.insert(con, 1, "new round", stamp=1799999999999)
        self.assertEqual([s.text for s in ad.tick(time.time())], ["new round"])
        self.assertEqual(ad.tick(time.time()), [])
        con.execute("delete from active_turn_projection_items")
        self.insert(con, 1, "same cursor changed", stamp=1799999999999, turn="new-turn")
        self.assertEqual([s.text for s in ad.tick(time.time())], ["same cursor changed"])
        self.assertEqual(ad.tick(time.time()), [])
        con.execute("delete from active_turn_projection_items")
        con.commit()
        self.assertEqual(ad.tick(time.time()), [])
        self.insert(con, 2, "after empty", stamp=1800000000001)
        self.assertEqual([s.text for s in ad.tick(time.time())], ["after empty"])

    def test_same_cursor_rows_from_two_sessions_are_both_delivered(self):
        """主键含 session/turn，(created_at,ordinal) 相同是合法的两行，不能只读一行。"""
        ad, con, main = self.adapter()
        con.execute("delete from active_turn_projection_items")
        for sid, tid, text in (("s1", "t1", "from s1"), ("s2", "t2", "from s2")):
            con.execute("insert into active_turn_projection_items values (?,?,?,?,?,?,?)",
                        (sid, tid, "assistant",
                         json.dumps({"type": "assistant", "message": {"content": text}}),
                         1800000000001, 101, ""))
        con.commit()
        self.assertEqual(sorted(s.text for s in ad.tick(time.time())),
                         ["from s1", "from s2"])
        self.assertEqual(ad.tick(time.time()), [])

    def test_in_place_payload_update_is_not_generation_reset(self):
        ad, con, main = self.adapter()
        payload = json.dumps({"type": "assistant", "message": {"content": "stream update"}})
        con.execute("update active_turn_projection_items set item_json=?", (payload,))
        con.commit()
        for _ in range(3):
            self.assertEqual(ad.tick(1800000000), [])
        self.assertEqual(ad.cur, (1800000000000, 100))

    def test_idle_poll_stress(self):
        ad, con, main = self.adapter()
        start = time.process_time()
        for _ in range(2000):
            self.assertEqual(ad.tick(1800000000), [])
        elapsed = time.process_time() - start
        self.assertEqual(ad.steps, 0)
        print("\nSynthetic Qoder: 2000 idle polls, 0 events, CPU %.4fs" % elapsed)

    def terminal_schema(self, main):
        main.execute("alter table chat_session_messages add column status default 'running'")
        main.execute("alter table chat_session_messages add column updated_at default 1800000000000")
        main.execute("create index turn_sequence on chat_session_messages(session_id,turn_id,sequence)")
        main.commit()

    def final_row(self, main, status='completed', sid='session', tid='turn', **extra):
        payload = dict(resultUuid='result-id', completedAt='2026-10-29T00:22:09.523Z',
                       finalTextId='final', parts=[{'type': 'thinking', 'thinking': 'working'}] * 20
                       + [{'type': 'text', 'text': 'final answer'},
                          {'type': 'hook', 'hook': {'event': 'Stop'}}])
        payload.update(extra)
        main.execute("insert into chat_session_messages values (?,?,?,?,?,?,?,?)",
                     (sid, tid, 2, json.dumps(payload), 'assistant', 1800000000001,
                      status, 1800000000002))
        main.commit()

    def test_completed_envelope_after_eight_parts_and_late_file(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.insert(con, 101, 'working')
        self.final_row(main)
        steps = ad.tick(1800000001)
        self.assertEqual([s.kind for s in steps], [K_ANSWER, K_RESULT])
        self.assertEqual(steps[-1].meta['terminal'], True)
        self.insert(con, 102, 'late answer')
        ad.session, ad.turn, ad._file_turn = 'session', 'turn', ''
        ad.main.q = Mock(wraps=ad.main.q)
        self.assertEqual(ad.tick(1800000003), [])
        self.assertFalse(any('turn_file_change_files' in call.args[0]
                             for call in ad.main.q.call_args_list))
        self.assertEqual(ad.phase, P_DONE)

    def test_completion_updates_existing_message_during_buffer_activity(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.final_row(main, status='running', completedAt=None, resultUuid=None)
        ad.tick(1800000001)
        payload = json.dumps(dict(resultUuid='r', completedAt='now', parts=[]))
        main.execute("update chat_session_messages set payload_json=?,status='completed',updated_at=?",
                     (payload, 1800000005000))
        main.commit()
        self.insert(con, 101, 'other active', turn='other-turn')
        steps = ad.tick(1800000005)
        self.assertEqual([s.kind for s in steps], [K_ANSWER, K_RESULT])
        self.assertEqual(steps[-1].turn, 'turn')

    def test_background_and_notification_are_not_foreground_completion(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.final_row(main, backgroundTaskId='bg', responseKind='background-summary')
        self.assertEqual(ad.tick(1800000001), [])
        main.execute("delete from chat_session_messages")
        self.final_row(main, resultUuid=None, completedAt=None,
                       parts=[{'type': 'hook', 'hook': {'event': 'Notification'}}])
        ad.mcur = (0, 0)
        steps = ad.tick(1800000003)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertNotIn(('session', 'turn'), ad._closed)

    def test_failed_turn_is_not_success_and_next_turn_runs(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.final_row(main, status='failed')
        steps = ad.tick(1800000001)
        self.assertEqual([s.kind for s in steps], [K_ERROR])
        self.assertTrue(steps[0].meta['terminal'])
        self.insert(con, 101, 'new request', turn='next-turn')
        self.assertEqual([s.kind for s in ad.tick(1800000003)], [K_ANSWER])

    def test_terminal_tracking_is_bounded(self):
        ad, con, main = self.adapter()
        for n in range(1000):
            ad._watch('s', str(n))
        self.assertEqual(len(ad._watching), 128)

    def test_replacement_and_explicit_replay(self):
        ad, con, main = self.adapter()
        ad.replay(5)
        self.assertEqual([s.text for s in ad.tick(time.time())], ["seed"])
        self.assertEqual(ad.tick(time.time()), [])
        ad.buf.close()
        con.close()
        path, replacement = self.db("replacement.sqlite", "create table active_turn_projection_items "
                                    "(session_id,turn_id,item_type,item_json,created_at,ordinal,parent_tool_use_id)")
        self.insert(replacement, 1, "replacement", stamp=1000)
        replacement.close()
        os.replace(path, ad.buf.path)
        self.assertEqual([s.text for s in ad.tick(time.time())], ["replacement"])
        self.assertEqual(ad.tick(time.time()), [])

    @staticmethod
    def question_block(call="call-1", **extra):
        block = dict(type="tool_use", id=call, name="AskUserQuestion",
                     input={"questions": [{"question": "private question", "options": ["private option"]}],
                            "answers": {"draft": "private answer"}})
        block.update(extra)
        return block

    @staticmethod
    def native_buffer(con, ordinal, content, role="assistant", offset=1,
                      sid="session", tid="turn", parent=""):
        con.execute("insert into active_turn_projection_items values (?,?,?,?,?,?,?)",
                    (sid, tid, role, json.dumps({"type": role, "message": {"content": content}}),
                     1800000000000 + int(offset * 1000), ordinal, parent))
        con.commit()

    def native_row(self, main, state="running", sid="session", tid="turn", seq=2,
                   offset=1, prefix=0, **extra):
        tool = dict(id="call-1", name="AskUserQuestion", status=state,
                    parentToolUseId=None, startedAt="2026-10-29T00:00:00Z", completedAt=None,
                    input=self.question_block()["input"])
        tool.update(extra)
        payload = dict(parts=[{"type": "text", "text": "working"}] * prefix
                             + [{"type": "tool", "tool": tool}])
        main.execute("insert into chat_session_messages "
                     "(session_id,turn_id,sequence,payload_json,role,created_at) values (?,?,?,?,?,?)",
                     (sid, tid, seq, json.dumps(payload), "assistant", 1800000000000 + offset * 1000))
        main.commit()
        return payload

    def test_native_buffer_request_result_and_empty_result_are_safe(self):
        ad, con, main = self.adapter()
        self.native_buffer(con, 101, [self.question_block()])
        steps = ad.tick(1800000001)
        self.assertEqual([s.kind for s in steps], [K_ASK])
        self.assertEqual(steps[0].text, ad.QUESTION_TEXT)
        self.assertEqual((steps[0].session, steps[0].turn, steps[0].meta["callId"]),
                         ("session", "turn", "call-1"))
        self.assertNotIn("private", repr(steps))
        self.assertEqual(ad.phase, "asking")
        self.native_buffer(con, 102, [{"type": "tool_result", "tool_use_id": "call-1",
                                      "content": "private answer"}], role="user", offset=2)
        steps = ad.tick(1800000002)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertEqual(steps[0].meta["phase"], P_THINKING)
        self.assertIs(steps[0].meta["terminal"], False)
        self.assertNotIn("private", repr(steps))
        self.assertEqual(ad.phase, P_THINKING)
        self.assertFalse(ad._questions)
        self.native_buffer(con, 103, [self.question_block("call-2")], offset=3)
        self.assertEqual([s.kind for s in ad.tick(1800000003)], [K_ASK])
        self.native_buffer(con, 104, [{"type": "tool_result", "tool_use_id": "call-2", "content": []}],
                           role="user", offset=4)
        self.assertEqual([s.kind for s in ad.tick(1800000004)], [K_STATUS])
        self.assertEqual(ad.phase, P_THINKING)
        self.assertEqual(ad.tick(1800000010), [])

    def test_native_results_require_exact_identity_and_ignore_old_duplicates(self):
        ad, con, main = self.adapter()
        self.native_buffer(con, 101, [self.question_block()])
        ad.tick(1800000001)
        for n, (sid, tid, call) in enumerate((("other", "turn", "call-1"),
                                             ("session", "old-turn", "call-1"),
                                             ("session", "turn", "other"),
                                             ("session", "turn", ""))):
            self.native_buffer(con, 102 + n, [{"type": "tool_result", "tool_use_id": call, "content": []},
                                            {"type": "text", "text": "tool envelope"}],
                               role="user", offset=2 + n * .1, sid=sid, tid=tid)
            self.assertEqual(ad.tick(1800000002 + n * .1), [])
        old = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "call-1", "content": "private old answer"}]}})
        self.assertEqual(ad._parse("session", "turn", "user", old, 99, 1800000000), [])
        self.assertIn("call-1", ad._questions[("session", "turn")])
        self.native_buffer(con, 106, [self.question_block()], offset=3)
        self.assertEqual(ad.tick(1800000003), [])
        self.assertEqual([s.kind for s in ad.tick(1800000006)], [K_ASK])
        self.native_buffer(con, 107, [{"type": "tool_result", "tool_use_id": "call-1", "content": ""}],
                           role="user", offset=7)
        self.assertEqual([s.kind for s in ad.tick(1800000007)], [K_STATUS])
        self.native_buffer(con, 108, [self.question_block()], offset=8)
        self.native_buffer(con, 109, [{"type": "tool_result", "tool_use_id": "call-1", "content": "private"}],
                           role="user", offset=9)
        self.native_row(main)
        self.assertEqual(ad.tick(1800000009), [])
        self.assertEqual(ad.tick(1800000020), [])

    def test_native_result_before_request_is_tombstoned(self):
        ad, con, main = self.adapter()
        self.native_buffer(con, 101, [{"type": "tool_result", "tool_use_id": "call-1", "content": []}],
                           role="user")
        self.assertEqual(ad.tick(1800000001), [])
        self.native_buffer(con, 102, [self.question_block()], offset=2)
        self.assertEqual(ad.tick(1800000002), [])
        self.assertFalse(ad._questions)

    def test_native_wait_holds_across_activity_and_refreshes_last(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.native_buffer(con, 101, [self.question_block()])
        ad.tick(1800000001)
        self.native_buffer(con, 102, [{"type": "tool_use", "name": "Read", "input": {"path": "file.py"}},
                                    {"type": "thinking", "thinking": "working"}], offset=2)
        steps = ad.tick(1800000002)
        self.assertTrue(steps)
        self.assertTrue(all(s.meta["phase"] == "asking" for s in steps))
        self.assertEqual(ad.phase, "asking")
        self.assertEqual(ad.tick(1800000005.9), [])
        self.insert(con, 103, "buffer activity", stamp=1800000006000)
        main.execute("create table turn_file_change_sets (change_set_id,turn_id)")
        main.execute("create table turn_file_change_files (change_set_id,display_path,operation,additions,deletions)")
        main.execute("insert into turn_file_change_sets values ('set','turn')")
        main.execute("insert into turn_file_change_files values ('set','file.py','edit',1,0)")
        main.execute("insert into chat_session_messages "
                     "(session_id,turn_id,sequence,payload_json,role,created_at) values (?,?,?,?,?,?)",
                     ("session", "turn", 2, json.dumps({"parts": [{"type": "text", "text": "main activity"}]}),
                      "assistant", 1800000006000))
        main.commit()
        steps = ad.tick(1800000006)
        self.assertEqual([s.kind for s in steps], [K_ANSWER, K_ANSWER, K_EDIT, K_ASK])
        self.assertTrue(all(s.meta["phase"] == "asking" for s in steps))
        self.assertEqual(ad.phase, "asking")
        self.assertEqual(ad.tick(1800000010.9), [])
        self.assertEqual([s.kind for s in ad.tick(1800000011)], [K_ASK])

    def test_native_main_tool_after_eight_parts_and_in_place_resolution_poll(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        payload = self.native_row(main, prefix=10)
        steps = ad.tick(1800000001)
        self.assertEqual(steps[-1].kind, K_ASK)
        self.assertNotIn("private", repr(steps))
        self.native_buffer(con, 101, [self.question_block()], offset=2)
        self.assertEqual(ad.tick(1800000002), [])
        payload["parts"][-1]["tool"].update(status="completed", completedAt="now",
                                            resultMeta={"answers": ["private result"]})
        main.execute("update chat_session_messages set payload_json=?,updated_at=?",
                     (json.dumps(payload), 1800000003000))
        main.commit()
        ad.main.q = Mock(wraps=ad.main.q)
        with patch("adapters.qoder.json.loads", side_effect=AssertionError("reread full payload")):
            self.assertEqual(ad.tick(1800000002.9), [])
            steps = ad.tick(1800000003)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertEqual(steps[0].meta["phase"], P_THINKING)
        self.assertEqual(ad.phase, P_THINKING)
        self.assertNotIn("private", repr(steps))
        queries = [c for c in ad.main.q.call_args_list if "$.tool.status" in c.args[0]]
        self.assertEqual(len(queries), 1)
        sql, args = queries[0].args
        self.assertEqual(args, ("session", "turn", "call-1"))
        self.assertIn("limit 16", sql)
        plan = main.execute("explain query plan " + sql, args).fetchall()
        self.assertTrue(any("turn_sequence" in str(row) for row in plan))
        self.assertFalse(any("SCAN chat_session_messages" in str(row) for row in plan))
        ad.main.q.reset_mock()
        self.assertEqual(ad.tick(1800000010), [])
        self.assertFalse(any("$.tool.status" in c.args[0] for c in ad.main.q.call_args_list))
        self.native_buffer(con, 102, [self.question_block()], offset=11)
        self.assertEqual(ad.tick(1800000011), [])

    def test_native_main_poll_uses_latest_foreground_exact_call_only(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.native_buffer(con, 101, [self.question_block()])
        ad.tick(1800000001)
        self.native_row(main, state="completed", seq=1)
        payload = self.native_row(main, seq=2)
        for seq, extra in ((3, {"backgroundTaskId": "bg"}), (4, {"responseKind": "notification"})):
            background = self.native_row(main, state="failed", seq=seq)
            background.update(extra)
            main.execute("update chat_session_messages set payload_json=? where sequence=?",
                         (json.dumps(background), seq))
        main.commit()
        ad.mcur = (1800000001000, 4)
        self.assertEqual(ad.tick(1800000002), [])
        for n, change in enumerate(({"id": "wrong"}, {"name": "OtherTool"},
                                    {"parentToolUseId": "subagent"})):
            tool = dict(payload["parts"][0]["tool"], status="completed", **change)
            main.execute("update chat_session_messages set payload_json=? where sequence=2",
                         (json.dumps({"parts": [{"type": "tool", "tool": tool}]}),))
            main.commit()
            self.assertEqual(ad.tick(1800000003 + n), [])
            self.assertTrue(ad._questions)
        payload["parts"][0]["tool"].update(status="failed", resultMeta={"nonExecutionKind": "interrupted"})
        main.execute("update chat_session_messages set payload_json=? where sequence=2", (json.dumps(payload),))
        main.commit()
        steps = ad.tick(1800000006)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertEqual(steps[0].meta["phase"], P_THINKING)
        self.assertFalse(ad._questions)
        self.assertFalse(ad._closed)

    def test_native_terminal_interrupt_without_result_uuid_clears_wait(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        payload = self.native_row(main)
        self.assertEqual([s.kind for s in ad.tick(1800000001)], [K_ASK])
        payload["completedAt"] = "2026-10-29T00:22:09.523Z"
        payload["parts"][0]["tool"].update(status="failed", completedAt="now",
                                          resultMeta={"nonExecutionKind": "interrupted"})
        main.execute("update chat_session_messages set payload_json=?,status='interrupted',updated_at=?",
                     (json.dumps(payload), 1800000006000))
        main.commit()
        steps = ad.tick(1800000006)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertTrue(steps[0].meta["terminal"])
        self.assertEqual(steps[0].meta["phase"], "cancelled")
        self.assertEqual(steps[0].meta["evidence"], "chat_session_messages.status+completedAt")
        self.assertEqual(ad.phase, "cancelled")
        self.assertFalse(ad._questions)
        self.native_buffer(con, 101, [self.question_block()], offset=7)
        self.native_buffer(con, 102, [{"type": "tool_result", "tool_use_id": "call-1", "content": []}],
                           role="user", offset=8)
        self.assertEqual(ad.tick(1800000008), [])
        self.assertEqual(ad.tick(1800000015), [])

    def test_terminal_requires_completion_evidence_and_preserves_background_exclusions(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        for n, (status, extra, expected) in enumerate((
                ("completed", {}, None), ("failed", {"completedAt": None}, None),
                ("failed", {}, K_ERROR), ("canceled", {}, K_STATUS),
                ("interrupted", {"backgroundTaskId": "bg"}, None),
                ("interrupted", {"responseKind": "notification"}, None))):
            tid = "turn-%s" % n
            self.final_row(main, status=status, tid=tid, resultUuid=None, **extra)
            ad._watch("session", tid)
            ad.mcur = (1800000000001, 2)
            steps = ad.tick(1800000001 + n)
            self.assertEqual([s.kind for s in steps], [] if expected is None else [expected])
            self.assertEqual(("session", tid) in ad._closed, expected is not None)

    def test_new_user_turn_and_polled_supersession_are_session_scoped(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.native_buffer(con, 101, [self.question_block()])
        self.native_buffer(con, 102, [self.question_block()], sid="other")
        self.assertEqual(len(ad.tick(1800000001)), 2)
        self.native_buffer(con, 103, "new request", role="user", offset=2, tid="next")
        steps = ad.tick(1800000002)
        self.assertEqual([s.kind for s in steps], [K_USER])
        self.assertEqual(list(ad._questions), [("other", "turn")])
        self.native_buffer(con, 104, [self.question_block()], offset=3, tid="next")
        self.assertEqual([s.kind for s in ad.tick(1800000003)], [K_ASK])
        self.native_buffer(con, 105, [self.question_block("late-old")], offset=4)
        self.native_buffer(con, 106, [{"type": "tool_result", "tool_use_id": "call-1", "content": []}],
                           role="user", offset=4)
        self.assertEqual(ad.tick(1800000004), [])
        self.assertIn("call-1", ad._questions[("session", "next")])
        main.execute("insert into chat_session_messages "
                     "(session_id,turn_id,sequence,payload_json,role,created_at) values (?,?,?,?,?,?)",
                     ("session", "last", 10, json.dumps({"parts": [{"type": "text", "text": "new prompt"}]}),
                      "user", 1800000005000))
        main.commit()
        ad.mcur = (1800000005000, 10)
        ad.tick(1800000005)
        self.assertEqual(list(ad._questions), [("other", "turn")])
        self.assertEqual([s.session for s in ad.tick(1800000006)], ["other"])

    def test_main_tool_result_envelope_and_other_session_do_not_supersede(self):
        ad, con, main = self.adapter()
        self.terminal_schema(main)
        self.native_buffer(con, 101, [self.question_block()])
        ad.tick(1800000001)
        parts = [{"type": "text", "text": "tool envelope"},
                 {"type": "tool_result", "tool_use_id": "unrelated", "content": []}]
        for sid, tid, seq in (("session", "different-turn", 2), ("other", "next", 3)):
            main.execute("insert into chat_session_messages "
                         "(session_id,turn_id,sequence,payload_json,role,created_at) values (?,?,?,?,?,?)",
                         (sid, tid, seq, json.dumps({"parts": parts}), "user", 1800000002000))
        main.commit()
        self.assertEqual(ad.tick(1800000002), [])
        self.assertEqual(list(ad._questions), [("session", "turn")])
        self.assertEqual([s.kind for s in ad.tick(1800000006)], [K_ASK])

    def test_native_same_tick_resolution_never_refreshes_resolved_call(self):
        ad, con, main = self.adapter()
        self.native_buffer(con, 101, [self.question_block()])
        self.native_buffer(con, 102, [{"type": "tool_result", "tool_use_id": "call-1", "content": []}],
                           role="user")
        self.assertEqual([s.kind for s in ad.tick(1800000001)], [K_STATUS])
        self.assertEqual(ad.phase, P_THINKING)
        self.assertEqual(ad.tick(1800000006), [])
        self.native_buffer(con, 103, [self.question_block("call-2"), self.question_block("call-3")], offset=7)
        self.assertEqual([s.kind for s in ad.tick(1800000007)], [K_ASK, K_ASK])
        self.native_buffer(con, 104, [{"type": "tool_result", "tool_use_id": "call-2", "content": []}],
                           role="user", offset=8)
        steps = ad.tick(1800000008)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertEqual(steps[0].meta["phase"], "asking")
        self.assertEqual(ad.phase, "asking")
        steps = ad.tick(1800000012)
        self.assertEqual([s.meta["callId"] for s in steps], ["call-3"])
        self.native_buffer(con, 105, [{"type": "tool_result", "tool_use_id": "call-3", "content": ""}],
                           role="user", offset=13)
        self.assertEqual(ad.tick(1800000013)[0].meta["phase"], P_THINKING)

    def test_native_main_resolved_snapshot_prevents_late_request_reopening(self):
        ad, con, main = self.adapter()
        self.native_row(main, state="completed")
        self.native_row(main, state="failed", id="call-2", seq=3)
        self.assertEqual(ad.tick(1800000001), [])
        self.native_buffer(con, 101, [self.question_block(), self.question_block("call-2")], offset=2)
        self.assertEqual(ad.tick(1800000002), [])
        self.assertEqual(ad.tick(1800000010), [])
        self.assertFalse(ad._questions)

    def test_unrelated_tool_result_text_is_preserved_without_clearing_wait(self):
        ad, con, main = self.adapter()
        self.native_buffer(con, 101, [self.question_block()])
        ad.tick(1800000001)
        for n in range(2):
            self.native_buffer(con, 102 + n,
                               [{"type": "tool_result", "tool_use_id": "read-call", "content": "file read"}],
                               role="user", offset=2 + n)
            steps = ad.tick(1800000002 + n)
            self.assertEqual([s.kind for s in steps], [K_TOOL_DONE])
            self.assertEqual(steps[0].meta["phase"], "asking")
        self.assertTrue(ad._questions)

    def test_native_missing_identity_and_subagent_do_not_open_wait(self):
        ad, con, main = self.adapter()
        for n, call in enumerate((None, "", [], "x" * 257)):
            self.native_buffer(con, 101 + n, [self.question_block(call)], offset=1 + n)
            self.assertEqual(ad.tick(1800000001 + n), [])
        self.native_buffer(con, 105, [self.question_block()], offset=5, parent="subagent")
        self.native_row(main, parentToolUseId="subagent")
        self.assertEqual(ad.tick(1800000005), [])
        self.assertFalse(ad._questions)

    def test_native_tracking_calls_tombstones_and_watcher_eviction_are_bounded(self):
        ad, con, main = self.adapter()
        for n in range(300):
            ad._start_question("session", "turn", str(n), n, 1800000001)
        self.assertEqual(len(ad._questions[("session", "turn")]), ad.QUESTION_CALLS)
        self.assertEqual(len(ad._question_history[("session", "turn")]), 256)
        for call in list(ad._questions[("session", "turn")]):
            ad._resolve_question("session", "turn", call, 1, 1800000002)
        self.assertFalse(ad._questions)
        self.assertEqual(ad._start_question("session", "turn", "299", 1, 1800000003), [])
        for n in range(300):
            ad._start_question("s" + str(n), "turn", "call", n, 1800000004)
        self.assertEqual(len(ad._questions), ad.QUESTION_LIMIT)
        self.assertEqual(len(ad._watching), 128)
        self.assertLessEqual(len(ad._question_history), 256)
        for n in range(128):
            ad._watch("eviction", str(n))
        self.assertFalse(ad._questions)
        self.assertLessEqual(len(ad._question_history), 256)
        self.terminal_schema(main)
        for n in range(260):
            self.final_row(main, tid=str(n))
            ad._watch("session", str(n))
            ad._poll_terminal(1800000010 + n)
        self.assertEqual(len(ad._closed), 256)


class DoubaoWorkTests(Samples):
    """Synthetic lines mirroring the real `saman_*.log` shapes (field names only)."""

    def adapter(self):
        root = self.path("DoubaoWork")
        logdir = os.path.join(root, "User Data", "sdk_storage", "log")
        os.makedirs(logdir, exist_ok=True)
        log = os.path.join(logdir, "saman_2026.1029.0.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write('[1:2:1029/120000.000:INFO:CONSOLE(0)] [neotix] [desktop] "{}"\n')
        ad = DoubaoWorkAdapter({"roots": [root], "exes": []})
        self.assertTrue(ad.setup())
        ad._scan_at = 10 ** 12          # 测试中不要重扫目录
        return ad, log

    @staticmethod
    def office(name, **meta):
        return ('[1:2:1029/120000.000:INFO:CONSOLE(0)] [neotix] [office-api-logger] "'
                + json.dumps({"eventName": name, "meta": meta}) + '", source:  (0)\n')

    @staticmethod
    def sse(event, data):
        return '  "data": ' + json.dumps({"event": event, "data": data}) + '\n'

    @staticmethod
    def append(log, lines):
        with open(log, "a", encoding="utf-8") as f:
            f.writelines(lines)

    def test_frontend_log_counts_but_repair_does_not(self):
        ad, _ = self.adapter()
        names = ["saman_2026.1029.1.log", "saman_frontend_2026.1029.0.log",
                 "saman_repair_2026.1029.0.log", "saman_crashpad_handler_2026.1029.log"]
        self.assertEqual([n for n in names if ad._is_task_log(n)],
                         ["saman_2026.1029.1.log", "saman_frontend_2026.1029.0.log"])

    def test_new_version_events_from_frontend_log(self):
        ad, _ = self.adapter()
        front = os.path.join(os.path.dirname(ad._paths[0]),
                             "saman_frontend_2026.1029.0.log")
        with open(front, "w", encoding="utf-8") as f:
            f.write("")
        ad._scan_at = 0.0
        ad.tick(1800000000)
        self.assertTrue(any(os.path.basename(t.path).startswith("saman_frontend_")
                            for t in ad.tails))
        with open(front, "a", encoding="utf-8") as f:
            f.writelines([
                self.office("on_message_session_start", chat_session_id="S",
                            send_message_id="M1", resumed="0"),
                self.office("on_message_receiving_start", chat_session_id="S",
                            receive_message_id="R1", send_message_id="M1"),
                self.office("on_message_session_end", chat_session_id="S",
                            receive_message_id="R1", session_success="1",
                            agent_error="0"),
            ])
        steps = ad.tick(1800000001)
        self.assertEqual([s.kind for s in steps], [K_USER, K_THINK, K_RESULT])
        self.assertTrue(steps[-1].meta.get("terminal"))

    def test_only_session_end_completes_and_turn_stays_continuous(self):
        ad, log = self.adapter()
        self.append(log, [
            self.office("on_message_session_start", chat_session_id="S",
                        send_message_id="M1", resumed="0"),
            self.office("on_message_receiving_start", chat_session_id="S",
                        receive_message_id="R1", send_message_id="M1"),
            self.office("flow_message_time", answer_id="R1", message_id="X1",
                        ev_name="cot_first_token"),
            self.office("flow_message_time", answer_id="R1", message_id="X1",
                        ev_name="answer_first_token"),
            self.office("rd_flow_message_streaming_finished", message_id="R1",
                        replied_message_id="P1", is_interrupted="0"),
            self.office("message_task_association_reply_message_ended",
                        reply_message_id="P1", end_status="Success"),
            self.office("on_message_receiving_end", chat_session_id="S",
                        receive_message_id="R1", receive_success="1"),
        ])
        steps = ad.tick(1800000000)
        self.assertEqual([s.kind for s in steps],
                         [K_USER, K_THINK, K_THINK, K_ANSWER, K_STATUS, K_STATUS])
        self.assertEqual({s.session for s in steps}, {"S"})
        # flow / 流式 / 回复结束都不带会话 ID，必须落到同一会话的同一轮次上，
        # 否则后面的 session_end 会被当成旧轮而吞掉完成态。
        self.assertEqual({s.turn for s in steps[1:]}, {"R1"})
        self.assertNotIn(K_RESULT, [s.kind for s in steps])
        self.assertNotIn(K_ASK, [s.kind for s in steps])

        self.append(log, [self.office("on_message_session_end", chat_session_id="S",
                                      receive_message_id="R1", session_success="1",
                                      agent_error="0")])
        done = ad.tick(1800000001)
        self.assertEqual([s.kind for s in done], [K_RESULT])
        self.assertEqual(done[0].turn, "R1")
        self.assertTrue(done[0].meta.get("terminal"))

    def test_repeated_identical_lines_collapse_to_one_step(self):
        ad, log = self.adapter()
        start = self.office("on_message_session_start", chat_session_id="S",
                            send_message_id="M1", resumed="0")
        self.append(log, [start])
        self.assertEqual([s.kind for s in ad.tick(1800000000)], [K_USER])
        done_reply = self.office("on_message_receiving_end", chat_session_id="S",
                                 receive_message_id="M1", receive_success="1")
        self.append(log, [done_reply, done_reply])
        self.assertEqual([s.kind for s in ad.tick(1800000001)], [K_STATUS])
        self.append(log, [done_reply])
        self.assertEqual([s.kind for s in ad.tick(1800000011)], [K_STATUS])

        end = self.office("on_message_session_end", chat_session_id="S",
                          receive_message_id="M1", session_success="1",
                          agent_error="0")
        self.append(log, [end, end])
        self.assertEqual([s.kind for s in ad.tick(1800000012)], [K_RESULT])

    def test_session_start_inside_open_session_is_not_a_new_task(self):
        ad, log = self.adapter()
        self.append(log, [self.office("on_message_session_start", chat_session_id="S",
                                      send_message_id="M1")])
        self.assertEqual([s.kind for s in ad.tick(1800000000)], [K_USER])
        self.append(log, [self.office("on_message_session_start", chat_session_id="S",
                                      receive_message_id="R1", send_message_id="M1")])
        self.assertEqual(ad.tick(1800000001), [])
        self.append(log, [self.office("on_message_session_end", chat_session_id="S",
                                      receive_message_id="R1", session_success="1",
                                      agent_error="0")])
        self.assertEqual([s.kind for s in ad.tick(1800000002)], [K_RESULT])
        self.append(log, [self.office("on_message_session_start", chat_session_id="S",
                                      send_message_id="M2")])
        self.assertEqual([s.kind for s in ad.tick(1800000003)], [K_USER])

    def test_ambiguous_flow_and_toolcalls_are_not_attributed(self):
        ad, log = self.adapter()
        self.append(log, [
            self.office("on_message_session_start", chat_session_id="A", send_message_id="m-a"),
            self.office("on_message_session_start", chat_session_id="B", send_message_id="m-b"),
            self.office("flow_message_time", answer_id="r-a", ev_name="cot_first_token"),
            self.sse("toolcall", {"name": "shell", "status": "received", "tcid": "t1"}),
        ])
        self.assertEqual([s.kind for s in ad.tick(1800000000)], [K_USER, K_USER])

        self.append(log, [self.office("on_message_session_end", chat_session_id="B",
                                      receive_message_id="r-b", session_success="1")])
        self.assertEqual([(s.kind, s.session) for s in ad.tick(1800000001)],
                         [(K_RESULT, "B")])

        self.append(log, [
            self.office("flow_message_time", answer_id="r-a", ev_name="cot_first_token"),
            self.sse("toolcall", {"name": "shell", "status": "received", "tcid": "t1"}),
            self.sse("toolcall", {"name": "shell", "status": "received", "tcid": "t1"}),
            self.sse("toolcall", {"name": "shell", "status": "success", "tcid": "t1"}),
        ])
        self.assertEqual([(s.kind, s.session, s.turn) for s in ad.tick(1800000002)],
                         [(K_THINK, "A", "m-a"), (K_TOOL, "A", "m-a"),
                          (K_TOOL_DONE, "A", "m-a")])

    def test_session_end_cancel_and_error_are_not_success(self):
        ad, log = self.adapter()
        self.append(log, [
            self.office("on_message_session_start", chat_session_id="C", send_message_id="m-c"),
            self.office("on_message_session_end", chat_session_id="C", receive_message_id="r-c",
                        cancel_source="user"),
            self.office("on_message_session_start", chat_session_id="E", send_message_id="m-e"),
            self.office("on_message_session_end", chat_session_id="E", receive_message_id="r-e",
                        agent_error="500", session_success="0"),
            self.office("on_message_session_start", chat_session_id="K", send_message_id="m-k"),
            self.office("on_message_session_end", chat_session_id="K", receive_message_id="r-k",
                        session_success="1", agent_error="0"),
        ])
        ends = {s.session: s for s in ad.tick(1800000000)
                if s.kind in (K_STATUS, K_ERROR, K_RESULT)}
        self.assertEqual(ends["C"].meta.get("outcome"), "cancelled")
        self.assertNotIn("terminal", ends["C"].meta)
        self.assertEqual(ends["E"].kind, K_ERROR)
        self.assertTrue(ends["E"].meta.get("terminal"))
        self.assertEqual(ends["K"].kind, K_RESULT)
        self.assertTrue(ends["K"].meta.get("terminal"))

    def test_command_and_toolcall_dedupe(self):
        ad, log = self.adapter()
        self.append(log, [
            self.office("on_message_session_start", chat_session_id="S", send_message_id="M"),
            self.office("on_message_receiving_start", chat_session_id="S",
                        receive_message_id="R", send_message_id="M"),
            self.sse("command", {"name": "bash", "command_id": "c1", "status": "received"}),
            self.sse("command", {"name": "bash", "command_id": "c1", "status": "received"}),
            self.sse("command", {"name": "bash", "command_id": "c1", "status": "success"}),
            self.sse("toolcall", {"name": "shell", "status": "received", "tcid": "t1"}),
            self.sse("toolcall", {"name": "shell", "status": "success", "tcid": "t1"}),
            self.sse("toolcall", {"name": "shell", "status": "success", "tcid": "t1"}),
        ])
        steps = ad.tick(1800000000)
        self.assertEqual([s.kind for s in steps],
                         [K_USER, K_THINK, K_TOOL, K_TOOL_DONE, K_TOOL, K_TOOL_DONE])
        self.assertEqual([s.meta.get("tool") for s in steps[2:]],
                         ["bash", "bash", "shell", "shell"])
        self.assertEqual({s.turn for s in steps[2:]}, {"R"})

    def test_unrelated_and_malformed_lines_are_ignored(self):
        ad, log = self.adapter()
        self.append(log, [
            '  get_domain_default_json: {"x": 1}\n',
            '  "data": {"event": {"engine_startup": 1}}\n',
            self.office("shell_api_neotix_log", chat_session_id="S"),
            self.office("on_message_receiving_start", chat_session_id="",
                        receive_message_id="r"),
            'not json at all\n',
        ])
        self.assertEqual(ad.tick(1800000000), [])

    def test_resumed_session_and_no_ask(self):
        ad, log = self.adapter()
        self.append(log, [self.office("on_message_session_start", chat_session_id="S",
                                      send_message_id="M", resumed="1")])
        steps = ad.tick(1800000000)
        self.assertEqual([(s.kind, s.text) for s in steps],
                         [(K_USER, "继续未完成的任务")])

    def test_open_session_expires_without_faking_terminal(self):
        ad, _ = self.adapter()
        ad._touch("S", "T", open_=True, now=1000.0)
        ad._expire(1000.0 + ad.OPEN_TTL + 1)
        self.assertFalse(ad._sess["S"]["open"])
        self.assertIn("S", ad._sess)
        ad._expire(1000.0 + ad.SESSION_TTL + 1)
        self.assertNotIn("S", ad._sess)


class DoubaoWorkRegistrationTests(unittest.TestCase):
    def test_engine_uses_the_dedicated_a_level_adapter(self):
        from core import discover
        from core.engine import ADAPTERS
        self.assertEqual(ADAPTERS["doubao_work"],
                         ("adapters.doubao_work", "DoubaoWorkAdapter"))
        spec = next(s for s in discover.SPECS if s["key"] == "doubao_work")
        self.assertEqual(spec["tier"], "A")


class WorkBuddyTests(Samples):
    # Sanitized metadata from the 2026-10-28 GUI run. No prompt/auth/transport data.
    SDK_TIME = datetime.datetime(2026, 10, 28, 9, 43, 40,
                                 tzinfo=datetime.timezone.utc).timestamp()

    def sdk_line(self, label, offset, sid="conversation", iid="ci-2", **data):
        stamp = datetime.datetime.fromtimestamp(self.SDK_TIME + offset,
                    datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        obj = dict(conversationId=sid, instanceId=iid, **data)
        return (stamp + " " + label + " " + json.dumps(obj) + "\n").encode()

    def sdk_start(self, request="request-1", offset=0, **ids):
        return (self.sdk_line("state-machine:transition", offset + 2.427,
                             input="PROMPT_SENT", valid=True, to="working", **ids)
                + self.sdk_line("event-machine:dispatch", offset + 4.769,
                                input="agent_message_chunk", requestId=request, **ids))

    def sdk_stop(self, offset=0, **data):
        obj = dict(event="TURN_COMPLETED", stopReason="end_turn", requestState="completed",
                   hasFatalError=False, hasTurnError=False)
        obj.update(data)
        return self.sdk_line("runtime.applyStopReason", offset + 11.508, **obj)

    def sdk_projections(self, offset=0):
        return (self.sdk_line("state-machine:transition", offset + 11.509,
                             input="TURN_COMPLETED", valid=True, finalState="completed", to="idle")
                + self.sdk_line("runtime-status:persisted", offset + 11.526,
                                status="completed", previous="working", state="idle"))

    def sdk_adapter(self, seed=b"", startup=0):
        ad = WorkBuddyAdapter({"roots": [], "exes": []})
        ad.logs_dir = self.path("logs")
        folder = self.path("logs/2026-10-28/sdk/conversations")
        os.makedirs(folder)
        path = os.path.join(folder, "conversation.log")
        self.write(path, seed)
        ad._pick_sdk(first=True, now=self.SDK_TIME + startup)
        ad._switch_at = float("inf")
        return ad, path

    def chat_line(self, role="assistant", request="request-1", offset=11.304, **extra):
        obj = dict(type="message", role=role, sessionId="conversation",
                   timestamp=(self.SDK_TIME + offset) * 1000,
                   content="PETHUB-WORKBUDDY-OK", status="completed",
                   providerData={"conversationRequestId": request, "stepSeq": 7})
        obj.update(extra)
        return json.dumps(obj)

    def test_sdk_gui_completed_once_with_project_request_identity(self):
        ad, path = self.sdk_adapter()
        chat = self.path("conversation.jsonl")
        self.write(chat, b"")
        ad.chat = TailReader(chat)
        self.write(chat, (self.chat_line("user", offset=2.187) + "\n"
                          + self.chat_line() + "\n").encode(), "ab")
        self.write(path, self.sdk_start(), "ab")
        steps = ad.tick(self.SDK_TIME + 11.4)
        self.assertEqual([s.kind for s in steps], [K_USER, K_ANSWER])
        self.assertTrue(all(s.turn == "request-1" for s in steps))
        self.assertNotEqual(ad.phase, P_DONE)  # assistant.status=completed isn't terminal
        self.write(path, self.sdk_stop() + self.sdk_projections() + self.sdk_stop(), "ab")
        steps = ad.tick(self.SDK_TIME + 12)
        self.assertEqual([s.kind for s in steps], [K_RESULT])
        self.assertEqual((steps[0].session, steps[0].turn), ("conversation", "request-1"))
        self.assertAlmostEqual(steps[0].ts, self.SDK_TIME + 11.508, places=3)
        self.assertEqual(steps[0].meta["evidence"], "sdk.runtime.applyStopReason")
        self.assertEqual(ad.phase, P_DONE)
        self.assertEqual(ad.tick(self.SDK_TIME + 13), [])
        # A late archive copy cannot reopen a completed request.
        self.write(chat, (self.chat_line() + "\n").encode(), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 14), [])
        self.assertEqual(ad.phase, P_DONE)

    def test_sdk_closed_request_ignores_unlinked_late_archive(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_stop(), 'ab')
        self.assertEqual(ad.tick(self.SDK_TIME + 12)[-1].kind, K_RESULT)
        self.assertEqual(ad._msg(self.chat_line(providerData={}), self.SDK_TIME + 14), [])
        self.assertEqual(ad.phase, P_DONE)

    def sdk_question(self, offset=6, call="call-1", **ids):
        return (self.sdk_line("event-machine:dispatch", offset,
                             requestId="request-1", input="tool_call", toolCallId=call, **ids)
                + self.sdk_line("ask-user-question:pending-created", offset + 1,
                                toolCallId=call, toolName="AskUserQuestion", **ids))

    def test_question_span_cannot_create_an_independent_task(self):
        ad = WorkBuddyAdapter({})
        for kind in ("span_start", "span_end"):
            line = json.dumps({"type": kind, "span": {
                "traceId": "detached-trace", "spanId": "detached-span",
                "name": "AskUserQuestion", "toolName": "AskUserQuestion",
                "type": "function", "status": "ok"}})
            self.assertEqual(ad._span(line, self.SDK_TIME), [])
        self.assertFalse(ad._open_span)

    def test_span_joins_conversation_row_instead_of_trace_row(self):
        ad = WorkBuddyAdapter({})
        self.assertEqual([s.kind for s in ad._msg(self.chat_line("user"), self.SDK_TIME)], [K_USER])
        span = json.dumps({"type": "span_start", "span": {
            "traceId": "trace_abc", "spanId": "span_1", "toolName": "Bash",
            "type": "function", "startedAt": "2026-10-28T09:43:42.000Z"}})
        steps = ad._span(span, self.SDK_TIME + 2)
        self.assertEqual([(s.kind, s.session, s.turn) for s in steps],
                         [(K_TOOL, "conversation", "request-1")])
        end = json.dumps({"type": "span_end", "span": {
            "traceId": "trace_abc", "spanId": "span_1", "toolName": "Bash",
            "type": "function", "status": "ok", "duration": 1000}})
        steps = ad._span(end, self.SDK_TIME + 3)
        self.assertEqual([(s.kind, s.session, s.turn) for s in steps],
                         [(K_TOOL_DONE, "conversation", "request-1")])

    def test_span_before_events_falls_back_to_chat_session_file(self):
        ad = WorkBuddyAdapter({})
        ad.projects_dir = self.root
        chat = self.path("1798efd5-113e-4403-acf3-85eb51ea950d.jsonl")
        self.write(chat, b"")
        ad._pick_chat(first=True)
        span = json.dumps({"type": "span_start", "span": {
            "traceId": "trace_abc", "spanId": "span_1", "toolName": "Read",
            "type": "function"}})
        steps = ad._span(span, self.SDK_TIME)
        self.assertEqual((steps[0].session, steps[0].turn),
                         ("1798efd5-113e-4403-acf3-85eb51ea950d", "span_1"))

    def test_sdk_question_wait_usage_refresh_respond_and_complete(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_question(), "ab")
        asks = [s for s in ad.tick(self.SDK_TIME + 7) if s.kind == K_ASK]
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0].session, asks[0].turn), ("conversation", "request-1"))
        self.assertEqual(asks[0].meta["tool"], "AskUserQuestion")
        self.write(path, self.sdk_line("event-machine:dispatch", 8,
                   input="usage_update", requestId="request-1"), "ab")
        self.assertFalse(any(s.kind == K_STATUS for s in ad.tick(self.SDK_TIME + 8)))
        asks = [s for s in ad.tick(self.SDK_TIME + 12) if s.kind == K_ASK]
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0].session, asks[0].turn), ("conversation", "request-1"))
        self.assertEqual(asks[0].meta["tool"], "AskUserQuestion")
        self.write(path, self.sdk_line("ask-user-question:respond", 13,
                   toolCallId="call-1", decision="allow"), "ab")
        steps = ad.tick(self.SDK_TIME + 13)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertEqual(steps[0].meta["phase"], P_THINKING)
        self.assertEqual((steps[0].session, steps[0].turn), ("conversation", "request-1"))
        self.write(path, self.sdk_stop(offset=3), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 15)], [K_RESULT])
        self.assertEqual(ad.tick(self.SDK_TIME + 20), [])

    def test_sdk_question_requires_bound_call_and_matching_identity(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_line("event-machine:dispatch", 6,
                   requestId="request-1", input="tool_call", toolCallId="call-1"), "ab")
        ad.tick(self.SDK_TIME + 6)
        for n, ids in enumerate(({"toolCallId": "unbound"},
                                 {"sid": "other-conversation"}, {"iid": "other-instance"})):
            with self.subTest(ids=ids):
                data = dict(toolCallId="call-1", toolName="AskUserQuestion")
                data.update(ids)
                self.write(path, self.sdk_line("ask-user-question:pending-created", 7 + n,
                                               **data), "ab")
                self.assertEqual(ad.tick(self.SDK_TIME + 7 + n), [])
        self.assertEqual(ad.tick(self.SDK_TIME + 15), [])
        self.write(path, self.sdk_line("ask-user-question:pending-created", 16,
                   toolCallId="call-1", toolName="AskUserQuestion"), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 16)], [K_ASK])

    def test_sdk_question_wrong_response_does_not_clear_wait(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_question(), "ab")
        self.assertIn(K_ASK, [s.kind for s in ad.tick(self.SDK_TIME + 7)])
        for n, ids in enumerate(({"toolCallId": "wrong-call"},
                                 {"sid": "other-conversation"}, {"iid": "other-instance"})):
            with self.subTest(ids=ids):
                data = dict(toolCallId="call-1", decision="allow")
                data.update(ids)
                self.write(path, self.sdk_line("ask-user-question:respond", 8 + n, **data), "ab")
                self.assertEqual(ad.tick(self.SDK_TIME + 8 + n), [])
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 12)], [K_ASK])
        self.write(path, self.sdk_line("ask-user-question:respond", 13,
                   toolCallId="call-1", decision="allow"), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 13)], [K_STATUS])
        self.assertEqual(ad.tick(self.SDK_TIME + 18), [])

    def test_sdk_question_older_events_cannot_create_or_clear_wait(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_line("event-machine:dispatch", 6,
                   requestId="request-1", input="tool_call", toolCallId="call-1"), "ab")
        ad.tick(self.SDK_TIME + 6)
        self.write(path, self.sdk_line("ask-user-question:pending-created", 1,
                   toolCallId="call-1", toolName="AskUserQuestion"), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 7), [])
        self.write(path, self.sdk_line("ask-user-question:pending-created", 8,
                   toolCallId="call-1", toolName="AskUserQuestion"), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 8)], [K_ASK])
        self.write(path, self.sdk_line("ask-user-question:respond", 7,
                   toolCallId="call-1", decision="allow"), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 9), [])
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 13)], [K_ASK])

    def test_sdk_question_terminal_clears_wait_and_ignores_late_events(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_question(), "ab")
        self.assertIn(K_ASK, [s.kind for s in ad.tick(self.SDK_TIME + 7)])
        self.write(path, self.sdk_stop(), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 12)], [K_RESULT])
        self.write(path, self.sdk_question(offset=13)
                   + self.sdk_line("ask-user-question:respond", 15,
                                   toolCallId="call-1", decision="allow"), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 16), [])
        self.assertEqual(ad.tick(self.SDK_TIME + 21), [])
        self.assertEqual(ad.phase, P_DONE)

    def test_sdk_question_new_prompt_clears_wait_and_old_call_binding(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_question(), "ab")
        self.assertIn(K_ASK, [s.kind for s in ad.tick(self.SDK_TIME + 7)])
        self.write(path, self.sdk_line("state-machine:transition", 20,
                   input="PROMPT_SENT", valid=True, to="working"), "ab")
        self.assertNotIn(K_ASK, [s.kind for s in ad.tick(self.SDK_TIME + 20)])
        self.assertNotIn(K_ASK, [s.kind for s in ad.tick(self.SDK_TIME + 25)])
        self.write(path, self.sdk_line("event-machine:dispatch", 26,
                   input="agent_message_chunk", requestId="request-2")
                   + self.sdk_line("ask-user-question:pending-created", 27,
                                   toolCallId="call-1", toolName="AskUserQuestion")
                   + self.sdk_line("ask-user-question:respond", 28,
                                   toolCallId="call-1", decision="allow"), "ab")
        steps = ad.tick(self.SDK_TIME + 29)
        self.assertEqual([(s.kind, s.turn) for s in steps], [(K_STATUS, "request-2")])
        self.assertEqual(ad.tick(self.SDK_TIME + 34), [])

    def test_sdk_question_startup_does_not_restore_historical_wait(self):
        ad, path = self.sdk_adapter(self.sdk_start() + self.sdk_question(), startup=10)
        self.assertEqual(ad.tick(self.SDK_TIME + 10), [])
        self.assertEqual(ad.tick(self.SDK_TIME + 15), [])
        self.write(path, self.sdk_line("ask-user-question:pending-created", 9,
                   toolCallId="call-1", toolName="AskUserQuestion"), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 16), [])
        self.write(path, self.sdk_line("ask-user-question:pending-created", 10,
                   toolCallId="call-1", toolName="AskUserQuestion"), "ab")
        steps = ad.tick(self.SDK_TIME + 17)
        self.assertEqual([(s.kind, s.session, s.turn) for s in steps],
                         [(K_ASK, "conversation", "request-1")])

    def test_sdk_next_request_excludes_old_terminal_and_projection_duplicates(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_stop(), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 12)], [K_STATUS, K_RESULT])
        self.write(path, self.sdk_start("request-2", offset=20), "ab")
        self.assertEqual([s.turn for s in ad.tick(self.SDK_TIME + 25)], ["request-2"])
        self.assertEqual(ad.phase, P_THINKING)
        self.write(path, self.sdk_stop() + self.sdk_projections(offset=20)
                   + self.sdk_line("event-machine:dispatch", 30, input="usage_update",
                                   requestId="request-1"), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 32), [])
        self.assertEqual(ad.cur_turn, "request-2")
        self.assertEqual(ad.phase, P_THINKING)
        self.write(path, self.sdk_stop(offset=24) + self.sdk_projections(offset=24), "ab")
        steps = ad.tick(self.SDK_TIME + 36)
        self.assertEqual([(s.kind, s.turn) for s in steps], [(K_RESULT, "request-2")])

    def test_sdk_requires_matching_instance_and_active_request(self):
        ad, path = self.sdk_adapter()
        self.write(path, self.sdk_start() + self.sdk_stop(iid="unrelated")
                   + self.sdk_stop(sid="other-conversation")
                   + self.sdk_stop(requestId="wrong-request"), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 12)], [K_STATUS])
        # A new prompt clears the old request even before its first dispatch arrives.
        self.write(path, self.sdk_line("state-machine:transition", 20,
                   input="PROMPT_SENT", valid=True) + self.sdk_stop(offset=10), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 22), [])
        self.write(path, self.sdk_line("event-machine:dispatch", 23,
                   requestId="request-2", input="agent_message_chunk")
                   + self.sdk_stop(offset=13), "ab")
        steps = ad.tick(self.SDK_TIME + 25)
        self.assertEqual([(s.kind, s.turn) for s in steps],
                         [(K_STATUS, "request-2"), (K_RESULT, "request-2")])

    def test_sdk_old_backlog_does_not_complete_newer_project_request(self):
        ad, path = self.sdk_adapter()
        steps = ad._msg(self.chat_line("user", request="request-2", offset=20), self.SDK_TIME + 20)
        self.assertEqual(steps[0].turn, "request-2")
        self.write(path, self.sdk_start() + self.sdk_stop(), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 21), [])
        self.assertEqual(ad.phase, P_RECEIVED)
        self.assertEqual(ad.cur_turn, "request-2")
        self.write(path, self.sdk_start("request-2", offset=20) + self.sdk_stop(offset=20), "ab")
        self.assertEqual([s.turn for s in ad.tick(self.SDK_TIME + 32)], ["request-2"])
        self.assertEqual(ad.phase, P_DONE)

    def test_sdk_explicit_error_flags_override_completed_without_private_error_text(self):
        for flag in ("hasFatalError", "hasTurnError"):
            with self.subTest(flag=flag):
                ad = WorkBuddyAdapter({})
                for line in self.sdk_start().decode().splitlines():
                    ad._sdk_event(ad._sdk_record(line), self.SDK_TIME)
                line = self.sdk_stop(**{flag: True, "errorMessage": "private error detail"}).decode()
                steps = ad._sdk_event(ad._sdk_record(line), self.SDK_TIME)
                self.assertEqual([s.kind for s in steps], [K_ERROR])
                self.assertEqual(steps[0].turn, "request-1")
                self.assertEqual(ad.phase, P_ERROR)
                self.assertNotIn("private", repr(steps[0]))
                self.assertEqual(ad._sdk_event(ad._sdk_record(line), self.SDK_TIME), [])
                for line in self.sdk_projections().decode().splitlines():
                    record = ad._sdk_record(line)
                    if record:
                        self.assertEqual(ad._sdk_event(record, self.SDK_TIME), [])
                self.assertEqual(ad.phase, P_ERROR)

    def test_no_completion_from_title_trace_assistant_status_or_guessed_results(self):
        ad = WorkBuddyAdapter({})
        for typ in ("trace_end", "trace_start"):
            self.assertEqual(ad._span(json.dumps({"type": typ, "status": "ok",
                "trace": {"name": "title-generation", "status": "completed"}}), self.SDK_TIME), [])
        self.assertEqual(ad._msg(self.chat_line(), self.SDK_TIME)[0].kind, K_ANSWER)
        for typ in ("result", "tool_result", "title_result", "permission_result"):
            self.assertEqual(ad._msg(self.chat_line(type=typ), self.SDK_TIME), [])
        self.assertEqual(ad._msg(self.chat_line(type="function_result"), self.SDK_TIME)[0].kind,
                         K_TOOL_DONE)
        self.assertNotEqual(ad.phase, P_DONE)
        for line in self.sdk_start().decode().splitlines():
            ad._sdk_event(ad._sdk_record(line), self.SDK_TIME)
        for data in ({"stopReason": "max_tokens"}, {"requestState": "working"},
                     {"hasTurnError": None}, {"hasFatalError": "false"}, {"event": "OTHER"}):
            record = ad._sdk_record(self.sdk_stop(**data).decode())
            self.assertEqual(ad._sdk_event(record, self.SDK_TIME), [])

    def test_sdk_stale_startup_seeds_only_and_live_terminal_after_startup_is_not_lost(self):
        ad, path = self.sdk_adapter(self.sdk_start() + self.sdk_stop() + self.sdk_projections(), startup=20)
        self.assertEqual(ad.tick(self.SDK_TIME + 20), [])
        self.assertEqual(ad.phase, P_IDLE)
        self.write(path, self.sdk_projections(offset=20) + self.sdk_stop(), "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 32), [])
        self.write(path, self.sdk_start("request-2", offset=40) + self.sdk_stop(offset=40), "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 52)], [K_STATUS, K_RESULT])
        # Startup in the middle of a request must seed its dispatch, not emit history.
        self.write(path, self.sdk_start("request-3", offset=60), "ab")
        ad._pick_sdk(first=True, now=self.SDK_TIME + 66)
        self.assertEqual(ad.tick(self.SDK_TIME + 66), [])
        self.write(path, self.sdk_stop(offset=60), "ab")
        self.assertEqual([(s.kind, s.turn) for s in ad.tick(self.SDK_TIME + 72)],
                         [(K_RESULT, "request-3")])

    def test_sdk_new_session_and_date_rollover_read_initial_writes(self):
        ad, path = self.sdk_adapter()
        os.unlink(path)
        ad._pick_sdk(now=self.SDK_TIME + 1)
        self.write(path, self.sdk_start())
        ad._pick_sdk(now=self.SDK_TIME + 5)
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 5)], [K_STATUS])
        nextday = self.path("logs/2026-10-29/sdk/conversations")
        os.makedirs(nextday)
        newpath = os.path.join(nextday, "conversation.log")
        self.write(newpath, self.sdk_stop(offset=86400))
        ad._pick_sdk(now=self.SDK_TIME + 86412)
        self.assertEqual([(s.kind, s.turn) for s in ad.tick(self.SDK_TIME + 86412)],
                         [(K_RESULT, "request-1")])
        self.assertEqual(ad.tick(self.SDK_TIME + 86413), [])
        # Another session can start and finish entirely between discovery polls.
        session = os.path.join(nextday, "new-session.log")
        self.write(session, self.sdk_start("new-request", offset=86420, sid="new-session")
                   + self.sdk_stop(offset=86420, sid="new-session"))
        ad._pick_sdk(now=self.SDK_TIME + 86435)
        steps = ad.tick(self.SDK_TIME + 86435)
        self.assertEqual([(s.kind, s.session, s.turn) for s in steps],
                         [(K_STATUS, "new-session", "new-request"),
                          (K_RESULT, "new-session", "new-request")])

    def test_sdk_partial_lines_rotation_and_bounded_tail(self):
        ad, path = self.sdk_adapter()
        self.write(path, b"x" * (ad.SDK_TAIL_BYTES * 2) + b"\n" + self.sdk_start(), "ab")
        # Rediscover the same path from a bounded tail, not from byte zero.
        ad._sdk_readers.clear()
        ad._pick_sdk(now=self.SDK_TIME + 5)
        reader = next(iter(ad._sdk_readers.values()))[0]
        self.assertGreater(reader.off, 0)
        self.assertIsInstance(reader, TailReader)
        steps = []
        for _ in range(4):
            steps.extend(ad.tick(self.SDK_TIME + 5))
            self.assertLessEqual(len(reader.buf), reader.max_line)
            self.assertLessEqual(len(reader._pending), reader.max_step)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        stop = self.sdk_stop()
        self.write(path, stop[:-1], "ab")
        self.assertEqual(ad.tick(self.SDK_TIME + 12), [])
        self.write(path, b"\n", "ab")
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 13)], [K_RESULT])
        replacement = self.path("replacement.log")
        self.write(replacement, self.sdk_start("request-2", offset=20) + self.sdk_stop(offset=20))
        os.replace(replacement, path)
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 32)], [K_STATUS, K_RESULT])

    def test_sdk_sources_caches_and_idle_polling_are_bounded(self):
        ad, path = self.sdk_adapter()
        for i in range(ad.SDK_SOURCES + 3):
            self.write(os.path.join(os.path.dirname(path), "session-%d.log" % i), b"{}\n")
        ad._pick_sdk(now=self.SDK_TIME + 1)
        self.assertEqual(len(ad._sdk_readers), ad.SDK_SOURCES)
        ad.tick(self.SDK_TIME + 1)
        start = time.process_time()
        with patch("adapters.workbuddy.os.scandir", side_effect=AssertionError("idle scanned")), \
                patch("adapters.base.io.open", side_effect=AssertionError("idle opened")):
            for _ in range(500):
                self.assertEqual(ad.tick(self.SDK_TIME + 2), [])
        print("\nSynthetic WorkBuddy: 500 idle polls, 0 events, CPU %.4fs" % (time.process_time() - start))
        for i in range(ad.SDK_CLOSED + ad.SDK_REQUESTS):
            data = self.sdk_start(str(i), sid=str(i)) + self.sdk_stop(sid=str(i))
            for line in data.decode().splitlines():
                ad._sdk_event(ad._sdk_record(line), self.SDK_TIME)
        self.assertLessEqual(len(ad._requests), ad.SDK_REQUESTS)
        self.assertLessEqual(len(ad._closed_requests), ad.SDK_CLOSED)

    def test_sdk_setup_with_only_logs_and_empty_log_directory(self):
        home = self.path("home")
        logs = os.path.join(home, ".workbuddy", "logs")
        os.makedirs(logs)
        ad = WorkBuddyAdapter({})
        with patch("adapters.workbuddy.os.path.expanduser", return_value=home), \
                patch("adapters.workbuddy.time.time", return_value=self.SDK_TIME):
            self.assertTrue(ad.setup())
        folder = os.path.join(logs, "2026-10-28", "sdk", "conversations")
        os.makedirs(folder)
        self.write(os.path.join(folder, "conversation.log"), self.sdk_start() + self.sdk_stop())
        self.assertEqual([s.kind for s in ad.tick(self.SDK_TIME + 12)], [K_STATUS, K_RESULT])

    def test_deleted_source_switches_trace_and_chat_and_closes_old(self):
        ad = WorkBuddyAdapter({"roots": [], "exes": []})
        ad.traces_dir = self.path("traces")
        ad.projects_dir = self.path("projects")
        os.makedirs(ad.traces_dir)
        os.makedirs(ad.projects_dir)
        span = json.dumps({"type": "span_start", "span": {
            "spanId": "first", "toolName": "Bash", "type": "function"}}).encode() + b"\n"
        message = json.dumps({"type": "message", "role": "user", "content": "hello"}).encode() + b"\n"
        for folder, oldname, newname, attr, picker, data in (
                (ad.traces_dir, "trace_old.jsonl", "trace_new.jsonl", "trace", ad._pick_trace, span),
                (ad.projects_dir, "old.jsonl", "new.jsonl", "chat", ad._pick_chat, message)):
            oldpath = os.path.join(folder, oldname)
            newpath = os.path.join(folder, newname)
            self.write(oldpath, data)
            picker(first=True)
            old = getattr(ad, attr)
            self.assertEqual(old.read_lines(), [])
            close = getattr(old, "close", lambda: None)
            old.close = Mock(side_effect=close)
            os.unlink(oldpath)
            self.write(newpath, data)
            picker()
            self.assertEqual(getattr(ad, attr).path, newpath)
            old.close.assert_called_once()
        ad._switch_at = time.time()
        self.assertEqual({s.kind for s in ad.tick(time.time())}, {K_TOOL, K_USER})
        self.assertEqual(ad.tick(time.time()), [])

    def test_missing_candidates_do_not_abort_selection(self):
        good = self.path("good.jsonl")
        self.write(good, b"{}\n")
        self.assertEqual(WorkBuddyAdapter._newest_path([self.path("deleted"), good]), good)

    def test_new_source_first_batch_and_stable_selection(self):
        ad = WorkBuddyAdapter({"roots": [], "exes": []})
        ad.traces_dir = self.root
        old = self.path("trace_a.jsonl")
        new = self.path("trace_b.jsonl")
        self.write(old, b"old\n")
        os.utime(old, (100, 100))
        ad._pick_trace(first=True)
        self.write(new, b"first\n")
        os.utime(new, (101, 101))
        ad._pick_trace()
        reader = ad.trace
        self.write(new, b"second\n", "ab")
        self.assertEqual(reader.read_lines(), ["first", "second"])
        ad._pick_trace()
        self.assertIs(ad.trace, reader)
        self.assertEqual(reader.read_lines(), [])


class QianfanTests(Samples):
    def setUp(self):
        super().setUp()
        self.ad = BaiduDaziAdapter({"roots": [], "exes": []})

    def event(self, typ, properties, now=1800000000):
        return self.ad._line("event: " + json.dumps({"type": typ, "properties": properties}), now)

    def test_busy_after_done_and_export_is_not_turn_completion(self):
        self.event("session.turn.completed", {"sessionID": "s"})
        self.assertEqual(self.ad.phase, P_DONE)
        steps = self.event("session.status", {"sessionID": "s", "status": {"type": "busy"}})
        self.assertEqual(steps[0].meta["phase"], P_THINKING)
        self.assertEqual(self.ad.phase, P_THINKING)
        part = {"type": "file-export", "sessionID": "s", "id": "f",
                "files": [{"path": "report.pdf"}]}
        self.assertEqual([s.kind for s in self.ad._part(part, 1800000000)], [K_EDIT])
        self.assertNotEqual(self.ad.phase, P_DONE)
        self.assertEqual(self.ad._part(part, 1800000001), [])
        steps = self.event("session.status", {"sessionID": "s", "status": {"type": "idle"}})
        self.assertEqual([s.kind for s in steps], [K_RESULT])
        self.assertEqual(self.event("session.status", {"sessionID": "s", "status": {"type": "idle"}}), [])

    def test_busy_survives_part_cache_ttl(self):
        with patch("adapters.qianfan.time.monotonic", return_value=100):
            self.event("session.status", {"sessionID": "s", "status": {"type": "busy"}})
        with patch("adapters.qianfan.time.monotonic", return_value=100 + _Recent.TTL + 1):
            self.ad.tick(1800000000)
            steps = self.event("session.status", {"sessionID": "s", "status": {"type": "idle"}})
        self.assertEqual([s.kind for s in steps], [K_RESULT])

    def test_ten_thousand_parts_are_bounded_and_terminal_cleans_session(self):
        for i in range(10000):
            self.ad._part({"type": "text", "sessionID": "s", "id": str(i),
                           "text": "answer", "time": {"end": 1}}, 1800000000)
            self.ad._part({"type": "tool", "sessionID": "s", "id": str(i),
                           "tool": "read", "state": {"status": "completed"}}, 1800000000)
        self.assertLessEqual(len(self.ad._texts), _Recent.LIMIT)
        self.assertLessEqual(len(self.ad._tools), _Recent.LIMIT)
        self.event("session.turn.completed", {"sessionID": "s"})
        self.assertFalse(self.ad._texts)
        self.assertFalse(self.ad._tools)

    def test_session_scoping_deletion_and_idle_ttl(self):
        with patch("adapters.qianfan.time.monotonic", return_value=100):
            for sid in ("a", "b"):
                self.ad._part({"type": "text", "sessionID": sid, "id": "same",
                               "text": "answer", "time": {"end": 1}}, 1800000000)
                self.event("message.part.reasoning-summary", {"sessionID": sid,
                           "partID": "same", "summary": "reason"})
                self.event("file.edited", {"sessionID": sid, "file": "a.py"})
                self.event("session.created", {"info": {"id": sid, "title": "task"}})
                self.event("todo.updated", {"sessionID": sid,
                           "todos": [{"subject": "task", "status": "pending"}]})
        self.assertEqual(len(self.ad._texts), 2)
        self.event("session.deleted", {"info": {"id": "a"}})
        self.assertEqual(set(self.ad._texts), {("b", "same")})
        with patch("adapters.qianfan.time.monotonic", return_value=100 + _Recent.TTL + 1):
            self.ad.tick(1800000000)
        for cache in (self.ad._texts, self.ad._tools, self.ad._thinks,
                      self.ad._titles, self.ad._todos, self.ad._edited):
            self.assertFalse(cache)

    def test_terminal_orders_emit_once_and_ignore_all_late_activity(self):
        idle = ("session.status", {"sessionID": "s", "status": {"type": "idle"}})
        done = ("session.turn.completed", {"sessionID": "s"})
        for endings in ((idle, done), (done, idle)):
            with self.subTest(endings=endings):
                self.ad = BaiduDaziAdapter({"roots": [], "exes": []})
                self.event("session.status", {"sessionID": "s", "status": {"type": "busy"}})
                steps = []
                for typ, props in endings * 2:
                    steps.extend(self.event(typ, props))
                self.assertEqual([s.kind for s in steps], [K_RESULT])
                self.assertIs(steps[0].meta.get("terminal"), True)
                before = (self.ad.steps, self.ad.last_event, self.ad.detail)
                parts = [
                    {"type": "text", "text": "late answer", "time": {"end": 1}},
                    {"type": "text", "phase": "commentary", "text": "late thought", "time": {"end": 1}},
                    {"type": "file-export", "files": [{"path": "late.pdf"}]},
                ] + [{"type": "tool", "tool": "write", "state": {"status": status}}
                     for status in ("running", "completed", "error")]
                for i, part in enumerate(parts):
                    part.update(sessionID="s", id=str(i))
                    self.assertEqual(self.event("message.part.updated", {"part": part}), [])
                for typ, props in (
                    ("message.part.reasoning-summary", {"partID": "r", "summary": "late"}),
                    ("file.edited", {"file": "late.py"}),
                    ("todo.updated", {"todos": [{"subject": "late", "status": "pending"}]}),
                    ("session.compacted", {}),
                    ("session.created", {"info": {"id": "s", "title": "late"}}),
                    ("session.updated", {"info": {"id": "s", "title": "late"}}),
                ):
                    self.assertEqual(self.event(typ, dict(props, sessionID="s")), [])
                self.assertEqual(self.ad.phase, P_DONE)
                self.assertEqual((self.ad.steps, self.ad.last_event, self.ad.detail), before)
                self.assertEqual(self.ad._file_step("late.py", 1800000001, "s"), [])
                self.assertEqual(self.ad._todos_step({"sessionID": "s", "todos": [{}]}, 1800000001), [])
                for cache in (self.ad._texts, self.ad._tools, self.ad._thinks,
                              self.ad._todos, self.ad._edited):
                    self.assertFalse(cache)
                # A different session remains independent.
                self.assertEqual(self.ad._part({"type": "text", "sessionID": "other", "id": "p",
                    "text": "answer", "time": {"end": 1}}, 1800000001)[0].kind, K_ANSWER)

    def test_closed_survives_ttl_and_new_busy_restores_processing(self):
        with patch("adapters.qianfan.time.monotonic", return_value=100):
            self.event("session.status", {"sessionID": "s", "status": {"type": "busy"}}, 10)
            self.event("session.turn.completed", {"sessionID": "s"}, 20)
        with patch("adapters.qianfan.time.monotonic", return_value=100 + _Recent.TTL + 1):
            self.ad.tick(30)
            self.assertTrue(self.ad._closed("s"))
            self.assertEqual(self.event("session.turn.completed", {"sessionID": "s"}, 30), [])
            self.assertEqual(self.event("session.status", {"sessionID": "s", "status": {"type": "busy"}}, 10), [])
            self.assertEqual(self.ad.phase, P_DONE)
            self.assertEqual(self.event("session.status", {"sessionID": "s", "status": {"type": "busy"}}, 40)[0].kind, K_STATUS)
            self.assertFalse(self.ad._closed("s"))
            # Old terminal copies must not close the new busy round.
            self.assertEqual(self.event("session.turn.completed", {"sessionID": "s"}, 20), [])
            self.assertEqual(self.ad._part({"type": "tool", "sessionID": "s", "id": "p",
                "tool": "read", "state": {"status": "running"}}, 41)[0].kind, K_TOOL)
            self.assertEqual(self.event("file.edited", {"sessionID": "s", "file": "new.py"}, 42)[0].kind, K_EDIT)
            self.assertEqual(len(self.event("todo.updated", {"sessionID": "s", "todos": [{}]}, 43)), 1)
            self.assertEqual(self.ad._part({"type": "text", "sessionID": "s", "id": "a",
                "text": "answer", "time": {"end": 1}}, 44)[0].kind, K_ANSWER)
            self.assertEqual(self.event("session.turn.completed", {"sessionID": "s"}, 45)[0].kind, K_RESULT)

    def test_only_new_identified_user_message_reopens_closed_session(self):
        user = {"info": {"sessionID": "s", "id": "u1", "role": "user"}}
        self.assertEqual(self.event("message.updated", user, 10)[0].kind, K_USER)
        self.event("session.turn.completed", {"sessionID": "s"}, 20)
        self.assertEqual(self.event("message.updated", user, 21), [])
        for info in ({"sessionID": "s", "id": "a", "role": "assistant"},
                     {"sessionID": "s", "role": "user"}, {"id": "u2", "role": "user"}):
            self.assertEqual(self.event("message.updated", {"info": info}, 22), [])
        new = {"info": {"sessionID": "s", "id": "u2", "role": "user"}}
        self.assertEqual(self.event("message.updated", new, 15), [])
        self.assertEqual(self.ad.phase, P_DONE)
        self.assertEqual(self.event("message.updated", new, 30)[0].kind, K_USER)
        self.assertEqual(self.ad.phase, P_RECEIVED)
        self.assertEqual(self.event("message.updated", new, 31), [])
        self.assertFalse(self.ad._closed("s"))

    def test_tool_error_is_nonterminal_and_closed_cache_is_bounded(self):
        steps = self.ad._part({"type": "tool", "sessionID": "s", "id": "p",
            "tool": "read", "state": {"status": "error", "error": "failed"}}, 10)
        self.assertEqual(steps[0].kind, K_ERROR)
        self.assertIs(steps[0].meta.get("terminal"), False)
        self.assertFalse(self.ad._closed("s"))
        self.assertEqual(self.ad._part({"type": "text", "sessionID": "s", "id": "a",
            "text": "recovered", "time": {"end": 1}}, 11)[0].kind, K_ANSWER)
        for i in range(_Recent.LIMIT + 10):
            self.event("session.turn.completed", {"sessionID": str(i)})
        self.assertEqual(len(self.ad._busy), _Recent.LIMIT)
        sid = str(_Recent.LIMIT + 9)
        self.assertTrue(self.ad._closed(sid))
        self.event("session.deleted", {"sessionID": sid})
        self.assertIsNone(self.ad._busy.get(sid))

    def question(self, now=10, sid="s", rid="q", message="m", call="c"):
        return self.event("question.asked", {
            "sessionID": sid, "id": rid,
            "tool": {"messageID": message, "callID": call},
            "questions": [{"question": "private question", "options": ["private option"]}],
            "draft": {"answers": [["private draft"]]}}, now)

    def reply(self, now=11, sid="s", rid="q"):
        return self.event("question.replied", {
            "sessionID": sid, "requestID": rid, "answers": [["private answer"]]}, now)

    def question_tool(self, now=11, sid="s", message="m", call="c", status="completed"):
        return self.event("message.part.updated", {"part": {
            "sessionID": sid, "id": "p", "messageID": message, "callID": call,
            "type": "tool", "tool": "question", "state": {"status": status}}}, now)

    def test_native_question_draft_submit_and_terminal_lifecycle(self):
        self.event("session.status", {"sessionID": "s", "status": {"type": "busy"}}, 9)
        steps = self.question()
        self.assertEqual([s.kind for s in steps], [K_ASK])
        self.assertEqual(steps[0].text, self.ad.QUESTION_TEXT)
        self.assertEqual((steps[0].session, steps[0].meta["requestID"]), ("s", "q"))
        self.assertNotIn("private", repr(steps))
        self.assertEqual(self.ad.phase, "asking")
        self.question_tool(status="running")
        self.assertEqual(self.ad.phase, "asking")
        self.assertEqual(self.event("question.deadline.updated", {
            "sessionID": "s", "requestID": "q", "answers": [["private draft"]]}, 12), [])
        self.assertIn(("s", "q"), self.ad._questions)
        steps = self.reply(13)
        self.assertEqual([s.kind for s in steps], [K_STATUS])
        self.assertEqual(steps[0].meta["phase"], P_THINKING)
        self.assertEqual(self.ad.phase, P_THINKING)
        self.assertNotIn("private", repr(steps))
        self.assertFalse(self.ad._questions)
        self.assertEqual([s.kind for s in self.question_tool(14)], [K_TOOL_DONE])
        self.assertNotEqual(self.ad.phase, P_DONE)
        self.assertEqual([s.kind for s in self.event("session.turn.completed", {"sessionID": "s"}, 15)],
                         [K_RESULT])
        self.assertEqual(self.event("session.status", {"sessionID": "s", "status": {"type": "idle"}}, 16), [])
        self.assertEqual(self.ad.tick(30), [])

    def test_question_wrong_ids_and_sessions_do_not_resolve(self):
        self.question()
        for sid, rid in (("other", "q"), ("s", "other"), ("", "q"), ("s", "")):
            self.assertEqual(self.reply(11, sid=sid, rid=rid), [])
        self.assertEqual(self.event("question.replied", {"sessionID": "s", "id": "q"}, 11), [])
        self.assertEqual(list(self.ad._questions), [("s", "q")])
        self.question(12, sid="another")
        self.assertEqual([s.kind for s in self.reply(13)], [K_STATUS])
        self.assertEqual(list(self.ad._questions), [("another", "q")])

    def test_question_event_timestamp_and_refresh_clock_are_separate(self):
        with patch.object(self.ad, "_line_ts", return_value=10):
            steps = self.question(100)
        self.assertEqual(steps[0].ts, 10)
        self.assertEqual(self.ad.tick(104.9), [])
        self.assertEqual([s.ts for s in self.ad.tick(105)], [105])

    def test_question_resolution_in_tick_prevents_due_refresh(self):
        for typ, props, kind in (
                ("question.replied", {"sessionID": "s", "requestID": "q"}, K_STATUS),
                ("session.turn.completed", {"sessionID": "s"}, K_RESULT)):
            with self.subTest(typ=typ):
                self.ad = BaiduDaziAdapter({})
                self.question()
                self.ad.tail = Mock()
                self.ad.tail.read_lines.return_value = ["event: " + json.dumps({
                    "type": typ, "properties": props})]
                self.assertEqual([s.kind for s in self.ad.tick(15)], [kind])
                self.assertFalse(self.ad._questions)

    def test_question_duplicate_refresh_and_no_pending_ttl(self):
        with patch("adapters.qianfan.time.monotonic", return_value=100):
            self.question()
            self.assertEqual(self.question(12, message="wrong", call="wrong"), [])
            self.assertEqual(self.ad.tick(14.9), [])
            self.assertEqual([s.kind for s in self.ad.tick(15)], [K_ASK])
            self.assertEqual(self.question(16), [])
            self.assertEqual(self.ad.tick(19.9), [])
        with patch("adapters.qianfan.time.monotonic", return_value=100 + _Recent.TTL + 1):
            self.assertEqual([s.kind for s in self.ad.tick(1000)], [K_ASK])
            self.assertEqual(self.ad._questions[("s", "q")]["ts"], 10)
            self.assertEqual([s.kind for s in self.question_tool(1001)], [K_STATUS])
        self.assertEqual(self.ad.tick(1010), [])

    def test_question_refresh_at_tick_end_and_activity_keeps_waiting_phase(self):
        self.question()
        self.ad.tail = Mock()
        self.ad.tail.read_lines.return_value = ["event: " + json.dumps({
            "type": "message.part.updated", "properties": {"part": {
                "sessionID": "s", "id": "activity", "type": "tool", "tool": "read",
                "state": {"status": "running"}}}})]
        steps = self.ad.tick(15)
        self.assertEqual([s.kind for s in steps], [K_TOOL, K_ASK])
        self.assertTrue(all(s.meta["phase"] == "asking" for s in steps))
        for typ, props in (
                ("message.part.reasoning-summary", {"partID": "r", "summary": "thinking"}),
                ("todo.updated", {"todos": [{"subject": "task", "status": "pending"}]}),
                ("file.edited", {"file": "file.py"}),
                ("session.compacted", {}),
                ("session.status", {"status": {"type": "busy"}})):
            steps = self.event(typ, dict(props, sessionID="s"), 16)
            self.assertTrue(all(s.meta["phase"] == "asking" for s in steps))
            self.assertEqual(self.ad.phase, "asking")
        self.ad.tail.read_lines.side_effect = OSError("temporarily unavailable")
        self.assertEqual([s.kind for s in self.ad.tick(20)], [K_ASK])

    def test_question_tool_fallback_requires_exact_native_binding(self):
        for status in ("completed", "error"):
            with self.subTest(status=status):
                self.ad = BaiduDaziAdapter({})
                self.question()
                self.question(10, rid="q2", call="c2")
                for sid, message, call in (("other", "m", "c"), ("s", "wrong", "c"),
                                           ("s", "m", "wrong"), ("s", "", "c")):
                    self.question_tool(sid=sid, message=message, call=call, status=status)
                    self.assertEqual(len(self.ad._questions), 2)
                steps = self.question_tool(12, status=status)
                self.assertEqual([s.kind for s in steps], [K_STATUS])
                self.assertEqual(list(self.ad._questions), [("s", "q2")])
                self.assertEqual(self.ad.phase, "asking")
                self.assertFalse(self.ad._closed("s"))
                steps = self.question_tool(13, call="c2", status=status)
                self.assertEqual([s.kind for s in steps], [K_STATUS])
                self.assertEqual(self.ad.phase, P_THINKING)
                self.assertFalse(self.ad._questions)
                self.assertEqual(self.question(14), [])

    def test_generic_question_tool_does_not_create_wait(self):
        for status in ("running", "completed", "error"):
            steps = self.question_tool(status=status)
            self.assertNotIn(K_ASK, [s.kind for s in steps])
            self.assertFalse(self.ad._questions)
        self.assertEqual(self.ad.tick(20), [])
        self.question(30, message="", call="")
        self.question_tool(31)
        self.assertTrue(self.ad._questions)
        self.assertEqual([s.kind for s in self.reply(32)], [K_STATUS])

    def test_question_older_events_and_resolved_tombstones(self):
        self.event("message.updated", {"info": {"sessionID": "s", "id": "u", "role": "user"}}, 10)
        self.assertEqual(self.question(9), [])
        self.question(12)
        self.assertEqual(self.reply(11), [])
        self.question_tool(11)
        self.assertTrue(self.ad._questions)
        self.assertEqual([s.kind for s in self.reply(13)], [K_STATUS])
        self.assertEqual(self.question(14), [])
        self.assertEqual(self.reply(15), [])
        self.assertEqual(self.reply(20, rid="arrived-first"), [])
        self.assertEqual(self.question(19, rid="arrived-first"), [])
        with patch("adapters.qianfan.time.monotonic", return_value=time.monotonic() + _Recent.TTL + 1):
            self.ad.tick(1000)
            self.assertEqual(self.question(1000), [])
        self.assertEqual(self.ad.phase, P_THINKING)

    def test_question_terminal_and_deletion_clear_only_matching_session(self):
        for typ, props in (("session.turn.completed", {}),
                           ("session.status", {"status": {"type": "idle"}}),
                           ("session.deleted", {})):
            with self.subTest(typ=typ):
                self.ad = BaiduDaziAdapter({})
                self.question()
                self.question(sid="other")
                self.event(typ, dict(props, sessionID="s"), 12)
                self.assertEqual(list(self.ad._questions), [("other", "q")])
                self.assertEqual(self.question(13), [])
                self.assertEqual(self.reply(14), [])
                self.assertEqual([s.session for s in self.ad.tick(20)], ["other"])

    def test_question_new_genuine_user_turn_clears_wait_not_archive_updates(self):
        user = {"info": {"sessionID": "s", "id": "u1", "role": "user"}}
        self.event("message.updated", user, 9)
        self.question()
        for props in (user, {"info": {"sessionID": "s", "id": "a", "role": "assistant"}},
                      {"info": {"sessionID": "s", "role": "user"}}):
            self.assertEqual(self.event("message.updated", props, 11), [])
            self.assertTrue(self.ad._questions)
        new = {"info": {"sessionID": "s", "id": "u2", "role": "user"}}
        self.assertEqual(self.event("message.updated", new, 8), [])
        self.assertEqual([s.kind for s in self.event("message.updated", new, 20)], [K_USER])
        self.assertFalse(self.ad._questions)
        self.assertEqual(self.question(21), [])
        self.assertEqual(self.question(19, rid="old"), [])
        self.assertEqual(self.reply(22), [])
        self.assertEqual(self.ad.phase, P_RECEIVED)
        self.assertEqual([s.kind for s in self.question(23, rid="new")], [K_ASK])

    def test_question_pending_and_tombstone_bounds(self):
        for n in range(self.ad.QUESTION_LIMIT + _Recent.LIMIT + 10):
            self.question(n + 10, sid="s" + str(n), rid="q" + str(n))
        self.assertEqual(len(self.ad._questions), self.ad.QUESTION_LIMIT)
        self.assertEqual(len(self.ad._resolved_questions), _Recent.LIMIT)
        self.assertEqual(self.question(1000, sid="s20", rid="q20"), [])
        for sid, rid in list(self.ad._questions):
            self.reply(2000, sid=sid, rid=rid)
        self.assertFalse(self.ad._questions)
        self.assertEqual(len(self.ad._resolved_questions), _Recent.LIMIT)
        self.assertEqual(self.ad.tick(3000), [])


class FileAndWindowTests(Samples):
    def test_raw_tail_rotation_truncation_bounded_reads_and_idle(self):
        path = self.path("data.log")
        self.write(path, b"old\n")
        rd = RawTail(path)
        self.assertEqual(rd.read(), b"")
        replacement = self.path("new.log")
        self.write(replacement, b"new\n")
        os.replace(replacement, path)
        self.assertEqual(rd.read(), b"new\n")
        self.write(path, b"changed-and-longer\n")
        self.assertEqual(rd.read(), b"changed-and-longer\n")
        self.write(path, b"x\n")
        self.assertEqual(rd.read(), b"x\n")
        self.write(path, b"0123456789", "ab")
        self.assertEqual(rd.read(4), b"0123")
        self.assertEqual(rd.read(4), b"4567")
        self.assertEqual(rd.read(4), b"89")
        with patch("builtins.open", side_effect=AssertionError("idle opened file")):
            for _ in range(100):
                self.assertEqual(rd.read(), b"")
        os.unlink(path)
        self.assertEqual(rd.read(), b"")
        self.write(path, b"restored\n")
        self.assertEqual(rd.read(), b"restored\n")

    def test_shell_tail_uses_shared_reader_rotation_contract(self):
        ad = DoubaoAdapter({"roots": [], "exes": []})
        ad._discover_at = ad._raw_at = float("inf")
        ad.USE_WINDOWS = False
        path = self.path("chat.jsonl")
        old = b'{"role":"user","content":"old text"}\n'
        new = b'{"role":"user","content":"new text"}\n'
        self.write(path, old)
        reader = TailReader(path)
        self.resources.append(reader)
        ad.tails = [reader]
        self.assertEqual(ad.tick(100), [])
        replacement = self.path("replace.jsonl")
        self.write(replacement, new)
        os.replace(replacement, path)
        steps = ad.tick(101)
        self.assertTrue(any("new text" in s.text for s in steps))
        self.write(path, b'{"content":"short text"}\n')
        self.assertTrue(any("short text" in s.text for s in ad.tick(102)))
        self.assertEqual(ad.tick(103), [])

    def test_each_window_is_deduplicated_independently(self):
        ad = DoubaoAdapter({"roots": [], "exes": []})
        ad._discover_at = ad._raw_at = float("inf")
        ad._win_titles = Mock(return_value=[(1, "first"), (2, "second")])
        self.assertEqual(len(ad.tick(10)), 2)
        for now in (20, 30, 40):
            self.assertEqual(ad.tick(now), [])
        ad._win_titles.return_value = [(2, "second"), (1, "changed")]
        steps = ad.tick(50)
        self.assertEqual([s.text for s in steps], ["窗口: changed"])
        ad._win_titles.return_value = [(1, "changed")]
        self.assertEqual(ad.tick(60), [])
        ad._win_titles.return_value = [(1, "changed"), (3, "second")]
        self.assertEqual(len(ad.tick(70)), 1)
        self.assertEqual(set(ad._window_titles), {1, 3})

    def test_raw_discovery_collects_first_batch_only_for_new_files(self):
        directory = self.path("Local Storage/leveldb")
        os.makedirs(directory)
        old = os.path.join(directory, "0001.log")
        self.write(old, b"old")
        ad = DoubaoAdapter({"roots": [self.root], "exes": []})
        ad._discover_raws(first=True)
        self.assertEqual(ad.raws[0].read(), b"")
        new = os.path.join(directory, "0002.log")
        self.write(new, b"first")
        ad._discover_raws()
        reads = {os.path.basename(rd.path): rd.read() for rd in ad.raws}
        self.assertEqual(reads, {"0001.log": b"", "0002.log": b"first"})
        for _ in range(5):
            ad._discover_raws()
        self.assertLessEqual(len(ad.raws), 2)
        self.assertTrue(all(rd.read() == b"" for rd in ad.raws))

    def test_doubao_unverified_text_cannot_infer_tool_or_done(self):
        ad = DoubaoAdapter({"roots": [], "exes": []})
        for kind in (K_TOOL, K_RESULT, K_ANSWER):
            step = ad.emit(kind, "stored text", 100)
            ad._note(kind, 100)
            self.assertEqual(step.kind, K_STATUS)
            self.assertEqual(step.meta["phase"], P_IDLE)
            self.assertEqual(ad.phase, P_IDLE)
            self.assertEqual(step.meta["evidence"], "unverified_storage")


class CodexTests(Samples):
    SCHEMA = "create table logs (id integer primary key,ts,level,target,feedback_log_body)"

    def reader(self):
        path, con = self.db("logs_2.sqlite", self.SCHEMA)
        self.insert(con, 100, "tool/seed")
        rd = CodexLogReader(path)
        self.resources.append(rd)
        self.assertTrue(rd.open())
        return rd, con

    @staticmethod
    def insert(con, rid, name):
        con.execute("insert into logs values (?,?,?,?,?)",
                    (rid, 1800000000, "INFO", "codex_core::tools", 'otel.name="%s"' % name))
        con.commit()

    def test_id_rollback_empty_then_refill_and_equal_id_rebuild(self):
        rd, con = self.reader()
        self.assertEqual(rd.poll(), [])
        con.execute("delete from logs")
        self.insert(con, 1, "tool/new")
        self.assertEqual(rd.poll()[0][1], "tool/new")
        self.assertEqual(rd.poll(), [])
        con.execute("delete from logs")
        self.insert(con, 1, "tool/equal-id")
        self.assertEqual(rd.poll()[0][1], "tool/equal-id")
        con.execute("delete from logs")
        con.commit()
        self.assertEqual(rd.poll(), [])
        self.insert(con, 1, "tool/refill")
        self.assertEqual(rd.poll()[0][1], "tool/refill")
        self.assertEqual(rd.poll(), [])

    def test_generation_replacement_reopens_readonly(self):
        rd, con = self.reader()
        rd.close()
        con.close()
        path, replacement = self.db("replace.sqlite", self.SCHEMA)
        self.insert(replacement, 1, "tool/replaced")
        replacement.close()
        os.replace(path, rd.path)
        self.assertEqual(rd.poll()[0][1], "tool/replaced")
        with self.assertRaises(sqlite3.OperationalError):
            rd.con.execute("delete from logs")
        self.assertEqual(rd.poll(), [])

    def test_missing_schema_closes_and_recovers(self):
        path, con = self.db("empty.sqlite", "create table other (id)")
        rd = CodexLogReader(path)
        self.resources.append(rd)
        self.assertFalse(rd.open())
        self.assertIsNone(rd.con)
        con.execute(self.SCHEMA)
        self.insert(con, 1, "tool/recovered")
        self.assertEqual(rd.poll()[0][1], "tool/recovered")

    def test_transient_close_resumes_without_history_replay(self):
        rd, con = self.reader()
        rd.close()
        self.insert(con, 101, "tool/after-reconnect")
        self.assertEqual([row[1] for row in rd.poll()], ["tool/after-reconnect"])

    def test_codex_cache_ttl_capacity_and_explicit_provenance(self):
        ad = ChatGPTAdapter({"roots": [], "exes": []})
        ad._discover_at = ad._raw_at = float("inf")
        ad.USE_WINDOWS = False
        ad.codex = Mock()
        ad.codex.poll.return_value = [(K_TOOL, "tool/%d" % i, 100, {}) for i in range(10000)]
        with patch("adapters.chat_apps.time.monotonic", return_value=100):
            steps = ad.tick(100)
        self.assertLessEqual(len(ad._codex_last), 512)
        self.assertEqual(steps[0].kind, K_TOOL)
        self.assertEqual(steps[0].meta["product"], "Codex")
        ad.codex.poll.return_value = []
        with patch("adapters.chat_apps.time.monotonic", return_value=121):
            ad.tick(121)
        self.assertFalse(ad._codex_last)

    def test_chatgpt_filters_codex_roots_and_processes(self):
        codex_root = self.path(".codex")
        chat_root = self.path("ChatGPT")
        info = {"roots": [codex_root, chat_root], "exes": ["ChatGPT.exe", "Codex.exe"]}
        ad = ChatGPTAdapter(info)
        self.assertEqual(ad.roots, [chat_root])
        self.assertEqual(ad.info["exes"], ["ChatGPT.exe"])
        self.assertEqual(info["exes"], ["ChatGPT.exe", "Codex.exe"])
        explicit = ChatGPTAdapter(dict(info, codex_logs=True))
        self.assertEqual(explicit._codex_roots, [codex_root, chat_root])
        self.assertIn("Codex.exe", explicit.info["exes"])

    def test_ordinary_chatgpt_does_not_enable_codex_implicitly(self):
        ad = ChatGPTAdapter({"roots": [self.root], "exes": []})
        with patch.object(ChromiumChatAdapter, "setup", return_value=True), \
                patch.object(ad, "_setup_codex") as setup_codex:
            ad.setup()
            setup_codex.assert_not_called()
        self.assertIsNone(ad.codex)
        self.assertNotEqual(ad.tier, "A")


if __name__ == "__main__":
    unittest.main(verbosity=2)
