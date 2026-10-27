"""
Codex CLI 会话监听器 → PetHub
监听 ~/.codex/sessions 下最新的 .jsonl 会话文件，把活动状态上报为任务。
实验性：按文件活动推断状态（有新行=running，静默 2 分钟=done）。
"""
import json
import time
import urllib.request
# 本机回环绝不能被系统代理劫持：装了 Clash / V2Ray 的机器上，
# 127.0.0.1 的请求会被代理转走，返回 502，看起来就像"桥没开"。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

BRIDGE = "http://127.0.0.1:8765/api/events"
HOME = Path.home()
SESSIONS = HOME / ".codex" / "sessions"
IDLE_DONE_SEC = 120


def newest_session():
    if not SESSIONS.exists():
        return None
    files = sorted(SESSIONS.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime)
    return files[-1] if files else None


def session_title(f):
    """从会话文件前几行提取用户第一条消息做标题"""
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for _ in range(30):
                line = fh.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                # 常见结构探测
                for key in ("text", "message", "content", "prompt"):
                    v = ev.get(key)
                    if isinstance(v, str) and len(v) > 4:
                        return v[:40]
                    if isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict) and isinstance(item.get("text"), str):
                                return item["text"][:40]
    except Exception:
        pass
    return "Codex 会话"


def report(task_id, title, status, message=""):
    try:
        req = urllib.request.Request(BRIDGE,
            data=json.dumps({"platform": "Codex", "task_id": task_id, "title": title,
                             "status": status, "message": message},
                            ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        _opener.open(req, timeout=2)
    except Exception:
        pass


def main():
    cur_file, cur_id, cur_title = None, None, None
    last_size, last_active, reported = 0, time.time(), None
    while True:
        f = newest_session()
        now = time.time()
        if f != cur_file:
            cur_file = f
            if f:
                cur_id = f.stem[-20:]
                cur_title = session_title(f)
                last_size = f.stat().st_size
                last_active = now
                reported = None
                report(cur_id, cur_title, "running", "会话开始")
        elif f:
            size = f.stat().st_size
            if size != last_size:
                last_size = size
                last_active = now
                if reported != "running":
                    report(cur_id, cur_title, "running", "执行中")
                    reported = "running"
            elif reported != "done" and now - last_active > IDLE_DONE_SEC:
                report(cur_id, cur_title, "done", "会话静默")
                reported = "done"
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
