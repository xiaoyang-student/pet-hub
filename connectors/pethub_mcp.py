"""
pethub_mcp.py —— 把 PetHub 桌宠变成一个标准 MCP 工具服务（stdio，零依赖）

思路：不去猜应用在干什么，而是**让应用自己上报**——所以桌宠显示的每一条状态
都是应用亲口说的，不存在"猜出来"的假事件。

任何支持 MCP 的客户端都能调用（Qoder、WorkBuddy、Claude Desktop、Cursor、
Cherry Studio、自建 Agent……），在执行任务的过程中顺手调用一次即可：

  pethub_report  上报状态（running / thinking / waiting / done / error）
  pethub_ask     明确说"我在问用户，要授权/要拍板"（真实事件，不是推断）
  pethub_done    标记完成
  pethub_remove  移除任务
  pethub_state   查询桌宠当前显示的任务

注册方式（以 Qoder 为例，写入 ~/.qoder/mcp.json）：
  {"mcpServers": {"pethub": {"command": "python",
     "args": ["<PetHub 安装目录>/connectors/pethub_mcp.py"]}}}
"""
import json
import sys
import urllib.request
# 本机回环绝不能被系统代理劫持：装了 Clash / V2Ray 的机器上，
# 127.0.0.1 的请求会被代理转走，返回 502，看起来就像"桥没开"。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BRIDGE = "http://127.0.0.1:8765/api/events"
STATE_URL = "http://127.0.0.1:8765/api/state"
PROTO = "2024-11-05"

TOOLS = [
    {
        "name": "pethub_report",
        "description": "把当前任务状态实时推给桌面宠物（应用在干活时调用）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "来源平台名，如 Qoder / WorkBuddy / 豆包"},
                "title": {"type": "string", "description": "任务名，如「重构登录模块」"},
                "status": {"type": "string", "enum": ["running", "thinking", "waiting", "done", "error"],
                           "description": "running=执行中 thinking=思考中 waiting=等用户 done=完成 error=出错"},
                "progress": {"type": "number", "description": "进度 0-100，可省略"},
                "message": {"type": "string", "description": "一句话说明当前在做什么，可省略"},
                "task_id": {"type": "string", "description": "任务唯一 id，省略时按 title 归并"}
            },
            "required": ["platform", "title", "status"]
        }
    },
    {
        "name": "pethub_ask",
        "description": "【真实事件】应用正在向用户提问/请求授权/请用户拍板时调用，桌宠会喊人去处理",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "question": {"type": "string", "description": "要用户决定的问题，如「是否允许执行 rm -rf build/」"},
                "title": {"type": "string", "description": "任务名，可省略"},
                "task_id": {"type": "string", "description": "可省略"}
            },
            "required": ["platform", "question"]
        }
    },
    {
        "name": "pethub_done",
        "description": "任务完成，桌宠播报并让心情值上升",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "title": {"type": "string"},
                "task_id": {"type": "string"}
            },
            "required": ["platform", "title"]
        }
    },
    {
        "name": "pethub_remove",
        "description": "从桌宠任务列表移除一个任务",
        "inputSchema": {
            "type": "object",
            "properties": {"platform": {"type": "string"}, "task_id": {"type": "string"}},
            "required": ["platform", "task_id"]
        }
    },
    {
        "name": "pethub_state",
        "description": "查询桌宠当前显示的所有任务",
        "inputSchema": {"type": "object", "properties": {}}
    },
]


def post(ev):
    req = urllib.request.Request(BRIDGE, data=json.dumps(ev, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def tool_call(name, args):
    a = args or {}
    if name == "pethub_report":
        ev = {"platform": a["platform"], "task_id": a.get("task_id") or a["title"],
              "title": a["title"], "status": a["status"],
              "progress": a.get("progress"), "message": a.get("message", ""), "ask": False}
        post(ev)
        return {"ok": True, "sent": ev}

    if name == "pethub_ask":
        q = a["question"]
        ev = {"platform": a["platform"], "task_id": a.get("task_id") or a.get("title") or "ask",
              "title": a.get("title") or q[:24], "status": "waiting",
              "progress": None, "message": q, "ask": True}
        post(ev)
        return {"ok": True, "sent": ev}

    if name == "pethub_done":
        ev = {"platform": a["platform"], "task_id": a.get("task_id") or a["title"],
              "title": a["title"], "status": "done", "progress": 100,
              "message": a.get("message", ""), "ask": False}
        post(ev)
        return {"ok": True, "sent": ev}

    if name == "pethub_remove":
        post({"platform": a["platform"], "type": "remove", "task_id": a["task_id"]})
        return {"ok": True, "removed": a["task_id"]}

    if name == "pethub_state":
        with _opener.open(STATE_URL, timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))

    return {"ok": False, "error": "unknown tool: " + str(name)}


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        try:
            if method == "initialize":
                reply(mid, {"protocolVersion": PROTO,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "pethub", "version": "1.0"}})
            elif method == "tools/list":
                reply(mid, {"tools": TOOLS})
            elif method == "tools/call":
                p = msg.get("params") or {}
                res = tool_call(p.get("name"), p.get("arguments") or {})
                reply(mid, {"content": [{"type": "text",
                                         "text": json.dumps(res, ensure_ascii=False)}],
                            "isError": not res.get("ok", True)})
            elif method == "ping":
                reply(mid, {})
            elif method and method.startswith("notifications/"):
                pass                      # 通知类无需回复
            elif mid is not None:
                reply(mid, None, {"code": -32601, "message": "method not found: " + str(method)})
        except Exception as e:
            if mid is not None:
                reply(mid, None, {"code": -32000, "message": str(e)})


if __name__ == "__main__":
    main()
