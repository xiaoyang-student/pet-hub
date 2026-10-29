"""
universal_hook.py —— PetHub 通用 Hook（一个脚本接入所有 hooks 标准平台）

平台名识别顺序：
  1. 环境变量 PETHUB_PLATFORM（安装器注入 settings.json 时自动设置）
  2. 命令行第一个参数
  3. 兜底 "未知平台"

stdin 接收 hooks 标准事件 JSON（session_id / prompt / tool_name ...），
自动映射为 PetHub 任务事件。不依赖任何平台名单。
"""
import json
import os
import sys
import time
import urllib.request
# 本机回环绝不能被系统代理劫持：装了 Clash / V2Ray 的机器上，
# 127.0.0.1 的请求会被代理转走，返回 502，看起来就像"桥没开"。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

BRIDGE = "http://127.0.0.1:8765/api/events"
DUMP = Path.home() / ".pethub-hookdump.log"      # 事件留痕（排查用，可删）

# 这些工具名 = 平台在向用户提问 / 要授权 / 要拍板
ASK_TOOLS = {"askuserquestion", "exitplanmode", "requestpermission",
             "permissionrequest", "askquestion", "confirm", "userconfirmation"}
ASK_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog"}

STATUS_MAP = {"start": "running", "running": "running", "thinking": "thinking",
              "waiting": "waiting", "done": "done", "error": "error"}


def platform_name():
    env = os.environ.get("PETHUB_PLATFORM")
    if env:
        return env
    if len(sys.argv) > 2 and sys.argv[2].strip():
        return sys.argv[2]
    return "未知平台"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in STATUS_MAP:
        return
    status = STATUS_MAP[sys.argv[1]]
    try:
        raw = sys.stdin.read()
        ev = json.loads(raw) if raw.strip() else {}
    except Exception:
        ev = {}

    if not isinstance(ev, dict):
        return
    sess = str(ev.get("session_id") or ev.get("sessionId")
               or ev.get("conversation_id") or ev.get("conversationId")
               or ev.get("session") or "main")

    # ---- 事件留痕（默认关闭！会记录平台送来的原始内容，可能含隐私） ----
    # 排查时才开：设置环境变量 PETHUB_DUMP=1
    if os.environ.get("PETHUB_DUMP") == "1":
        try:
            if not DUMP.exists() or DUMP.stat().st_size < 400_000:
                with open(DUMP, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.time(), "argv": sys.argv[1:],
                                         "ev": ev}, ensure_ascii=False)[:2000] + "\n")
        except Exception:
            pass

    tool = str(ev.get("tool_name") or ev.get("toolName") or "")
    notification = str(ev.get("notification_type") or ev.get("notificationType") or "").lower()
    ask = status == "waiting" and (tool.lower() in ASK_TOOLS
                                    or notification in ASK_NOTIFICATIONS)
    # Notification 也包含空通知和完成通知，不能一律当成等待用户。
    if status == "waiting" and not ask:
        return

    title = (ev.get("title") or ev.get("prompt") or ev.get("tool_name")
             or "会话")
    if isinstance(title, (dict, list)):
        title = json.dumps(title, ensure_ascii=False)
    title = str(title)[:40]
    msg = ev.get("tool_input", "")
    if isinstance(msg, dict):
        msg = (msg.get("command") or msg.get("file_path")
               or msg.get("description") or "")
    body = {"platform": platform_name(), "task_id": sess,
            "title": title, "status": status, "message": str(msg)[:80],
            "ask": ask}
    try:
        req = urllib.request.Request(BRIDGE,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        _opener.open(req, timeout=2)
    except Exception:
        pass   # 桥未运行时静默，绝不影响宿主平台


if __name__ == "__main__":
    main()
