"""
WorkBuddy Hook → PetHub 桥接脚本（与 claude_code_hook.py 同构）
在 ~/.workbuddy/settings.json 的 hooks 中按 connectors/README.md 配置。
"""
import json
import sys
import urllib.request
# 本机回环绝不能被系统代理劫持：装了 Clash / V2Ray 的机器上，
# 127.0.0.1 的请求会被代理转走，返回 502，看起来就像"桥没开"。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BRIDGE = "http://127.0.0.1:8765/api/events"
PLATFORM = "WorkBuddy"

MAP = {"start": "running", "running": "running", "waiting": "waiting",
       "done": "done", "error": "error"}


def main():
    if len(sys.argv) < 2:
        return
    status = MAP.get(sys.argv[1])
    if not status:
        return
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
    except Exception:
        ev = {}
    sess = str(ev.get("session_id") or ev.get("conversation_id") or "main")
    title = ev.get("title") or ev.get("prompt") or ev.get("tool_name") or "WorkBuddy 会话"
    if isinstance(title, str) and len(title) > 40:
        title = title[:40] + "…"
    msg = ev.get("tool_input", "")
    if isinstance(msg, dict):
        msg = msg.get("command") or msg.get("file_path") or msg.get("description") or ""
    body = {"platform": PLATFORM, "task_id": sess, "title": str(title),
            "status": status, "message": str(msg)[:80]}
    try:
        req = urllib.request.Request(BRIDGE, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        _opener.open(req, timeout=2)
    except Exception:
        pass


if __name__ == "__main__":
    main()
