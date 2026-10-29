# -*- coding: utf-8 -*-
"""ZCode CLI lifecycle + indexed, read-only WAL message enrichment.

Only turn.completed is unconditional completion evidence. A persisted
turn_complete additionally needs the matching turn_usage.status=completed.
GUI task status, model completions and step-finish are never turn results.
Existing logs attach at EOF; TailReader owns truncation/rotation handling.
No database-wide message scan, GUI index, headers, tool arguments or outputs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from adapters.base import Adapter, TailReader
from core.model import (K_ANSWER, K_ASK, K_RESULT, K_STATUS, K_THINK, K_TOOL,
                        K_TOOL_DONE, K_USER, short)


@dataclass
class _Message:
    id: str
    role: str
    cursor: tuple = (-1, -1, "")
    watch: OrderedDict = field(default_factory=OrderedDict)
    seen: OrderedDict = field(default_factory=OrderedDict)
    drained: bool = False


@dataclass
class _Turn:
    session: str
    id: str
    event_ts: float
    touched: float
    user_id: str = ""
    cursor: tuple = (-1, -1, "")
    messages: OrderedDict = field(default_factory=OrderedDict)
    db_sig: object = None
    usage_completed: bool = False
    user_sent: bool = False
    status_sent: bool = False
    answer_sent: bool = False
    drained: bool = False
    terminal: str = ""
    terminal_ts: float = 0.0
    terminal_at: float = 0.0
    duration_ms: int | None = None
    done: bool = False
    native_sig: object = None
    # Exact callID -> (rowid, pending). Resolved entries are bounded tombstones.
    native_calls: OrderedDict = field(default_factory=OrderedDict)
    native_retired: int = 0
    ask_at: float = 0.0
    resumed: bool = False


class ZCodeAdapter(Adapter):
    key, zh, en, tier = "zcode", "ZCode", "ZCode", "A"
    MAX_LOGS = 8
    MAX_DIRECTORY_ENTRIES = 2048
    SCAN_INTERVAL = 1.0
    MAX_TURNS = 128
    TURN_TTL = 6 * 60 * 60
    TURNS_PER_TICK = 8
    MESSAGES_PER_TURN = 64
    MESSAGES_PER_TICK = 8
    PAGE = 16
    WATCH_PARTS = 8
    SEEN_PARTS = 64
    MAX_JSON = 64 << 10
    QUERY_BUDGET = 96
    DB_SECONDS = 0.10
    DB_INSTRUCTIONS = 200_000
    COMPLETION_GRACE = 3.0
    NATIVE_CALLS = 32
    ASK_INTERVAL = 5.0
    ASK_TEXT = "等待你在 ZCode 中回答问题"
    RESUME_TEXT = "询问已处理，继续思考"
    _LOG_NAME = re.compile(r"^zcode-.*\.jsonl$")

    def __init__(self, info, log=None):
        super().__init__(info, log)
        self.cli = None
        self.db_path = None
        self._readers = OrderedDict()
        self._turns = OrderedDict()
        self._attached = 0.0
        self._retired_before = 0.0
        self._scan_at = 0.0
        self._schema_key = None
        self._indexes = None
        self._queries = 0

    def setup(self):
        # Discovery sometimes supplies only the Electron installation/GUI root.
        # Do not recursively probe that root's unrelated SQLite/config files.
        candidates = []
        for root in self.roots + [str(Path.home() / ".zcode")]:
            p = Path(root)
            candidates.append(p if p.name.lower() == "cli" else p / "cli")
        self.cli = next((p for p in candidates if p.is_dir()), None)
        if self.cli is None:
            return False
        self.db_path = self.cli / "db" / "db.sqlite"
        self.source = str(self.cli / "log") + " + " + str(self.db_path)
        self._attached = time.time()
        self._retired_before = self._attached
        self._readers.clear()
        self._turns.clear()
        self._schema_key = self._indexes = None
        self._scan(self._attached, initial=True)
        self.enabled = True
        return True

    def _scan(self, now, initial=False):
        if not initial and now < self._scan_at:
            return
        self._scan_at = now + self.SCAN_INTERVAL
        found = []
        try:
            with os.scandir(self.cli / "log") as entries:
                for i, ent in enumerate(entries):
                    if i >= self.MAX_DIRECTORY_ENTRIES:
                        break
                    if not self._LOG_NAME.fullmatch(ent.name) or not ent.is_file():
                        continue
                    st = ent.stat()
                    found.append((st.st_mtime_ns, ent.path))
        except OSError:
            return
        paths = [p for _, p in sorted(found, reverse=True)[:self.MAX_LOGS]]
        for p in list(self._readers):
            if p not in paths:
                del self._readers[p]
        for p in paths:
            if p not in self._readers:
                self._readers[p] = TailReader(
                    p, start_at_end=initial, max_step=128 << 10,
                    max_line=self.MAX_JSON, max_lines=128)

    @staticmethod
    def _json(raw):
        if not isinstance(raw, str) or len(raw) > ZCodeAdapter.MAX_JSON:
            return {}
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except (ValueError, RecursionError):
            return {}

    @staticmethod
    def _object(value):
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _id(value):
        return value if isinstance(value, str) and 0 < len(value) <= 256 else ""

    @staticmethod
    def _timestamp(value):
        if not isinstance(value, str) or len(value) > 40:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            return None

    def _ingest(self, obj, now):
        event = obj.get("event")
        ctx = self._object(obj.get("context"))
        if event == "turn.started":
            if obj.get("status") != "started":
                return
        elif event == "turn.completed":
            if obj.get("status") != "completed":
                return
        elif event == "session.event.persistence.completed":
            if (ctx.get("sessionEventType") != "turn_complete"
                    or obj.get("status") != "completed"):
                return
        else:
            return
        sid, tid = self._id(obj.get("sessionId")), self._id(obj.get("turnId"))
        ts = self._timestamp(obj.get("timestamp"))
        if not sid or not tid or ts is None or ts > now + 60:
            return
        key = sid, tid
        turn = self._turns.get(key)
        if ts < self._attached or (turn is None and ts <= self._retired_before):
            return
        if turn is None:
            turn = self._turns[key] = _Turn(sid, tid, ts, now)
        if turn.done:
            return
        turn.event_ts = max(turn.event_ts, ts)
        turn.touched = now
        if event != "turn.started":
            # Terminal evidence fences out even delayed/stale native tool rows.
            turn.native_calls.clear()
            turn.ask_at, turn.resumed = 0.0, False
            # Prefer direct turn completion over a persistence notification.
            if not turn.terminal:
                turn.terminal_at = now
            if turn.terminal != "turn.completed":
                turn.terminal, turn.terminal_ts = event, ts
                turn.db_sig = None
            duration = obj.get("durationMs")
            if isinstance(duration, (int, float)) and 0 <= duration < 1e12:
                turn.duration_ms = int(duration)
        self._trim(now)

    def _trim(self, now):
        for key, turn in list(self._turns.items()):
            if now - turn.touched > self.TURN_TTL:
                self._retired_before = max(self._retired_before, turn.event_ts)
                del self._turns[key]
        while len(self._turns) > self.MAX_TURNS:
            _, turn = self._turns.popitem(last=False)
            # Bounded dedupe: retired history cannot re-enter on log rollback.
            self._retired_before = max(self._retired_before, turn.event_ts)

    def _db_signature(self):
        sig = []
        for p in (self.db_path, Path(str(self.db_path) + "-wal")):
            try:
                st = p.stat()
                sig.append((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns))
            except OSError:
                sig.append(None)
        return tuple(sig)

    def _q(self, con, sql, args=()):
        if self._queries <= 0:
            raise sqlite3.OperationalError("ZCode read budget exhausted")
        self._queries -= 1
        return con.execute(sql, args).fetchall()

    @staticmethod
    def _quote(name):
        return '"' + name.replace('"', '""') + '"'

    def _schema(self, con, sig):
        version = self._q(con, "PRAGMA schema_version")[0][0]
        key = sig[0][:2], version
        if key == self._schema_key:
            return bool(self._indexes)
        required = {
            "turn_usage": {"session_id", "turn_id", "user_message_id", "status", "duration_ms"},
            "message": {"id", "session_id", "sequence", "time_created", "data"},
            "part": {"id", "message_id", "sequence", "time_created", "data"},
        }
        prefixes = {
            "turn_usage": [("session_id", "turn_id")],
            "message": [("id",), ("session_id", "sequence", "time_created", "id")],
            "part": [("id",), ("message_id", "sequence", "time_created", "id")],
        }
        indexes = {}
        for table, columns in required.items():
            cols = self._q(con, "PRAGMA table_info(%s)" % self._quote(table))
            if not columns.issubset({r[1] for r in cols}):
                self._schema_key, self._indexes = key, None
                return False
            found = {}
            for row in self._q(con, "PRAGMA index_list(%s)" % self._quote(table)):
                if row[4]:  # Partial indexes do not guarantee a bounded lookup.
                    continue
                names = tuple(r[2] for r in self._q(
                    con, "PRAGMA index_info(%s)" % self._quote(row[1])))
                for prefix in prefixes[table]:
                    if names[:len(prefix)] == prefix:
                        found[prefix] = self._quote(row[1])
            if any(p not in found for p in prefixes[table]):
                self._schema_key, self._indexes = key, None
                return False
            indexes[table] = found[prefixes[table][-1]]
        # Optional native GUI metadata. Older CLI databases need neither this
        # table nor its indexes. Require the exact rowid-ordered turn index and
        # unique session/call lookup; never fall back to a scan or temporary sort.
        columns = {"session_id", "turn_id", "tool_call_id", "tool_name",
                   "status", "approval_status", "completed_at"}
        cols = self._q(con, 'PRAGMA table_info("tool_usage")')
        native = {}
        if (columns.issubset({r[1] for r in cols})
                and not any(r[1].lower() == "rowid" for r in cols)):
            for row in self._q(con, 'PRAGMA index_list("tool_usage")'):
                if row[4]:
                    continue
                info = self._q(con, "PRAGMA index_xinfo(%s)" % self._quote(row[1]))
                names = tuple(r[2] for r in info if r[5])
                if (names == ("session_id", "turn_id")
                        and all(r[4] == "BINARY" for r in info)
                        and any(r[1] == -1 and not r[5] for r in info)):
                    native["tool_usage"] = self._quote(row[1])
                if (names == ("session_id", "tool_call_id") and row[2]
                        and all(r[4] == "BINARY" for r in info)):
                    native["tool_call"] = self._quote(row[1])
        if len(native) == 2:
            indexes.update(native)
        self._schema_key, self._indexes = key, indexes
        return True

    def _emit_turn(self, turn, kind, text, now, **meta):
        self.cur_session, self.cur_turn = turn.session, turn.id
        if kind == K_STATUS and "phase" in meta:
            self.phase = meta["phase"]
        return self.emit(kind, text, now, session=turn.session, turn=turn.id,
                         **meta)

    def _native_questions(self, con, turn, sig):
        if turn.native_sig == sig:
            return
        if turn.terminal or "tool_usage" not in self._indexes:
            turn.native_sig = sig
            return
        # The real session/turn index ends in implicit rowid: a bounded recent
        # tail finds new calls even when no usable message anchor exists.
        # Read only metadata, never input/questions, errors, or tool output.
        fields = """rowid,substr(tool_call_id,1,257),substr(tool_name,1,121),
                    substr(status,1,32),substr(approval_status,1,32),
                    completed_at IS NOT NULL"""
        rows = self._q(con, """SELECT %s FROM tool_usage INDEXED BY %s
            WHERE session_id COLLATE BINARY=? AND turn_id COLLATE BINARY=?
            ORDER BY rowid DESC LIMIT ?"""
            % (fields, self._indexes["tool_usage"]),
            (turn.session, turn.id, self.NATIVE_CALLS))
        seen = {r[1] for r in rows}
        pending = [cid for cid, (_, waiting) in turn.native_calls.items()
                   if waiting and cid not in seen]
        if pending:
            # Pending calls may have left the recent tail. Refresh them by exact
            # unique session/call ID AND turn, in one bounded indexed query.
            rows += self._q(con, """SELECT %s FROM tool_usage INDEXED BY %s
                WHERE session_id COLLATE BINARY=? AND turn_id COLLATE BINARY=?
                AND tool_call_id COLLATE BINARY IN (%s) LIMIT ?"""
                % (fields, self._indexes["tool_call"], ",".join("?" for _ in pending)),
                (turn.session, turn.id, *pending, self.NATIVE_CALLS))
        # Apply only after all reads succeed; an interrupted/partial poll cannot
        # resolve a call. Absence/deletion is not resolution evidence either.
        for rid, cid, name, status, approval, completed in sorted(rows):
            if name != "AskUserQuestion" or not self._id(cid):
                continue
            previous = turn.native_calls.get(cid)
            waiting = status == "running" and approval == "requested" and not completed
            resolved = (completed or status in ("completed", "error", "cancelled")
                        or bool(approval and approval != "requested"))
            if previous is not None:
                if previous[1] and resolved:
                    turn.native_calls[cid] = (previous[0], False)
                    turn.resumed = True
                continue  # Resolved calls never reopen on stale part/DB evidence.
            if rid <= turn.native_retired or not (waiting or resolved):
                continue
            turn.native_calls[cid] = (rid, waiting)
            if waiting:
                turn.ask_at = 0.0
            while len(turn.native_calls) > self.NATIVE_CALLS:
                # Prefer retiring a resolved tombstone over an unanswered call.
                old = next((c for c, (_, p) in turn.native_calls.items() if not p),
                           next(iter(turn.native_calls)))
                old_rid, _ = turn.native_calls.pop(old)
                turn.native_retired = max(turn.native_retired, old_rid)
        turn.native_sig = sig

    def _question_status(self, now, out):
        activity = {}
        for step in out:
            activity.setdefault((step.session, step.turn), []).append(step)
        # Heartbeats are independent of the DB batch and file signature, so even
        # waiting turns outside TURNS_PER_TICK retain their asking state.
        for key, turn in self._turns.items():
            if turn.terminal or turn.done:
                continue
            pending = [cid for cid, (_, waiting) in turn.native_calls.items() if waiting]
            if pending:
                # SSE delivers each step, so a final ask cannot hide intermediate state changes.
                for step in activity.get(key, ()):
                    if step.kind in (K_USER, K_ANSWER, K_THINK, K_TOOL, K_TOOL_DONE, K_STATUS):
                        step.meta["phase"] = "asking"
                if (not turn.ask_at or now - turn.ask_at >= self.ASK_INTERVAL
                        or key in activity or turn.resumed):
                    out.append(self._emit_turn(turn, K_ASK, self.ASK_TEXT, now,
                                              evidence="cli.db.tool_usage",
                                              tool_call_ids=pending))
                    turn.ask_at = now
            elif turn.resumed:
                out.append(self._emit_turn(turn, K_STATUS, self.RESUME_TEXT, now,
                                          phase="thinking", evidence="cli.db.tool_usage"))
                turn.ask_at = 0.0
            turn.resumed = False

    def _visible(self, data):
        sem = self._object(data.get("semantics"))
        metadata = self._object(data.get("metadata"))
        return (not data.get("synthetic")
                and data.get("visibility") != "hidden"
                and metadata.get("visibility") != "hidden"
                and sem.get("uiVisibility") != "hidden"
                and sem.get("kind") not in ("system_reminder", "title", "memory"))

    def _part(self, turn, message, pid, raw, now, out):
        data = self._json(raw)
        if not self._visible(data):
            return
        typ = data.get("type")
        text, kind, stage = "", "", ""
        if typ in ("text", "reasoning"):
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            if message.role == "user":
                if typ != "text":
                    return
                kind = K_USER
            else:
                kind = K_ANSWER if typ == "text" else K_THINK
        elif typ == "tool" and message.role == "assistant":
            # Conservative optional typed-part support, never serialize args,
            # headers or tool output into status. No generic *.completed rule.
            state = self._object(data.get("state"))
            name, stage = data.get("tool"), state.get("status")
            if not isinstance(name, str) or not re.fullmatch(r"[\w.:-]{1,120}", name):
                return
            if (name == "AskUserQuestion"
                    and self._id(data.get("callID")) in turn.native_calls):
                return  # Native approval metadata, not a possibly stale part, owns this call.
            if stage in ("pending", "running"):
                kind, text = K_TOOL, name
                stage = "running"
            elif stage in ("completed", "error"):
                kind, text = K_TOOL_DONE, name + " : " + stage
            else:
                return
        else:
            return
        digest = hashlib.sha256((kind + "\0" + text).encode("utf-8")).digest()
        previous = message.seen.get(pid)
        if previous == digest:
            return
        message.seen[pid] = digest
        message.seen.move_to_end(pid)
        while len(message.seen) > self.SEEN_PARTS:
            message.seen.popitem(last=False)
        # A final turn cannot be inferred from a text part or finish=stop.
        meta = {"evidence": "cli.db.part", "message_id": message.id,
                "part_id": pid}
        if stage:
            meta["tool_status"] = stage
        if kind == K_USER:
            turn.user_sent = True
            self.title = short(text, 60)
        if kind == K_ANSWER:
            turn.answer_sent = True
        out.append(self._emit_turn(turn, kind, text, now, **meta))

    def _read_parts(self, con, turn, message, now, out):
        # Refresh the small streaming tail, then advance an indexed keyset page.
        for pid in list(message.watch):
            rows = self._q(con, "SELECT substr(data,1,?) FROM part WHERE id=? LIMIT 1",
                           (self.MAX_JSON + 1, pid))
            if rows:
                self._part(turn, message, pid, rows[0][0], now, out)
        rows = self._q(con, """SELECT id,sequence,time_created,substr(data,1,?)
            FROM part INDEXED BY %s WHERE message_id=?
            AND (sequence,time_created,id)>(?,?,?)
            ORDER BY sequence,time_created,id LIMIT ?""" % self._indexes["part"],
            (self.MAX_JSON + 1, message.id, *message.cursor, self.PAGE))
        for pid, seq, created, raw in rows:
            message.cursor = seq, created, pid
            self._part(turn, message, pid, raw, now, out)
            message.watch[pid] = None
            while len(message.watch) > self.WATCH_PARTS:
                message.watch.popitem(last=False)
        message.drained = len(rows) < self.PAGE

    def _enrich(self, con, turn, sig, now, out):
        rows = self._q(con, """SELECT user_message_id,status,duration_ms
            FROM turn_usage INDEXED BY %s WHERE session_id=? AND turn_id=? LIMIT 1"""
            % self._indexes["turn_usage"], (turn.session, turn.id))
        if not rows or not self._id(rows[0][0]):
            turn.db_sig = sig  # Retry only when the DB/WAL changes.
            return
        uid, status, duration = rows[0]
        turn.usage_completed = status == "completed"
        if isinstance(duration, int) and 0 <= duration < 1e12:
            turn.duration_ms = duration
        if not turn.user_id:
            users = self._q(con, """SELECT sequence,time_created,substr(data,1,?)
                FROM message WHERE id=? AND session_id=? LIMIT 1""",
                (self.MAX_JSON + 1, uid, turn.session))
            if not users:
                turn.db_sig = sig
                return
            seq, created, raw = users[0]
            data = self._json(raw)
            anchor = self._object(data.get("anchor"))
            if (data.get("role") != "user" or not self._visible(data)
                    or anchor.get("turnId", turn.id) != turn.id
                    or not isinstance(seq, int) or not isinstance(created, int)):
                turn.db_sig = sig
                return
            turn.user_id = uid
            turn.cursor = seq, created, uid
            turn.messages[uid] = _Message(uid, "user")
        # Message anchoring is exact; synthetic runtime prompts and other turns
        # in the same session cannot become this turn's answer.
        rows = self._q(con, """SELECT id,sequence,time_created,substr(data,1,?)
            FROM message INDEXED BY %s WHERE session_id=?
            AND (sequence,time_created,id)>(?,?,?)
            ORDER BY sequence,time_created,id LIMIT ?""" % self._indexes["message"],
            (self.MAX_JSON + 1, turn.session, *turn.cursor, self.PAGE))
        for mid, seq, created, raw in rows:
            turn.cursor = seq, created, mid
            data = self._json(raw)
            anchor = self._object(data.get("anchor"))
            sem = self._object(data.get("semantics"))
            if (data.get("role") != "assistant" or data.get("parentID") != turn.user_id
                    or anchor.get("turnId", turn.id) != turn.id
                    or sem.get("kind", "assistant_response") != "assistant_response"
                    or not self._visible(data)):
                continue
            if mid not in turn.messages:
                turn.messages[mid] = _Message(mid, "assistant")
            while len(turn.messages) > self.MESSAGES_PER_TURN:
                turn.messages.popitem(last=False)
        for mid in list(turn.messages)[:self.MESSAGES_PER_TICK]:
            self._read_parts(con, turn, turn.messages[mid], now, out)
            turn.messages.move_to_end(mid)
        turn.drained = len(rows) < self.PAGE and all(m.drained for m in turn.messages.values())
        if turn.drained:
            turn.db_sig = sig

    def tick(self, now):
        if not self.enabled:
            return []
        self._scan(now)
        for reader in self._readers.values():
            for line in reader.read_lines():
                self._ingest(self._json(line), now)
        self._trim(now)
        active = [(k, t) for k, t in self._turns.items() if not t.done][:self.TURNS_PER_TICK]
        if not active:
            return []
        out, con = [], None
        sig = self._db_signature()
        self._queries = self.QUERY_BUDGET
        need_db = sig[0] is not None and any(
            t.db_sig != sig or t.native_sig != sig for _, t in active)
        ready = False
        try:
            try:
                if need_db:
                    con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro",
                                          uri=True, timeout=0.05)
                    con.execute("PRAGMA query_only=1")
                    started, instructions = time.monotonic(), [0]

                    def limit():
                        instructions[0] += 1000
                        return int(instructions[0] > self.DB_INSTRUCTIONS
                                   or time.monotonic() - started > self.DB_SECONDS)

                    con.set_progress_handler(limit, 1000)
                    ready = self._schema(con, sig)
                    if not ready:
                        for _, turn in active:
                            turn.db_sig = turn.native_sig = sig
            except (sqlite3.Error, OSError, ValueError):
                pass  # Lifecycle reporting still works while DB is unavailable.
            for _, turn in active:
                if ready:
                    try:
                        # Native questions do not depend on user/assistant message
                        # anchoring, and are polled only once per changed snapshot.
                        self._native_questions(con, turn, sig)
                        if turn.db_sig != sig:
                            self._enrich(con, turn, sig, now, out)
                    except (sqlite3.Error, ValueError, TypeError):
                        ready = False  # Retry a fresh snapshot on the next tick.
                if not turn.status_sent:
                    if not turn.user_sent:
                        text = "正在读取本轮结果" if turn.terminal else "本轮开始"
                        out.append(self._emit_turn(turn, K_STATUS, text, now,
                                                  phase="thinking", evidence="cli.lifecycle"))
                    turn.status_sent = True
                direct = turn.terminal == "turn.completed"
                confirmed = direct or (bool(turn.terminal) and turn.usage_completed)
                grace_over = now - turn.terminal_at >= self.COMPLETION_GRACE
                if confirmed and ((turn.answer_sent and turn.drained) or grace_over):
                    meta = {"evidence": turn.terminal, "event_ts": turn.terminal_ts}
                    if turn.duration_ms is not None:
                        meta["duration_ms"] = turn.duration_ms
                    out.append(self._emit_turn(turn, K_RESULT, "本轮已完成", now, **meta))
                    turn.done = True
                    turn.messages.clear()
        finally:
            if con is not None:
                con.close()
            # Rotating the entire batch would retain the same first turn when
            # there are <= TURNS_PER_TICK sessions, starving the last on budget.
            self._turns.move_to_end(active[0][0])
        self._question_status(now, out)
        return out
