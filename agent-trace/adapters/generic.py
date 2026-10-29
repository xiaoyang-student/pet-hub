# -*- coding: utf-8 -*-
"""通用结构化探针：自动识表、自动定游标、自动解析消息体。

为什么需要它：
  同一款软件的不同版本，表名/列名会变（升级就换名）。与其把名字写死，
  不如让探针在启动时读一次表结构，自己挑出「序号列 / 正文列 / 类型列」，
  之后按唯一键和已有时间索引增量取行，不把会话局部序号当作全局游标。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import OrderedDict

from core.model import (K_ANSWER, K_EDIT, K_ERROR, K_RESULT, K_STATUS, K_THINK,
                        K_TOOL, K_TOOL_DONE, K_USER, short, first_line)

# 值得盯的表名关键字
TABLE_HINTS = ("message", "event", "item", "turn", "session", "chat", "task",
               "trajectory", "log", "step", "conversation", "history",
               "agent", "thread", "part", "span")
# 越靠前越优先盯（正文表 > 会话表 > 日志表）
HINT_RANK = ("item_json", "payload", "message", "event", "item", "turn",
             "session", "chat", "conversation", "thread", "history",
             "trajectory", "task", "step", "span", "log")

# 序号列候选（按优先级）
SEQ_COLS = ("seq", "sequence", "ordinal", "id", "rowid", "created_at",
            "occurred_at", "timestamp", "createdAt", "updated_at")
# 正文列候选
BODY_COLS = ("payload", "payload_json", "data", "json", "content", "body",
             "message", "message_json", "item_json", "parts", "text",
             "searchable_text", "arguments", "input")
# 类型列候选
TYPE_COLS = ("event_type", "type", "kind", "item_type", "message_type", "role",
             "operation", "status")
# 时间列候选
TS_COLS = ("created_at", "occurred_at", "timestamp", "createdAt", "updated_at",
           "created_time", "time")


def pick(cols, names):
    """按候选名挑列。先精确匹配，再按「列名包含候选词」匹配。

    软件的列名经常带前后缀（rollout_ordinal / created_at_ms / feedback_log_body），
    只认精确名会导致整张表被跳过，所以这里必须做包含匹配。
    """
    low = {c.lower(): c for c in cols}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    for n in names:
        nl = n.lower()
        best, best_len = "", 999
        for c in cols:
            cl = c.lower()
            if nl in cl and len(cl) < best_len:
                best, best_len = c, len(cl)
        if best:
            return best
    return ""


def _j(s):
    if not s:
        return None
    if isinstance(s, (dict, list)):
        return s
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8", "ignore")
        except Exception:
            return None
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s[0] not in "{[" or s[-1] not in "}]":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _blocks(o):
    """从各种消息体里挖出 content 块列表。"""
    if isinstance(o, dict):
        c = o.get("content")
        if isinstance(c, list):
            return [b for b in c if isinstance(b, dict)]
        if isinstance(c, str) and c.strip():
            return [{"type": "text", "text": c}]
        for k in ("message", "parts", "body", "payload", "item"):
            v = o.get(k)
            if isinstance(v, list):
                return [b for b in v if isinstance(b, dict)]
            if isinstance(v, dict):
                return _blocks(v)
        return [o]
    if isinstance(o, list):
        return [b for b in o if isinstance(b, dict)]
    return []


_JUNK_HEAD = re.compile(r"^\s*[\"'\[\{,]")
_JUNK_BODY = ("opencode.ai", "$schema", "initializedBuiltin", "text_picker",
              "current_user", "sqlite startup", "startup state", "heavy_ad")


def junk(t: str) -> bool:
    """把明显不是会话内容的碎片挡掉（JSON 残片、配置片段等）。"""
    s = (t or "").strip()
    if len(s) < 4:
        return True
    if _JUNK_HEAD.match(s) or s.endswith(","):
        return True
    low = s.lower()
    for k in _JUNK_BODY:
        if k.lower() in low:
            return True
    return False


def parse_item(o, kind_hint=""):
    """任意消息体 -> [(kind, text, meta)]。识别不了就丢一段摘要文本。"""
    out = []
    if not isinstance(o, (dict, list)):
        return out
    for b in _blocks(o)[:10]:
        bt = str(b.get("type") or b.get("event_type") or kind_hint or "")
        if bt == "tool_use" or (bt == "function_call"):
            name = str(b.get("name") or "tool")
            inp = b.get("input") or b.get("arguments") or {}
            arg = ""
            if isinstance(inp, dict):
                for k in ("command", "file_path", "path", "pattern", "query", "url",
                          "description", "filePath"):
                    if k in inp and isinstance(inp[k], str):
                        arg = inp[k]
                        break
                if not arg:
                    arg = short(json.dumps(inp, ensure_ascii=False), 120)
            else:
                arg = short(str(inp), 120)
            out.append((K_TOOL, name + ((" | " + arg) if arg else ""),
                        {"tool": name, "args": short(arg, 120)}))
        elif bt in ("tool_result", "function_result", "function_call_result"):
            tx = b.get("text") or b.get("content") or b.get("output") or ""
            if isinstance(tx, list):
                tx = " ".join(str(x) for x in tx[:5])
            if isinstance(tx, dict):
                tx = str(tx.get("text") or "")
            if str(tx).strip():
                out.append((K_TOOL_DONE, short(str(tx), 200), {}))
        elif bt == "thinking":
            tx = b.get("thinking") or b.get("text") or ""
            if str(tx).strip():
                out.append((K_THINK, short(str(tx), 200), {}))
        elif bt == "text":
            tx = b.get("text") or ""
            if str(tx).strip():
                out.append((K_ANSWER, short(str(tx), 200), {}))
        elif bt:
            # 不认识的类型：把能读的字段拼成一行，宁可少报也不错报
            tx = b.get("text") or b.get("content") or b.get("message") or ""
            if isinstance(tx, (dict, list)):
                tx = short(json.dumps(tx, ensure_ascii=False), 160)
            if str(tx).strip():
                role = str(b.get("role") or "")
                k = K_USER if role == "user" else (K_ANSWER if role == "assistant" else
                                                   (K_TOOL if "tool" in (bt or "").lower()
                                                    else K_STATUS))
                out.append((k, short(str(tx), 200), {}))
    if not out:
        # 整包兜底：可能是文件改动 / 事件记录
        for k in ("display_path", "path", "file_path", "filePath"):
            v = o.get(k) if isinstance(o, dict) else None
            if isinstance(v, str) and v:
                op = str(o.get("operation") or "edit")
                add = o.get("additions")
                dele = o.get("deletions")
                txt = "%s %s" % (op, os.path.basename(v))
                if add is not None or dele is not None:
                    txt += " +%s/-%s" % (add or 0, dele or 0)
                out.append((K_EDIT, txt, {"file": v}))
                break
        for k in ("text", "content", "message", "title", "summary", "name"):
            v = o.get(k) if isinstance(o, dict) else None
            if isinstance(v, str) and v.strip():
                out.append((K_STATUS, short(v, 200), {}))
                break
    return [(k, t, m) for k, t, m in out if not junk(t)]


def _ident(name):
    return '"' + name.replace('"', '""') + '"'


class TablePoller:
    """rowid 新增流 + 已有时间索引更新流；无索引更新仅复查最近 128 行。"""

    RECENT = 128
    MAX_BODY = 256 << 10

    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.cols = cols = db.cols(table) or []
        self.seq = pick(cols, SEQ_COLS)
        self.body = pick(cols, BODY_COLS)
        self.typec = pick(cols, TYPE_COLS)
        self.ts = pick(cols, TS_COLS)
        self.role = pick(cols, ("role",))
        self.last = None
        self._update_last = None
        self._ready = False
        self._start_at_end = True
        self._version = None
        self._backlog = True
        self._cycle = None
        self._recent = OrderedDict()
        self.is_log = "log" in table.lower() and "item" not in table.lower() \
            and "catalog" not in table.lower()
        info = self._query("PRAGMA table_info(%s)" % _ident(table)) or []
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        schema = self._query("select sql from sqlite_master where type='table' and name=?",
                             (table,)) or []
        without = bool(schema and re.search(r"\bWITHOUT\s+ROWID\b", schema[0][0] or "", re.I))
        self._rowid = next((n for n in ("_rowid_", "rowid", "oid")
                            if n not in {c.lower() for c in cols}), "") if not without else ""
        self._key = [self._rowid] if self._rowid else pk
        indexes = self._query("PRAGMA index_list(%s)" % _ident(table)) or []
        orders = []
        for idx in indexes:
            if len(idx) > 4 and idx[4]:
                continue
            parts = self._query("PRAGMA index_xinfo(%s)" % _ident(idx[1])) or []
            if parts and all(not r[3] and r[4] == "BINARY" and r[1] >= -1 for r in parts):
                orders.append([self._rowid if r[1] == -1 else r[2] for r in parts])
        updated = pick(cols, ("updated_at", "updatedAt", "modified_at", "modifiedAt",
                              "update_time", "modified_time", "time_updated"))
        created = pick(cols, TS_COLS)

        def indexed(column):
            order = [column] + [k for k in self._key if k != column] if column else []
            return order if self._key and any(p[:len(order)] == order for p in orders) else []

        self._updates = indexed(updated)
        self._order = [self._rowid] if self._rowid else (self._updates or indexed(created))
        if self._updates == self._order:
            self._updates = []
        self.ok = bool(self._order and (self.body or self.typec))
        self._sel = list(dict.fromkeys(c for c in
                         self._key + self._order + self._updates +
                         [self.body, self.typec, self.ts, self.role] if c))

    def _query(self, sql, args=()):
        try:
            rows = self.db.q(sql, args)
            return None if getattr(self.db, "last_error", None) is not None else rows
        except Exception:
            return None

    def _data_version(self):
        rows = self._query("PRAGMA data_version")
        return (self.db.con, rows[0][0]) if rows else None

    def _select(self):
        return ",".join(("substr(%s,1,%d)" % (_ident(c), self.MAX_BODY)
                         if c == self.body else _ident(c)) for c in self._sel)

    def _page(self, order, cursor=(), limit=60, reverse=False, values=True, upper=()):
        fields = ",".join(map(_ident, order))
        rows = []
        for part in (range(len(order) - 1, -1, -1) if cursor else (None,)):
            where, args = [], ()
            if len(order) > 1:
                where.append("%s is not null" % _ident(order[0]))
            if part is not None:
                where.extend(_ident(c) + "=?" for c in order[:part])
                where.append("%s%s?" % (_ident(order[part]), "<" if reverse else ">"))
                args = tuple(cursor[:part + 1])
            if upper:
                where.append("(%s)<=(%s)" % (fields, ",".join("?" for _ in order)))
                args += tuple(upper)
            sql = "select %s from %s%s order by %s limit %d" % (
                self._select() if values else fields, _ident(self.table),
                " where " + " and ".join(where) if where else "",
                ",".join(_ident(c) + (" desc" if reverse else "") for c in order), limit - len(rows))
            page = self._query(sql, args)
            if page is None:
                return None
            rows.extend(page)
            if len(rows) == limit:
                break
        return rows

    def _cursor(self, row, order):
        d = dict(zip(self._sel, row))
        return tuple(d[c] for c in order)

    def _remember(self, row):
        key = self._cursor(row, self._key)
        digest = hashlib.blake2b(repr(row).encode("utf-8", "backslashreplace"), digest_size=16).digest()
        changed = self._recent.get(key) != digest
        self._recent[key] = digest
        self._recent.move_to_end(key)
        while len(self._recent) > self.RECENT:
            self._recent.popitem(last=False)
        return changed

    def seed(self, start_at_end=True):
        self._start_at_end = start_at_end
        if not self.ok:
            return
        if not start_at_end:
            self.last = self._update_last = ()
            self._cycle = None
            self._recent.clear()
            self._ready = self._backlog = True
            return
        if not self.db.open():
            return
        owned = not self.db.con.in_transaction
        if owned and self._query("BEGIN") is None:
            return
        try:
            version = self._data_version()
            rows = self._page(self._order, limit=1, reverse=True, values=False)
            if version is None or rows is None:
                return
            updates = self._page(self._updates, limit=1, reverse=True, values=False) if self._updates else []
            if updates is None:
                return
            recent = self._page(self._order, limit=self.RECENT, reverse=True)
            if recent is None:
                return
        finally:
            if owned and self.db.con is not None:
                self._query("ROLLBACK")
        self.last = tuple(rows[0]) if rows else ()
        self._update_last = tuple(updates[0]) if updates else ()
        self._cycle = None
        self._recent.clear()
        for row in reversed(recent):
            self._remember(row)
        self._version = version
        self._ready, self._backlog = True, False

    def rewind(self, n=40):
        if not self.ok:
            return
        rows = self._page(self._order, limit=max(1, min(int(n), 4096)) + 1,
                          reverse=True, values=False)
        if rows is None:
            return
        self.last = tuple(rows[-1]) if len(rows) > n else ()
        self._update_last = self.last if self._updates == self._order else self._update_last
        self._cycle = None
        self._recent.clear()
        self._ready = self._backlog = True

    def poll(self, limit=60):
        if not self.ok:
            return []
        if not self._ready:
            self.seed(self._start_at_end)
            if not self._ready:
                return []
        limit = max(1, min(int(limit), 256))
        version = self._data_version()
        if version is None or (version == self._version and not self._backlog):
            return []
        # Each page uses one short snapshot. Never keep a transaction open while
        # waiting for the next poll, or commit cursors after a failed query.
        owned = not self.db.con.in_transaction
        if owned and self._query("BEGIN") is None:
            return []
        last, update_last, cycle = self.last, self._update_last, self._cycle
        reset = False
        try:
            if self._rowid:
                maximum = self._query("select max(%s) from %s" %
                                      (_ident(self._rowid), _ident(self.table)))
                if maximum is None:
                    return []
                end = (maximum[0][0],) if maximum[0][0] is not None else ()
                # MAX(rowid) is a B-tree endpoint lookup, not a table scan.
                # Also detect a rebuild while an older batch is still draining.
                reset = bool((last and end < last) or (cycle and end < cycle[1]))
                if reset:
                    last = update_last = ()
                    cycle = None
            if self._updates and cycle is None:
                high = self._page(self._updates, limit=1, reverse=True, values=False)
                if high is None:
                    return []
                update_end = max(update_last, tuple(high[0]) if high else ())
                cycle = (last, end, update_end, version)
            if cycle:
                floor, end, update_end, _ = cycle
                rows = self._page(self._order, last, limit, upper=end) if end else []
                updates = (self._page(self._updates, update_last, limit, upper=update_end)
                           if update_end else [])
            else:
                rows = self._page(self._order, last, limit)
                updates = []
            recent = []
            if not reset and version != self._version and self._rowid and self._recent:
                keys = [k[0] for k in self._recent]
                recent = self._query("select %s from %s where %s in (%s)" % (
                    self._select(), _ident(self.table), _ident(self._rowid),
                    ",".join("?" for _ in keys)), keys)
            if any(r is None for r in (rows, updates, recent)):
                return []
        finally:
            if owned and self.db.con is not None:
                self._query("ROLLBACK")

        def covered(row, watermark):
            stamp = self._cursor(row, self._updates)
            return stamp[0] is None or stamp <= watermark

        if reset:
            self._recent.clear()
        # A fixed batch boundary assigns every insert to the append stream,
        # regardless of timestamp order or eviction from the recent cache.
        # Updates newer than the snapshot belong to the next batch (including
        # rows changed before their append page was read).
        merged = OrderedDict()
        for row in recent:
            if not cycle or covered(row, update_last):
                merged[self._cursor(row, self._key)] = row
        for row in rows:
            if not cycle or covered(row, update_end):
                merged[self._cursor(row, self._key)] = row
        for row in updates:
            if self._cursor(row, self._key) <= floor:
                merged[self._cursor(row, self._key)] = row
        out = []
        for row in merged.values():
            if self._remember(row):
                out.extend(self._items(row))
        self.last = self._cursor(rows[-1], self._order) if rows else last
        self._update_last = self._cursor(updates[-1], self._updates) if updates else update_last
        self._backlog = len(rows) == limit or len(updates) == limit
        self._version = cycle[3] if cycle else version
        self._cycle = cycle if self._backlog else None
        if cycle and not self._backlog:
            self._update_last = update_end
        return out

    def _items(self, row):
        d = dict(zip(self._sel, row))
        raw = d.get(self.body)
        obj = _j(raw)
        if obj is None and isinstance(raw, str) and raw.strip():
            obj = {"content": raw}
        items = parse_item(obj, str(d.get(self.typec) or ""))
        if not items and (d.get(self.typec) or d.get(self.role)):
            items = [(K_STATUS, short("%s %s" % (d.get(self.role) or "",
                                                 d.get(self.typec) or ""), 160), {})]
        tv = d.get(self.ts)
        ts = (tv / 1000.0) if isinstance(tv, (int, float)) and tv > 1e11 else time.time()
        return [(K_STATUS if self.is_log and k == K_ANSWER else k, tx, ts, meta)
                for k, tx, meta in items]


class SchemaProbe:
    """在一个 SQLite 库里自动挑出该盯的表，并统一增量轮询。"""

    def __init__(self, db, log=None):
        self.db = db
        self.log = log or (lambda *a: None)
        self.pollers = []

    def build(self, max_tables=8, start_at_end=True):
        if not self.db or not self.db.path:
            return 0
        if not self.db.open():
            return len(self.pollers)
        try:
            rows = self.db.q("select name from sqlite_master where type='table'")
        except Exception:
            return len(self.pollers)
        if getattr(self.db, "last_error", None) is not None:
            return len(self.pollers)
        names = [r[0] for r in rows if isinstance(r[0], str)]
        names = [n for n in names if not n.startswith(("sqlite_", "__drizzle"))]
        names = [n for n in names
                 if any(h in n.lower() for h in TABLE_HINTS)]

        def rank(n):
            nl = n.lower()
            for i, h in enumerate(HINT_RANK):
                if h in nl:
                    return i
            return len(HINT_RANK)

        names.sort(key=lambda n: (rank(n), n))
        previous = {p.table: p for p in self.pollers}
        pollers = []
        for n in names[:max_tables]:
            if n in previous:
                pollers.append(previous[n])
                continue
            try:
                tp = TablePoller(self.db, n)
            except Exception:
                continue
            if tp.ok:
                tp.seed(start_at_end=start_at_end)
                pollers.append(tp)
        self.pollers = pollers
        if set(previous) != {p.table for p in pollers}:
            self.log("   表: " + ", ".join(p.table for p in pollers))
        return len(self.pollers)

    def poll(self, per=40):
        out = []
        for tp in self.pollers:
            try:
                out.extend(tp.poll(per))
            except Exception:
                continue
        return out
