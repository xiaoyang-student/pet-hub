# -*- coding: utf-8 -*-
import gc
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(os.environ.get("AGENT_TRACE_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from adapters.base import ReadOnlyDB, TailReader
from adapters.generic import SchemaProbe, TablePoller
from adapters.structured import StructuredAdapter


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-trace-readers-")
        self.addCleanup(self.cleanup_temp)
        self.root = Path(self.temp.name)

    def cleanup_temp(self):
        gc.collect()
        self.temp.cleanup()

    def database(self, schema, name="messages.sqlite"):
        path = self.root / name
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.executescript(schema)
        writer.commit()
        db = ReadOnlyDB(str(path))
        self.addCleanup(db.close)
        self.assertTrue(db.open())
        return writer, db

    def put(self, writer, sql, args=()):
        writer.execute(sql, args)
        writer.commit()

    def texts(self, poller, limit=60):
        return [r[1] for r in poller.poll(limit)]


class TailTests(TempCase):
    def reader(self, initial=b"", **kwargs):
        self.path = self.root / "messages.jsonl"
        self.path.write_bytes(initial)
        return TailReader(str(self.path), **kwargs)

    def append(self, data):
        with self.path.open("ab") as stream:
            stream.write(data)

    def test_startup_skips_history(self):
        reader = self.reader(b"old message\n")
        self.assertEqual(reader.read_lines(), [])
        self.append(b"new message\n")
        self.assertEqual(reader.read_lines(), ["new message"])
        self.assertEqual(reader.read_lines(), [])

    def test_windows_ctime_disagreement_does_not_reopen_idle_file(self):
        from types import SimpleNamespace
        reader = self.reader(b"old message\n")
        st = self.path.stat()
        fields = {key: getattr(st, key) for key in
                  ("st_dev", "st_ino", "st_size", "st_mtime_ns")}
        fields["st_birthtime_ns"] = 123
        by_path = SimpleNamespace(**fields, st_ctime_ns=123)
        by_handle = SimpleNamespace(**fields, st_ctime_ns=456)
        with mock.patch("adapters.base.os.name", "nt"):
            reader._sig = reader._stat_sig(by_handle)
            with mock.patch.object(reader, "_signature", return_value=reader._stat_sig(by_path)), \
                    mock.patch("adapters.base.io.open", side_effect=AssertionError("idle opened")):
                for _ in range(100):
                    self.assertEqual(reader.read_lines(), [])

    def test_partial_line_waits_for_newline(self):
        reader = self.reader()
        self.append("新消息".encode("utf-8")[:4])
        self.assertEqual(reader.read_lines(), [])
        self.append("新消息".encode("utf-8")[4:])
        self.assertEqual(reader.read_lines(), [])
        self.append(b"\n")
        self.assertEqual(reader.read_lines(), ["新消息"])

    def test_startup_partial_suffix_is_not_a_message(self):
        reader = self.reader(b"old partial")
        self.append(b" suffix\nnew message\n")
        self.assertEqual(reader.read_lines(), ["new message"])

    def test_truncate_reads_new_generation_from_start(self):
        reader = self.reader(b"a long historical message\n")
        self.path.write_bytes(b"fresh\n")
        self.assertEqual(reader.read_lines(), ["fresh"])

    def test_equal_size_replacement(self):
        reader = self.reader(b"old entry\n")
        replacement = self.root / "next.jsonl"
        replacement.write_bytes(b"new entry\n")
        os.replace(replacement, self.path)
        self.assertEqual(reader.read_lines(), ["new entry"])
        self.assertEqual(reader.read_lines(), [])

    def test_copytruncate_regrows_between_polls(self):
        reader = self.reader(b"old entry\n")
        self.path.write_bytes(b"new entry\nanother message\n")
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
        self.assertEqual(reader.read_lines(), ["new entry", "another message"])

    def test_missing_then_created(self):
        self.path = self.root / "messages.jsonl"
        reader = TailReader(str(self.path))
        self.assertEqual(reader.read_lines(), [])
        self.path.write_bytes(b"first message\n")
        self.assertEqual(reader.read_lines(), ["first message"])

    def test_removed_partial_does_not_leak_into_replacement(self):
        reader = self.reader()
        self.append(b"stale partial")
        self.assertEqual(reader.read_lines(), [])
        self.path.unlink()
        self.assertEqual(reader.read_lines(), [])
        self.path.write_bytes(b"fresh\n")
        self.assertEqual(reader.read_lines(), ["fresh"])

    def test_touch_does_not_replay(self):
        reader = self.reader(b"old message\n")
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000000))
        self.assertEqual(reader.read_lines(), [])

    def test_offset_counts_actual_short_read(self):
        reader = self.reader()
        self.append(b"abc\ndef\n")
        real_open = io.open

        class ShortRead:
            def __enter__(inner):
                inner.stream = real_open(self.path, "rb")
                return inner

            def __exit__(inner, *args):
                inner.stream.close()

            def fileno(inner):
                return inner.stream.fileno()

            def seek(inner, *args):
                return inner.stream.seek(*args)

            def read(inner, n):
                return inner.stream.read(min(n, 2))

        with mock.patch("adapters.base.io.open", return_value=ShortRead()):
            self.assertEqual(reader.read_lines(), [])
        self.assertEqual(reader.off, 2)
        self.assertEqual(reader.read_lines(), ["abc", "def"])

    def test_oversized_unterminated_line_is_bounded_and_recovers(self):
        reader = self.reader()
        self.append(b"x" * (3 << 20))
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        begin = time.perf_counter()
        for _ in range(4):
            self.assertEqual(reader.read_lines(), [])
            self.assertLessEqual(len(reader.buf), 256 << 10)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 5 << 20)
        self.append(b"\nvalid message\n")
        self.assertEqual(reader.read_lines(), ["valid message"])
        print("TAIL 3MiB: peak=%d bytes elapsed=%.4fs" % (peak, time.perf_counter() - begin))

    def test_long_complete_line_is_dropped_not_truncated(self):
        reader = self.reader(max_step=16, max_line=8)
        self.append(b"x" * 40 + b"\ngood\n")
        lines = []
        for _ in range(5):
            lines.extend(reader.read_lines())
        self.assertEqual(lines, ["good"])

    def test_output_pages_are_bounded_without_loss(self):
        reader = self.reader(max_lines=7)
        expected = ["message %d" % n for n in range(1000)]
        self.append(("\n".join(expected) + "\n").encode())
        got = []
        for _ in range(150):
            page = reader.read_lines()
            self.assertLessEqual(len(page), 7)
            got.extend(page)
        self.assertEqual(got, expected)

    def test_new_source_and_rewind_start_on_complete_line(self):
        reader = self.reader(b"old message\nnew message\n", start_at_end=False)
        self.assertEqual(reader.read_lines(), ["old message", "new message"])
        reader.rewind(15)
        self.assertEqual(reader.read_lines(), ["new message"])


class SQLiteTests(TempCase):
    def test_session_local_sequence_does_not_block_new_session(self):
        writer, db = self.database("create table messages(session text, seq integer, content text);")
        self.put(writer, "insert into messages values('old',100,'historical message')")
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "insert into messages values('new',1,'new session message')")
        self.assertEqual(self.texts(poller), ["new session message"])
        self.assertEqual(self.texts(poller), [])

    def test_same_value_61_rows_are_not_lost_at_page_boundary(self):
        writer, db = self.database("create table messages(created_at integer, content text);")
        poller = TablePoller(db, "messages")
        poller.seed()
        writer.executemany("insert into messages values(1000,?)", [("message %d" % n,) for n in range(61)])
        writer.commit()
        self.assertEqual(len(self.texts(poller)), 60)
        self.assertEqual(self.texts(poller), ["message 60"])
        self.assertEqual(self.texts(poller), [])

    def test_uuid_ids_do_not_require_integer_conversion(self):
        writer, db = self.database("create table messages(id text primary key, content text);")
        self.put(writer, "insert into messages values('ffffffff-0000','historical message')")
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "insert into messages values('00000000-1111','new UUID message')")
        self.assertEqual(self.texts(poller), ["new UUID message"])

    def test_rowid_without_any_sequence_column(self):
        writer, db = self.database("create table messages(content text);")
        poller = TablePoller(db, "messages")
        self.assertTrue(poller.ok)
        poller.seed()
        self.put(writer, "insert into messages values('rowid message')")
        self.assertEqual(self.texts(poller), ["rowid message"])

    def test_indexed_update_reaches_older_than_recent_window(self):
        writer, db = self.database("create table messages(id integer primary key, updated_at integer, content text);"
                                   "create index message_update on messages(updated_at);")
        writer.executemany("insert into messages values(?,1,?)", [(n, "old message %d" % n) for n in range(1, 301)])
        writer.commit()
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "update messages set updated_at=2, content='updated old message' where id=1")
        self.assertEqual(self.texts(poller), ["updated old message"])
        self.assertEqual(self.texts(poller), [])

    def test_updates_with_same_timestamp_paginate_and_stop(self):
        writer, db = self.database("create table messages(id integer primary key, updated_at integer, content text);"
                                   "create index message_update on messages(updated_at);")
        writer.executemany("insert into messages values(?,1,?)", [(n, "old message %d" % n) for n in range(1, 301)])
        writer.commit()
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "update messages set updated_at=2, content='changed message '||id where id<=61")
        self.assertEqual(len(self.texts(poller)), 60)
        self.assertEqual(self.texts(poller), ["changed message 61"])
        for _ in range(5):
            self.assertEqual(self.texts(poller), [])

    def test_recent_in_place_update_without_timestamp_index(self):
        writer, db = self.database("create table messages(id integer primary key, updated_at integer, content text);")
        self.put(writer, "insert into messages values(1,10,'old message')")
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "update messages set content='streaming message' where id=1")
        self.assertEqual(self.texts(poller), ["streaming message"])
        self.assertEqual(self.texts(poller), [])

    def test_new_row_with_older_timestamp_is_still_read(self):
        writer, db = self.database("create table messages(id text, updated_at integer, content text);"
                                   "create index message_update on messages(updated_at);")
        self.put(writer, "insert into messages values('old',100,'old message')")
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "insert into messages values('new',1,'late message')")
        self.assertEqual(self.texts(poller), ["late message"])

    def test_rowid_rollback_resets_both_streams_and_recent_cache(self):
        for indexed in (False, True):
            with self.subTest(indexed=indexed):
                writer, db = self.database(
                    "create table messages(id integer primary key, updated_at integer, content text);" +
                    ("create index message_update on messages(updated_at);" if indexed else ""),
                    "rollback-%s.sqlite" % indexed)
                self.put(writer, "insert into messages values(1000,1000,'historical message')")
                poller = TablePoller(db, "messages")
                poller.seed()
                writer.execute("delete from messages")
                self.put(writer, "insert into messages values(1,1,'rebuilt message')")
                self.assertEqual(self.texts(poller), ["rebuilt message"])
                self.assertEqual(poller.last, (1,))
                self.assertNotIn((1000,), poller._recent)
                self.assertEqual(self.texts(poller), [])
                self.put(writer, "update messages set updated_at=2,content='rebuilt update' where id=1")
                self.assertEqual(self.texts(poller), ["rebuilt update"])
                self.put(writer, "delete from messages")
                self.assertEqual(self.texts(poller), [])
                self.assertEqual(poller.last, ())
                self.assertFalse(poller._recent)
                self.put(writer, "insert into messages values(1,1,'rebuilt message')")
                self.assertEqual(self.texts(poller), ["rebuilt message"])
                self.assertEqual(self.texts(poller), [])

    def test_rollback_read_failure_preserves_state_until_retry(self):
        writer, db = self.database("create table messages(id integer primary key, content text);")
        self.put(writer, "insert into messages values(1000,'historical message')")
        poller = TablePoller(db, "messages")
        poller.seed()
        recent = poller._recent.copy()
        writer.execute("delete from messages")
        self.put(writer, "insert into messages values(1,'rebuilt message')")
        original = poller._page
        with mock.patch.object(poller, "_page", return_value=None):
            self.assertEqual(self.texts(poller), [])
        self.assertEqual(poller.last, (1000,))
        self.assertEqual(poller._recent, recent)
        with mock.patch.object(poller, "_page", wraps=original):
            self.assertEqual(self.texts(poller), ["rebuilt message"])
        self.assertFalse(db.con.in_transaction)

    def test_reversed_insert_timestamps_do_not_replay_after_cache_eviction(self):
        for historical in (0, 300):
            with self.subTest(historical=historical):
                writer, db = self.database(
                    "create table messages(id integer primary key, updated_at integer, content text);"
                    "create index message_update on messages(updated_at);", "reversed-%d.sqlite" % historical)
                writer.executemany("insert into messages values(?,1,?)",
                                   [(n, "historical message %d" % n) for n in range(1, historical + 1)])
                writer.commit()
                poller = TablePoller(db, "messages")
                poller.seed()
                expected = ["new message %d" % n for n in range(1, 301)]
                writer.executemany("insert into messages values(?,?,?)",
                                   [(historical + n, 1000 - n, text) for n, text in enumerate(expected, 1)])
                if historical:
                    writer.execute("update messages set updated_at=800,content='genuine old update' where id=1")
                    expected.append("genuine old update")
                writer.commit()
                got = []
                for _ in range(15):
                    got.extend(self.texts(poller))
                    self.assertLessEqual(len(poller._recent), poller.RECENT)
                self.assertCountEqual(got, expected)
                self.assertEqual(len(got), len(set(got)))
                self.put(writer, "update messages set updated_at=1001,content='updated evicted insert' where id=?",
                         (historical + 1,))
                self.assertEqual(self.texts(poller), ["updated evicted insert"])
                self.assertEqual(self.texts(poller), [])

    def test_updates_during_append_backlog_are_not_lost_or_replayed(self):
        writer, db = self.database(
            "PRAGMA journal_mode=WAL;"
            "create table messages(id integer primary key, updated_at integer, content text);"
            "create index message_update on messages(updated_at);")
        poller = TablePoller(db, "messages")
        poller.seed()
        writer.executemany("insert into messages values(?,?,?)",
                           [(n, 1000 - n, "new message %d" % n) for n in range(1, 301)])
        writer.commit()
        got = self.texts(poller)
        self.put(writer, "update messages set updated_at=2000,content='changed during backlog '||id where id in (1,300)")
        expected = ["new message %d" % n for n in range(1, 300)]
        expected += ["changed during backlog 1", "changed during backlog 300"]
        for _ in range(20):
            got.extend(self.texts(poller))
        self.assertCountEqual(got, expected)
        self.assertEqual(self.texts(poller), [])
        self.assertFalse(db.con.in_transaction)

    def test_rebuild_during_backlog_discards_old_batch_watermarks(self):
        writer, db = self.database(
            "create table messages(id integer primary key, updated_at integer, content text);"
            "create index message_update on messages(updated_at);")
        poller = TablePoller(db, "messages")
        poller.seed()
        writer.executemany("insert into messages values(?,1000,?)",
                           [(n, "original message %d" % n) for n in range(1, 301)])
        writer.commit()
        self.assertEqual(len(self.texts(poller)), 60)
        writer.execute("delete from messages")
        expected = ["rebuilt message %d" % n for n in range(1, 101)]
        writer.executemany("insert into messages values(?,1,?)", enumerate(expected, 1))
        writer.commit()
        got = []
        for _ in range(8):
            got.extend(self.texts(poller))
        self.assertEqual(got, expected)
        self.assertIsNone(poller._cycle)
        self.assertEqual(poller.last, (100,))

    def test_poll_snapshot_defers_concurrent_wal_insert_without_loss(self):
        writer, db = self.database(
            "PRAGMA journal_mode=WAL;"
            "create table messages(id integer primary key, updated_at integer, content text);"
            "create index message_update on messages(updated_at);")
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "insert into messages values(1,1,'first message')")
        original = poller._page
        inserted = []

        def insert_during_poll(*args, **kwargs):
            rows = original(*args, **kwargs)
            if not inserted:
                self.put(writer, "insert into messages values(2,2,'concurrent message')")
                inserted.append(True)
            return rows

        with mock.patch.object(poller, "_page", side_effect=insert_during_poll):
            self.assertEqual(self.texts(poller), ["first message"])
        self.assertFalse(db.con.in_transaction)
        self.assertEqual(self.texts(poller), ["concurrent message"])
        self.assertEqual(self.texts(poller), [])

    def test_without_rowid_uses_indexed_unique_composite_cursor(self):
        writer, db = self.database("create table messages(session text, id text, updated_at integer, content text,"
                                   "primary key(session,id)) without rowid;"
                                   "create index message_update on messages(updated_at);")
        poller = TablePoller(db, "messages")
        self.assertTrue(poller.ok)
        poller.seed()
        expected = ["message %03d" % n for n in range(181)]
        writer.executemany("insert into messages values('session',?,10,?)",
                           [("id-%03d" % n, text) for n, text in enumerate(expected)])
        writer.commit()
        got = []
        for _ in range(6):
            got.extend(self.texts(poller))
        self.assertEqual(got, expected)
        self.put(writer, "update messages set updated_at=11,content='updated first message' where id='id-000'")
        self.assertEqual(self.texts(poller), ["updated first message"])

    def test_without_rowid_unindexed_random_key_is_not_claimed_reliable(self):
        _, db = self.database("create table messages(id text primary key, content text) without rowid;")
        self.assertFalse(TablePoller(db, "messages").ok)

    def test_quoted_identifiers_and_shadowed_rowid(self):
        writer, db = self.database('create table "chat messages"("rowid" text, "select" text, content text);')
        poller = TablePoller(db, "chat messages")
        self.assertTrue(poller.ok)
        poller.seed()
        self.put(writer, 'insert into "chat messages" values(\'uuid\',\'keyword\',\'quoted message\')')
        self.assertEqual(self.texts(poller), ["quoted message"])

    def test_query_failure_preserves_cursors_and_retries(self):
        writer, db = self.database("create table messages(id integer primary key, content text);")
        self.put(writer, "insert into messages values(1,'old message')")
        poller = TablePoller(db, "messages")
        poller.seed()
        last = poller.last
        self.put(writer, "insert into messages values(2,'new message')")
        with mock.patch.object(db, "q", side_effect=sqlite3.OperationalError("injected read failure")):
            self.assertEqual(self.texts(poller), [])
        self.assertEqual(poller.last, last)
        self.assertEqual(self.texts(poller), ["new message"])
        self.assertEqual(self.texts(poller), [])

    def test_page_failure_not_only_version_failure_preserves_cursor(self):
        writer, db = self.database("create table messages(id integer primary key, content text);")
        poller = TablePoller(db, "messages")
        poller.seed()
        last = poller.last
        self.put(writer, "insert into messages values(1,'new message')")
        original = db.q

        def fail(sql, args=()):
            if sql.lower().startswith("select"):
                db.last_error = sqlite3.OperationalError("injected select failure")
                return []
            return original(sql, args)

        with mock.patch.object(db, "q", side_effect=fail):
            self.assertEqual(self.texts(poller), [])
        self.assertEqual(poller.last, last)
        self.assertEqual(self.texts(poller), ["new message"])

    def test_failed_max_query_cannot_replay_history(self):
        writer, db = self.database("create table messages(id integer primary key, content text);")
        self.put(writer, "insert into messages values(100,'historical message')")
        poller = TablePoller(db, "messages")
        poller.seed()
        original = db.q
        self.put(writer, "update messages set content='changed message' where id=100")
        state = (poller.last, poller._update_last, poller._version, poller._recent.copy())

        def fail(sql, args=()):
            if "max(" in sql.lower():
                db.last_error = sqlite3.OperationalError("injected MAX failure")
                return []
            return original(sql, args)

        with mock.patch.object(db, "q", side_effect=fail):
            self.assertEqual(self.texts(poller), [])
        self.assertEqual((poller.last, poller._update_last, poller._version, poller._recent), state)
        self.assertEqual(self.texts(poller), ["changed message"])
        self.assertEqual(self.texts(poller), [])

    def test_failed_seed_does_not_become_zero_and_replay(self):
        writer, db = self.database("create table messages(id integer primary key, content text);")
        self.put(writer, "insert into messages values(100,'historical message')")
        poller = TablePoller(db, "messages")
        with mock.patch.object(db, "q", side_effect=sqlite3.OperationalError("injected seed failure")):
            poller.seed()
        self.assertIsNone(poller.last)
        self.assertEqual(self.texts(poller), [])
        self.put(writer, "insert into messages values(101,'new message')")
        self.assertEqual(self.texts(poller), ["new message"])

    def test_readonly_open_is_idempotent_and_uri_is_escaped(self):
        writer, db = self.database("create table messages(content text);", "messages # %.sqlite")
        connection = db.con
        self.assertTrue(db.open())
        self.assertIs(db.con, connection)
        self.assertEqual(db.q("PRAGMA query_only"), [(1,)])
        self.assertEqual(db.q("insert into messages values('forbidden')"), [])
        self.assertIsNotNone(db.last_error)
        self.assertEqual(writer.execute("select count(*) from messages").fetchone()[0], 0)

    def test_wal_commits_are_visible_without_copy_or_immutable(self):
        writer, db = self.database("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;"
                                   "create table messages(id integer primary key, content text);")
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "insert into messages values(1,'committed WAL message')")
        self.assertTrue(Path(db.path + "-wal").exists())
        self.assertEqual(self.texts(poller), ["committed WAL message"])
        db.close()
        self.put(writer, "insert into messages values(2,'after reconnect message')")
        self.assertEqual(self.texts(poller), ["after reconnect message"])

    def test_seed_snapshot_does_not_suppress_concurrent_wal_insert(self):
        writer, db = self.database("PRAGMA journal_mode=WAL;"
                                   "create table messages(id integer primary key, content text);"
                                   "insert into messages values(1,'historical message');")
        poller = TablePoller(db, "messages")
        original = poller._page
        inserted = []

        def insert_during_seed(*args, **kwargs):
            rows = original(*args, **kwargs)
            if not inserted:
                self.put(writer, "insert into messages values(2,'concurrent message')")
                inserted.append(True)
            return rows

        with mock.patch.object(poller, "_page", side_effect=insert_during_seed):
            poller.seed()
        self.assertFalse(db.con.in_transaction)
        self.assertEqual(self.texts(poller), ["concurrent message"])

    def test_probe_build_is_idempotent_and_new_tables_are_read(self):
        writer, db = self.database("create table messages(id integer primary key, content text);")
        probe = SchemaProbe(db)
        self.assertEqual(probe.build(), 1)
        poller = probe.pollers[0]
        self.put(writer, "insert into messages values(1,'pending message')")
        self.assertEqual(probe.build(), 1)
        self.assertIs(probe.pollers[0], poller)
        self.assertEqual([r[1] for r in probe.poll()], ["pending message"])
        writer.executescript("create table chat_events(id integer primary key, content text);"
                             "insert into chat_events values(1,'new table message');")
        self.assertEqual(probe.build(start_at_end=False), 2)
        self.assertEqual([r[1] for r in probe.poll()], ["new table message"])

    def test_rewind_uses_real_rows_not_id_arithmetic(self):
        writer, db = self.database("create table messages(id text primary key, content text);")
        writer.executemany("insert into messages values(?,?)", [("uuid%d" % n, "message %d" % n) for n in range(5)])
        writer.commit()
        poller = TablePoller(db, "messages")
        poller.seed()
        poller.rewind(2)
        self.assertEqual(self.texts(poller), ["message 3", "message 4"])
        self.assertEqual(self.texts(poller), [])

    def test_large_database_poll_uses_bounded_index_work(self):
        writer, db = self.database("create table messages(id integer primary key, updated_at integer, content text);"
                                   "create index message_update on messages(updated_at);")
        writer.executemany("insert into messages values(?,1,?)", [(n, "old message %d" % n) for n in range(1, 50001)])
        writer.commit()
        poller = TablePoller(db, "messages")
        poller.seed()
        self.put(writer, "update messages set updated_at=2,content='indexed update message' where id=1")
        calls, statements = [], []
        db.con.set_progress_handler(lambda: calls.append(1) or 0, 100)
        db.con.set_trace_callback(statements.append)
        begin = time.perf_counter()
        self.assertEqual(self.texts(poller), ["indexed update message"])
        elapsed = time.perf_counter() - begin
        db.con.set_progress_handler(None, 0)
        db.con.set_trace_callback(None)
        self.assertLess(len(calls), 200)
        for sql in statements:
            if sql.lower().startswith("select"):
                plan = " ".join(str(r[3]) for r in db.q("EXPLAIN QUERY PLAN " + sql))
                self.assertIn("SEARCH", plan.upper(), plan)
                self.assertNotIn("TEMP B-TREE", plan.upper(), plan)
        idle = []
        db.con.set_trace_callback(idle.append)
        begin = time.perf_counter()
        for _ in range(1000):
            self.assertEqual(self.texts(poller), [])
        idle_elapsed = time.perf_counter() - begin
        db.con.set_trace_callback(None)
        self.assertEqual(len(idle), 1000)
        self.assertTrue(all(sql == "PRAGMA data_version" for sql in idle))
        self.assertLessEqual(len(poller._recent), 128)
        print("SQLITE 50000 rows: vm_steps<%d poll=%.4fs idle1000=%.4fs" %
              ((len(calls) + 1) * 100, elapsed, idle_elapsed))


class StructuredTests(TempCase):
    def adapter(self, **attrs):
        adapter = StructuredAdapter({"roots": [str(self.root)]})
        for key, value in attrs.items():
            setattr(adapter, key, value)
        self.addCleanup(self.close_adapter, adapter)
        return adapter

    @staticmethod
    def close_adapter(adapter):
        for probe in adapter.probes:
            probe.db.close()
        for key, (_, obj) in getattr(adapter, "_source_cache", {}).items():
            if key[0] == "db":
                obj.db.close()

    def line(self, text):
        return (json.dumps({"type": "text", "text": text}) + "\n").encode()

    def tick(self, adapter, clock):
        with mock.patch("adapters.structured.time.monotonic", return_value=clock):
            return [s.text for s in adapter.tick(1700000000 + clock)]

    def setup_at(self, adapter, clock=100):
        with mock.patch("adapters.structured.time.monotonic", return_value=clock):
            return adapter.setup()

    def test_repeated_setup_preserves_single_probe_tail_and_pending_data(self):
        writer, _ = self.database("create table messages(id integer primary key, content text);")
        path = self.root / "chat.jsonl"
        path.write_bytes(self.line("historical message") * 10)
        adapter = self.adapter()
        self.assertTrue(self.setup_at(adapter))
        probe, tail = adapter.probes[0], adapter.tails[0]
        connection = probe.db.con
        self.put(writer, "insert into messages values(1,'new database message')")
        with path.open("ab") as stream:
            stream.write(self.line("new log message"))
        self.assertTrue(self.setup_at(adapter, 101))
        self.assertTrue(self.setup_at(adapter, 104))
        self.assertEqual(adapter.probes, [probe])
        self.assertEqual(adapter.tails, [tail])
        self.assertIs(probe.db.con, connection)
        self.assertEqual(self.tick(adapter, 105), ["new database message", "new log message"])

    def test_empty_root_remains_watched_without_fake_events(self):
        adapter = self.adapter()
        self.assertTrue(self.setup_at(adapter))
        self.assertTrue(adapter.enabled)
        self.assertEqual(self.tick(adapter, 101), [])
        (self.root / "chat.jsonl").write_bytes(self.line("first real message"))
        self.assertEqual(self.tick(adapter, 104), ["first real message"])

    def test_new_short_log_reads_content_written_before_discovery(self):
        old = self.root / "chat-old.jsonl"
        old.write_bytes(self.line("historical message") * 10)
        adapter = self.adapter()
        self.setup_at(adapter)
        new = self.root / "chat-new.jsonl"
        new.write_bytes(self.line("already written new message"))
        self.assertLess(new.stat().st_size, 256)
        self.assertEqual(self.tick(adapter, 104), ["already written new message"])
        self.assertEqual(self.tick(adapter, 108), [])

    def test_new_database_reads_rows_written_before_discovery(self):
        (self.root / "chat-old.jsonl").write_bytes(self.line("historical message") * 10)
        adapter = self.adapter()
        self.setup_at(adapter)
        self.database("create table messages(id integer primary key, content text);"
                      "insert into messages values(1,'new session database message');")
        self.assertEqual(self.tick(adapter, 104), ["new session database message"])
        self.assertEqual(self.tick(adapter, 108), [])

    def test_initially_unselected_logs_resume_observed_offsets(self):
        paths = [self.root / ("chat-%02d.jsonl" % n) for n in range(25)]
        for n, path in enumerate(paths):
            path.write_bytes(self.line("historical message %d" % n) + b'{"type":"text","text":"old partial')
            os.utime(path, (100 + n, 100 + n))
        adapter = self.adapter()
        self.setup_at(adapter)
        self.assertEqual(len(adapter.tails), 2)
        self.assertNotIn(str(paths[0]), [t.path for t in adapter.tails])
        for index, clock in ((0, 104), (1, 108), (0, 112)):
            text = "unselected log event %d" % clock
            with paths[index].open("ab") as stream:
                if clock != 112:
                    stream.write(b' suffix"}\n')
                stream.write(self.line(text))
            os.utime(paths[index], (200 + clock, 200 + clock))
            self.assertEqual(self.tick(adapter, clock), [text])
            self.assertLessEqual(len(adapter._source_cache), adapter.CACHE_SOURCES)
        self.assertEqual(self.tick(adapter, 116), [])

    def test_initially_unselected_databases_resume_both_cursors(self):
        sources = []
        for n in range(3):
            writer, db = self.database(
                "create table messages(id integer primary key, updated_at integer, content text);"
                "create index message_update on messages(updated_at);", "messages-%d.sqlite" % n)
            writer.executemany("insert into messages values(?,1000,?)",
                               [(i, "historical message %d" % i) for i in range(1, 301)])
            writer.commit()
            os.utime(db.path, (100 + n, 100 + n))
            sources.append((writer, db))
        adapter = self.adapter(CACHE_SOURCES=2)
        self.setup_at(adapter)
        self.assertEqual(len(adapter.probes), 2)
        self.assertNotIn(sources[0][1].path, [p.db.path for p in adapter.probes])
        first, db = sources[0]
        first.execute("update messages set updated_at=1001,content='unselected old row update' where id=1")
        self.put(first, "insert into messages values(301,1,'unselected database append')")
        os.utime(db.path, (200, 200))
        self.assertCountEqual(self.tick(adapter, 104),
                              ["unselected old row update", "unselected database append"])
        self.assertEqual(self.tick(adapter, 108), [])
        self.assertLessEqual(len(adapter._source_cache), adapter.CACHE_SOURCES)
        # A seeded but inactive DB must never retain an open connection.
        for key, (_, obj) in adapter._known_sources.items():
            if key[0] == "db" and obj not in adapter.probes:
                self.assertIsNone(obj.db.con)
        second, second_db = sources[1]
        self.put(second, "insert into messages values(301,1,'resumed database append')")
        os.utime(second_db.path, (300, 300))
        self.assertEqual(self.tick(adapter, 112), ["resumed database append"])

    def test_unselected_database_wal_activity_resumes_initial_cursor(self):
        sources = []
        for n in range(3):
            writer, db = self.database(
                "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;"
                "create table messages(id integer primary key, content text);"
                "insert into messages values(1,'historical WAL message');", "messages-wal-%d.sqlite" % n)
            os.utime(db.path, (100 + n, 100 + n))
            os.utime(db.path + "-wal", (100 + n, 100 + n))
            sources.append((writer, db))
        adapter = self.adapter()
        self.setup_at(adapter)
        writer, db = sources[0]
        self.assertNotIn(db.path, [p.db.path for p in adapter.probes])
        modified = os.stat(db.path).st_mtime_ns
        self.put(writer, "insert into messages values(2,'unselected WAL append')")
        self.assertEqual(os.stat(db.path).st_mtime_ns, modified)
        self.assertEqual(self.tick(adapter, 104), ["unselected WAL append"])
        self.assertEqual(self.tick(adapter, 108), [])

    def test_source_tracking_stays_bounded_during_churn(self):
        adapter = self.adapter()
        self.setup_at(adapter)
        path = self.root / "chat.jsonl"
        module = importlib.import_module("adapters.structured")
        for n in range(280):
            path.write_bytes(self.line("new source message %d" % n))
            # Distinct identities in a fixture, without creating 280 files.
            with mock.patch.object(module.StructuredAdapter, "_source_key", return_value="source-%d" % n):
                self.assertEqual(self.tick(adapter, 104 + 4 * n), ["new source message %d" % n])
            self.assertLessEqual(len(adapter._source_cache), adapter.CACHE_SOURCES)
            self.assertLessEqual(len(adapter._known_sources), 256)
            self.assertLessEqual(len(adapter._excluded_sources), 256)

    def test_discovery_is_rate_limited(self):
        (self.root / "chat.jsonl").write_bytes(self.line("historical message") * 10)
        adapter = self.adapter()
        module = importlib.import_module("adapters.structured")
        with mock.patch.object(module, "scan_files", wraps=module.scan_files) as scan:
            self.setup_at(adapter)
            initial = scan.call_count
            for clock in (100.1, 100.5, 101, 102):
                self.setup_at(adapter, clock)
                self.tick(adapter, clock)
            self.assertEqual(scan.call_count, initial)
            self.tick(adapter, 104)
            self.assertEqual(scan.call_count, initial + 2)

    def test_retired_database_connection_closes_and_cursor_resumes(self):
        first, _ = self.database("create table messages(id integer primary key, content text);", "messages-first.sqlite")
        adapter = self.adapter(MAX_DB=1)
        self.setup_at(adapter)
        probe = adapter.probes[0]
        connection = probe.db.con
        second, second_db = self.database("create table messages(id integer primary key, content text);"
                                          "insert into messages values(1,'second session message');", "messages-second.sqlite")
        st = (self.root / "messages-first.sqlite").stat()
        os.utime(second_db.path, ns=(st.st_atime_ns, st.st_mtime_ns + 10000000000))
        self.assertEqual(self.tick(adapter, 104), ["second session message"])
        self.assertIsNone(probe.db.con)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("select 1")
        self.put(first, "insert into messages values(1,'resumed first session message')")
        os.utime(probe.db.path, ns=(st.st_atime_ns, st.st_mtime_ns + 20000000000))
        self.assertEqual(self.tick(adapter, 108), ["resumed first session message"])
        self.assertIs(adapter.probes[0], probe)

    def test_removed_database_is_closed(self):
        writer, db = self.database("create table messages(id integer primary key, content text);")
        adapter = self.adapter()
        self.setup_at(adapter)
        probe = adapter.probes[0]
        probe.db.close()
        writer.close()
        db.close()
        Path(db.path).unlink()
        self.tick(adapter, 104)
        self.assertEqual(adapter.probes, [])
        self.assertIsNone(probe.db.con)

    def test_subclass_excluded_probe_is_not_readded(self):
        self.database("create table messages(id integer primary key, content text);")
        adapter = self.adapter()
        self.setup_at(adapter)
        probe = adapter.probes[0]
        adapter.probes = []
        self.tick(adapter, 104)
        self.assertEqual(adapter.probes, [])
        self.assertIsNone(probe.db.con)

    def test_existing_database_new_table_is_discovered(self):
        writer, _ = self.database("create table messages(id integer primary key, content text);")
        adapter = self.adapter()
        self.setup_at(adapter)
        writer.executescript("create table chat_events(id integer primary key, content text);"
                             "insert into chat_events values(1,'first new table message');")
        self.assertEqual(self.tick(adapter, 104), ["first new table message"])
        self.assertEqual(self.tick(adapter, 108), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
