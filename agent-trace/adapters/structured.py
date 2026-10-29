# -*- coding: utf-8 -*-
"""A 级通用适配器：自动找库 + 自动找日志，然后混合增量读取。

ZCode、千问办公这类软件把会话写进自己的库/日志里，
这里不做任何硬编码表名，全部交给 SchemaProbe 现场识表。
"""
from __future__ import annotations

import json
import os
import time
from collections import OrderedDict

from adapters.base import Adapter, TailReader, ReadOnlyDB, scan_files
from adapters.generic import SchemaProbe, parse_item
from core.model import K_ANSWER, K_ERROR, K_STATUS, K_TOOL, K_TOOL_DONE, K_USER, short


class StructuredAdapter(Adapter):
    """自动装配：先找库，再找日志，两者都算真实数据源。"""

    key = "?"
    # 子类可指定额外的搜索子目录
    DB_SUBDIRS = ("db", "data", "v2", "workspace", "storage", "")
    LOG_SUBDIRS = ("log", "logs", "history", "")
    MAX_DB = 2
    MAX_LOG = 2
    DISCOVER_EVERY = 3.0
    CACHE_SOURCES = 16

    def __init__(self, info, log=None):
        Adapter.__init__(self, info, log)
        self.probes = []
        self.tails = []
        self._initialized = False
        self._discover_at = 0.0
        self._source_cache = OrderedDict()
        self._owned_probes = []
        self._excluded_sources = OrderedDict()
        # Bounded dormant readers retain offsets/cursors, not just identities.
        # Their DB connections are closed; only selected probes stay open.
        self._known_sources = OrderedDict()

    def _dirs(self, subs):
        out = []
        for r in self.roots:
            if not r or not os.path.isdir(r):
                continue
            for s in subs:
                p = os.path.join(r, s) if s else r
                if os.path.isdir(p):
                    out.append(p)
        # 去重保序
        seen, res = set(), []
        for p in out:
            if p.lower() not in seen:
                seen.add(p.lower())
                res.append(p)
        return res

    @staticmethod
    def _source_key(path):
        return os.path.normcase(os.path.realpath(path))

    def setup(self):
        now = time.monotonic()
        if not self._initialized or now >= self._discover_at:
            self._discover(now)
        self.enabled = bool(self.enabled or self.probes or self.tails or
                            any(os.path.isdir(r) for r in self.roots if r))
        return self.enabled

    def _make_source(self, kind, path, start_at_end):
        if kind == "log":
            return TailReader(path, start_at_end=start_at_end)
        db = ReadOnlyDB(path)
        probe = SchemaProbe(db, self.log)
        if probe.build(start_at_end=start_at_end):
            return probe
        db.close()
        return None

    def _discover(self, now):
        self._discover_at = now + self.DISCOVER_EVERY
        initial = not self._initialized
        for probe in self._owned_probes:
            if probe not in self.probes:
                key = ("db", self._source_key(probe.db.path))
                probe.db.close()
                self._source_cache.pop(key, None)
                self._excluded_sources[key] = True
        old_probes = list(self.probes)
        selected = set()
        probes, tails = [], []
        for kind, subs, exts, score, maximum, result in (
                ("db", self.DB_SUBDIRS, (".sqlite", ".db"), -1, self.MAX_DB, probes),
                ("log", self.LOG_SUBDIRS, (".jsonl", ".ndjson", ".json"), 1, self.MAX_LOG, tails)):
            paths = scan_files(self._dirs(subs), exts=exts, max_depth=5,
                               max_files=30, min_score=score, min_size=0)
            candidates = []
            for path in paths:
                try:
                    st = os.stat(path)
                    modified = st.st_mtime_ns
                    if kind == "db":
                        try:
                            modified = max(modified, os.stat(path + "-wal").st_mtime_ns)
                        except OSError:
                            pass
                except OSError:
                    continue
                key = (kind, self._source_key(path))
                identity = (st.st_dev, st.st_ino)
                candidates.append((modified, path, key, identity))
                if initial and key not in self._known_sources:
                    # Snapshot every initial candidate (at most 30 per kind).
                    # Seeding only when it wins selection would skip events
                    # appended between discovery and that later selection.
                    obj = self._make_source(kind, path, start_at_end=True)
                    if obj is not None:
                        if kind == "db":
                            obj.db.close()
                        self._known_sources[key] = (identity, obj)
            for _, path, key, identity in sorted(candidates, reverse=True):
                if key in selected or key in self._excluded_sources or len(result) >= maximum:
                    continue
                cached = self._source_cache.get(key) or self._known_sources.get(key)
                obj = cached[1] if cached and cached[0] == identity else None
                if cached and obj is None and kind == "db":
                    cached[1].db.close()
                if obj is None:
                    obj = self._make_source(kind, path, start_at_end=initial)
                    if obj is None:
                        continue
                elif kind == "db":
                    obj.build(start_at_end=False)
                result.append(obj)
                selected.add(key)
                self._source_cache[key] = (identity, obj)
                self._source_cache.move_to_end(key)
                self._known_sources[key] = (identity, obj)
                self._known_sources.move_to_end(key)
        for probe in old_probes:
            if probe not in probes:
                probe.db.close()
        for key, (_, obj) in self._source_cache.items():
            if key not in selected and key[0] == "db":
                obj.db.close()
        while len(self._source_cache) > self.CACHE_SOURCES:
            key = next((k for k in self._source_cache if k not in selected), None)
            if key is None:
                break
            _, obj = self._source_cache.pop(key)
            if key[0] == "db":
                obj.db.close()
        for cache in (self._known_sources, self._excluded_sources):
            while len(cache) > 256:
                cache.popitem(last=False)
        changed = self.probes != probes or self.tails != tails
        self.probes, self.tails = probes, tails
        self._owned_probes = list(probes)
        self._initialized = True
        if changed or initial:
            paths = [p.db.path for p in probes] + [t.path for t in tails]
            self.source = ",".join(os.path.basename(p) for p in paths[:3]) or "waiting for source"
            self.log("[%s] 库=%d 日志=%d" % (self.key, len(probes), len(tails)))

    def tick(self, now):
        clock = time.monotonic()
        if clock >= self._discover_at:
            self._discover(clock)
        out = []
        for probe in self.probes:
            for kind, text, ts, meta in probe.poll():
                s = self.emit(kind, text, ts, seq=0, **meta)
                self._note(kind, ts)
                out.append(s)
        for t in self.tails:
            for line in self._take(t.read_lines()):
                out.extend(self._line(line, now))
        return self._take(out)

    def replay(self, n=25):
        Adapter.replay(self, n)
        for probe in self.probes:
            for tp in probe.pollers:
                try:
                    tp.rewind(40)
                except Exception:
                    pass
        for t in self.tails:
            try:
                t.rewind(40000)
            except Exception:
                pass

    def _line(self, line, now):
        obj = None
        try:
            obj = json.loads(line)
        except Exception:
            return []          # 非 JSON 行多为配置碎片，静默跳过
        if not isinstance(obj, (dict, list)):
            # 单行 JSON 的碎片（配置文件被逐行读）不算事件，直接丢弃
            return []
        if isinstance(obj, dict):
            ts = obj.get("ts") or obj.get("timestamp") or obj.get("time")
            if isinstance(ts, (int, float)) and ts > 1e11:
                ts = ts / 1000.0
            else:
                ts = now
            items = parse_item(obj, str(obj.get("type") or obj.get("event") or ""))
            if not items:
                return []
            res = []
            for kind, text, meta in items[:3]:
                s = self.emit(kind, text, ts, **meta)
                self._note(kind, ts)
                res.append(s)
            return res
        return []
