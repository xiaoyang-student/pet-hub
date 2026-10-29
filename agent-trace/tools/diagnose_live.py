from __future__ import annotations

import argparse
from collections import Counter
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import discover
from core.engine import Engine


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage")]


def memory_mb():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return round(counters.WorkingSetSize / 1048576, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--bridge-port", type=int)
    args = parser.parse_args()
    if args.bridge_port is not None:
        if not 1 <= args.bridge_port <= 65535:
            parser.error("bridge port must be 1..65535")
        args.pipeline = True
    t0, cpu0 = time.monotonic(), time.process_time()
    found = discover.detect_all()
    eng = Engine(log=lambda *args: None)
    eng.build()
    result = {key: {"installed": bool(info.get("installed")), "running": bool(info.get("running")),
                    "mounted": key in eng.adapters, "kinds": Counter(), "sessions": set(),
                    "errors": Counter(), "fresh_events": 0, "lag_seconds": [], "tick_max_ms": 0}
              for key, info in found.items()}
    setup_seconds = time.monotonic() - t0
    if args.replay:
        for key, adapter in eng.adapters.items():
            try:
                adapter.replay(30)
            except Exception as exc:
                result[key]["errors"][type(exc).__name__] += 1
    mem = [memory_mb()]
    started, started_wall = time.monotonic(), time.time()
    next_mem = started
    polls = 0
    pipeline_max_ms = 0

    def record(event, now):
        row = result[event.app]
        row["kinds"][event.kind] += 1
        if event.session and len(row["sessions"]) < 1000:
            row["sessions"].add(hashlib.sha256(event.session.encode()).hexdigest()[:12])
        if event.ts >= started_wall:
            row["fresh_events"] += 1
            if len(row["lag_seconds"]) < 2000:
                row["lag_seconds"].append(max(0, now - event.ts))

    class Observer:
        def push(self, event):
            record(event, time.time())

    if args.pipeline:
        eng.sinks.append(Observer())
    delivery = None
    if args.bridge_port is not None:
        from core.sinks import PetHubSink
        delivery = PetHubSink(url="http://127.0.0.1:%d/api/events" % args.bridge_port)
        eng.sinks.append(delivery)
    while time.monotonic() - started < args.seconds:
        now = time.time()
        if args.pipeline:
            ts = time.perf_counter()
            eng.tick_housekeeping(now)
            eng.poll(now)
            pipeline_max_ms = max(pipeline_max_ms, (time.perf_counter() - ts) * 1000)
        else:
            for key, adapter in eng.adapters.items():
                if now < getattr(adapter, "_next", 0):
                    continue
                ts = time.perf_counter()
                try:
                    for event in adapter.tick(now) or []:
                        record(event, now)
                    adapter._next = now + adapter.interval(now)
                except Exception as exc:
                    result[key]["errors"][type(exc).__name__] += 1
                    adapter._next = now + 3
                result[key]["tick_max_ms"] = max(result[key]["tick_max_ms"], (time.perf_counter() - ts) * 1000)
        polls += 1
        if time.monotonic() >= next_mem:
            mem.append(memory_mb())
            next_mem = time.monotonic() + 5
        time.sleep(eng.next_wake(time.time()))
    for key, row in result.items():
        row["sessions"] = len(row["sessions"])
        lag = sorted(row.pop("lag_seconds"))
        row["lag_p95_seconds"] = round(lag[min(len(lag) - 1, int(len(lag) * .95))], 3) if lag else None
        row["tick_max_ms"] = round(row["tick_max_ms"], 2)
        row["running_end"] = discover.is_running(key)
        row["mounted"] = key in eng.adapters
        if args.pipeline:
            row.pop("tick_max_ms")
            row.pop("errors")
    elapsed = time.monotonic() - t0
    print(json.dumps({"mode": "historical_replay" if args.replay else "live_read_only",
                      "pipeline": args.pipeline, "pipeline_max_ms": round(pipeline_max_ms, 2),
                      "bridge_port": args.bridge_port,
                      "delivery_pending": len(delivery._held) if delivery else None,
                      "delivery_evicted": delivery.evicted if delivery else None,
                      "engine_dropped": eng.dropped, "engine_suppressed": eng.suppressed,
                      "pid": os.getpid(), "setup_seconds": round(setup_seconds, 3),
                      "observe_seconds": round(time.monotonic() - started, 2),
                      "cpu_percent_one_core": round(100 * (time.process_time() - cpu0) / elapsed, 2),
                      "working_set_mb_start": mem[0], "working_set_mb_end": mem[-1],
                      "working_set_mb_max": max(mem), "polls": polls, "apps": result},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
