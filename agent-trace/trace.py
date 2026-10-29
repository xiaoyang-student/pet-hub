# -*- coding: utf-8 -*-
"""AgentTrace —— AI 办公软件的统一逐步追踪器（当前 8 款，见 core/engine.py:ADAPTERS）。

用法：
    python trace.py scan                 # 自动扫机器，看认出了哪几款
    python trace.py selftest             # 逐个通电自检，打印真实抓到的步骤
    python trace.py watch                # 持续追踪，打印每一步
    python trace.py watch --out log.jsonl --pethub
    python trace.py status               # 只看当前状态

零配置：装了哪款就自动挂哪款，不需要手工导入。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import discover                       # noqa: E402
from core.engine import Engine, run             # noqa: E402
from core.sinks import JsonlSink, PetHubSink, StdoutSink   # noqa: E402
from core.model import PHASE_TEXT, short        # noqa: E402

TXT = {
    "zh": {
        "detected": "本机检测到的 AI 办公软件",
        "none": "未检测到（未安装，或安装在非常规位置）",
        "not_found": "没有检测到任何一款。请确认软件已安装并至少运行过一次。",
        "cols": ("软件", "级别", "版本", "运行中", "数据/安装路径"),
        "yes": "是", "no": "否",
        "mount": "已挂载",
        "skip": "无数据源，跳过",
        "steps": "抓到步骤数",
        "watch": "开始追踪（Ctrl+C 结束）…",
        "no_adapter": "没有可追踪的软件，先运行：python trace.py scan",
        "self_head": "自检：每款轮询若干轮，统计真实抓到的步骤",
        "already": "追踪器已经在运行了，本次不再重复启动。",
    },
    "en": {
        "detected": "AI apps detected on this PC",
        "none": "not found (not installed, or installed in a non-standard path)",
        "not_found": "Nothing detected. Install the app and run it at least once.",
        "cols": ("App", "Tier", "Version", "Running", "Data / install path"),
        "yes": "yes", "no": "no",
        "mount": "mounted",
        "skip": "no data source, skipped",
        "steps": "steps captured",
        "watch": "Tracking started (Ctrl+C to stop)...",
        "no_adapter": "No trackable app. Run: python trace.py scan",
        "self_head": "Self-test: poll each app a few rounds and count real steps",
        "already": "Tracer already running, not starting another one.",
    },
}


def make_log(quiet=False):
    def log(*a):
        if not quiet:
            print("[trace]", *a)
    return log


# ---------------------------------------------------------------- 心跳
# 桌宠启动时看这个文件：90 秒内更新过就不重复拉起追踪器。
def _runtime_dir():
    """取 %LOCALAPPDATA%\\agent-trace；环境变量缺失时按约定路径兜底。

    有的启动方式（服务、计划任务、精简 shell）不传 LOCALAPPDATA，
    这时不能把心跳写到用户主目录去，否则桌宠永远找不到、反复拉起。
    """
    base = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not base:
        cand = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        if os.path.isdir(cand):
            base = cand
    if not base:
        base = os.path.expanduser("~")
    return os.path.join(base, "agent-trace")


RUNTIME_DIR = _runtime_dir()
HEARTBEAT = os.path.join(RUNTIME_DIR, "heartbeat.json")


_instance_file = None


def claim_single_instance():
    global _instance_file
    if _instance_file is not None:
        return True
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    handle = open(os.path.join(RUNTIME_DIR, "instance.lock"), "a+b")
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        # 内核持锁跨休眠有效，进程退出自动释放，不依赖心跳新鲜度。
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _instance_file = handle
    return True


def start_heartbeat():
    def beat():
        while True:
            try:
                os.makedirs(RUNTIME_DIR, exist_ok=True)
                tmp = HEARTBEAT + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "ts": time.time()}, f)
                os.replace(tmp, HEARTBEAT)
            except Exception:
                pass
            time.sleep(8)
    th = threading.Thread(target=beat, name="trace-heartbeat", daemon=True)
    th.start()


def cmd_scan(lang):
    t = TXT[lang]
    info = discover.detect_all()
    print("\n=== %s ===" % t["detected"])
    print("%-16s %-4s %-12s %-6s %s" % t["cols"])
    print("-" * 92)
    n = 0
    for key, d in info.items():
        if not d.get("installed"):
            continue
        n += 1
        path = d.get("install_path") or (d.get("roots") or [""])[0]
        name = d["zh"] if lang == "zh" else d["en"]
        print("%-16s %-4s %-12s %-6s %s" % (
            name, d["tier"], d.get("version") or "-",
            t["yes"] if d.get("running") else t["no"], short(path, 44)))
    if n == 0:
        print(t["not_found"])
    else:
        print("-" * 92)
        print(("共 %d 款" % n) if lang == "zh" else ("total: %d" % n))
    return 0


def cmd_selftest(lang, only, seconds=6.0, replay=False):
    t = TXT[lang]
    log = make_log(quiet=replay)
    eng = Engine(log=log, only=only)
    eng.build()
    if not eng.adapters:
        print(t["no_adapter"])
        return 1
    print("\n=== %s ===" % t["self_head"])
    counts = {k: 0 for k in eng.adapters}
    samples = {k: [] for k in eng.adapters}
    if replay:
        for ad in eng.adapters.values():
            try:
                ad.replay(10)
            except Exception:
                pass
        rounds, gap = 4, 0.35
    else:
        t0 = time.time()
        rounds, gap = int(seconds / 0.3) + 1, 0.3
        _ = t0
    for _ in range(rounds):
        for e in eng.poll():
            counts[e.app] += 1
            if len(samples[e.app]) < 3:
                samples[e.app].append(e)
        time.sleep(gap)
    for key, ad in eng.adapters.items():
        name = ad.zh if lang == "zh" else ad.en
        print("\n[%s] %s  src=%s" % (key, name, short(ad.source, 40)))
        print("   %s: %d" % (t["steps"], counts[key]))
        for s in samples[key]:
            print("     - %-9s %s" % (s.kind, short(s.text, 100)))
    return 0


def cmd_status(lang, only):
    eng = Engine(log=make_log(quiet=True), only=only)
    eng.build()
    if not eng.adapters:
        print(TXT[lang]["no_adapter"])
        return 1
    for st in eng.statuses():
        ad = eng.adapters[st["app"]]
        name = ad.zh if lang == "zh" else ad.en
        ph = st["phase"]
        phase = PHASE_TEXT.get(ph, (ph, ph))[0 if lang == "zh" else 1]
        print("%-14s %-10s %-14s %s" % (name, phase, short(st["title"], 18),
                                        short(st["detail"], 70)))
    return 0


def cmd_watch(lang, only, out, pethub, seconds=None):
    t = TXT[lang]
    log = make_log()
    if not claim_single_instance():
        print(t["already"])
        return 0
    start_heartbeat()
    eng = Engine(log=log, only=only)
    eng.build()
    if not eng.adapters:
        print(t["no_adapter"])
        return 1
    eng.sinks.append(StdoutSink())
    if out:
        eng.sinks.append(JsonlSink(out))
    if pethub:
        eng.sinks.append(PetHubSink())
    print(t["watch"])
    try:
        run(eng, stop_after=seconds)
    except KeyboardInterrupt:
        print("\n(stopped)")
    finally:
        for s in eng.sinks:
            try:
                s.close()
            except Exception:
                pass
    return 0


def main():
    ap = argparse.ArgumentParser(prog="trace.py",
                                 description="AgentTrace - unified step tracker for 7 AI office apps")
    ap.add_argument("cmd", nargs="?", default="scan",
                    choices=["scan", "selftest", "status", "watch"])
    ap.add_argument("--lang", default="zh", choices=["zh", "en"])
    ap.add_argument("--only", default="", help="comma separated app keys")
    ap.add_argument("--out", default="", help="write events to this JSONL file")
    ap.add_argument("--pethub", action="store_true", help="push to PetHub bridge")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds")
    ap.add_argument("--replay", action="store_true",
                    help="replay the most recent real records to verify the probe")
    a = ap.parse_args()
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    if a.cmd == "scan":
        return cmd_scan(a.lang)
    if a.cmd == "selftest":
        return cmd_selftest(a.lang, only, a.seconds or 6.0, a.replay)
    if a.cmd == "status":
        return cmd_status(a.lang, only)
    return cmd_watch(a.lang, only, a.out, a.pethub, a.seconds or None)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
