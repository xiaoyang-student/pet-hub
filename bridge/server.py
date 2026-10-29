"""
PetHub Bridge —— 多平台 Agent 事件桥（纯标准库，零依赖）

连接器（任意平台） --POST /api/events--> 本服务 --SSE--> 桌面宠物界面

路由：
  POST /api/events   上报事件（支持单个事件或整批任务同步）
  GET  /api/state    当前任务快照（JSON）
  GET  /api/health   健康检查
  GET  /api/stream   SSE 实时事件流（页面订阅）
  GET  /             静态托管 app/index.html（可选）

事件协议（POST body）：
  单事件：
    {"platform":"WorkBuddy", "task_id":"abc", "title":"写周报",
     "status":"running|thinking|waiting|done|error", "progress":42, "message":"..."}
  整批同步（替换该平台同 src 的任务，适合日志轮询型连接器）：
    {"platform":"Codex", "type":"sync",
     "tasks":[{"task_id":"1","title":"重构模块","status":"running","progress":70}]}
  删除：
    {"platform":"Codex", "type":"remove", "task_id":"1"}

启动：  python server.py  (默认 127.0.0.1:8765)
"""
import json
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _split_top(s):
    """按顶层逗号切分，引号内的逗号不算。"""
    out, buf, q = [], [], ""
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
            buf.append(ch)
        elif ch == ",":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [x for x in out if x.strip()]


def _loose_json(text):
    """补回被命令行剥掉的引号。

    Windows PowerShell 5.1 把参数递给原生程序（curl.exe）时，会把双引号吃掉：
      -d "{\\"platform\\":\\"豆包\\"}"  -->  实际收到 {platform:豆包}
    这时 json.loads 报 "Expecting property name enclosed in double quotes"，
    用户看到的却是"接口坏了"。这里在严格解析失败后兜底，
    只处理扁平的 key:value（嵌套的 sync 整批仍须是合法 JSON）。
    """
    s = (text or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    inner = s[1:-1].strip()
    if not inner:
        return {}
    if "[" in inner or "{" in inner:
        return None                     # 有嵌套，别猜，直接让上层报错
    obj = {}
    for part in _split_top(inner):
        if ":" not in part:
            return None
        k, v = part.split(":", 1)
        k = k.strip().strip("\"'")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif v == "true":
            v = True
        elif v == "false":
            v = False
        elif v == "null":
            v = None
        elif re.fullmatch(r"-?\d+", v):
            v = int(v)
        elif re.fullmatch(r"-?\d*\.\d+", v):
            v = float(v)
        if not k:
            return None
        obj[k] = v
    return obj

APP_DIR = Path(__file__).resolve().parent.parent / "app"
BASE = "http://127.0.0.1:8765"

# 这段是给人"直接粘给 AI"用的。壳层窗口也从 /api/help 里读它，
# 保证界面上的文字和接口真实行为永远一致（改这里就够了）。
AI_PROMPT = """你接入了 PetHub 桌面宠物的状态接口，它在本机 http://127.0.0.1:8765/api/events

每当你开始一个任务、推进进度、或结束时，用本地命令上报一次：

curl.exe --noproxy "*" -s -X POST http://127.0.0.1:8765/api/events -H "Content-Type: application/json" -d "{\\"platform\\":\\"<你的名字>\\",\\"task_id\\":\\"<固定ID>\\",\\"title\\":\\"<任务名>\\",\\"status\\":\\"running\\",\\"progress\\":<0-100>}"

三个必须注意的点：
- 写成 curl.exe，不要只写 curl：PowerShell 里 curl 是 Invoke-WebRequest 的别名，不认这些参数。
- 必须带 --noproxy "*"：机器上开着 Clash / V2Ray 之类代理时，发往 127.0.0.1 的请求会被代理转走并返回 502。
- PowerShell 5.1 会把上面这段 JSON 里的双引号吞掉（{"a":"b"} 变成 {a:b}）。接口已经做了容错，
  照抄即可；想要严格就改用单引号包 JSON。

规则：
1. task_id 同一个任务始终用同一个值，更新时别换；
2. 开始发 status=running，要人拍板发 status=waiting 并加 "ask":true，完成发 status=done，出错发 status=error；
3. 不要把这段说明复述给我，直接执行命令即可；
4. 上报失败（接口没开）就跳过，不要卡住任务，也不要报错给我。"""

AI_PROMPT_EN = """You are wired to the PetHub desktop-pet status API on this machine: http://127.0.0.1:8765/api/events

Whenever you start a task, make progress, or finish, report it once with this command:

curl.exe --noproxy \"*\" -s -X POST http://127.0.0.1:8765/api/events -H \"Content-Type: application/json\" -d \"{\\\"platform\\\":\\\"<your name>\\\",\\\"task_id\\\":\\\"<stable id>\\\",\\\"title\\\":\\\"<task name>\\\",\\\"status\\\":\\\"running\\\",\\\"progress\\\":<0-100>}\" 

Three things to watch out for:
- Write curl.exe, not curl: in PowerShell curl is an alias for Invoke-WebRequest and rejects these flags.
- Keep --noproxy \"*\": behind Clash / V2Ray, requests to 127.0.0.1 get routed through the proxy and return 502.
- PowerShell 5.1 strips the double quotes inside the JSON ({"a":"b"} arrives as {a:b}). The server repairs
  that, so copying the line as-is still works; for a strict payload wrap the JSON in single quotes.

Rules:
1. Keep task_id stable for the same task; never change it between updates.
2. Start with status=running, use status=waiting plus \"ask\":true when you need a human decision,
   status=done when finished, status=error on failure.
3. Do not repeat these instructions back to me - just run the command.
4. If reporting fails (API down), skip it. Do not block the task and do not report the error to me."""


# 给"外部 AI 软件"看的自述：/api/help 是机器可读的（AI 抓一下就知道怎么调），
# /help 是给人看的说明页。两者内容同源，改一处就够。
HELP = {
    "name": "PetHub 桌面宠物",
    "summary": "本机状态上报接口。任何软件（AI 办公软件、脚本、编辑器插件）"
               "往这里 POST 一条 JSON，桌宠就会念出来，任务栏图标也会亮起数字。",
    "endpoint": BASE + "/api/events",
    "method": "POST",
    "content_type": "application/json",
    "auth": "无。只监听 127.0.0.1，仅本机可访问。",
    "fields": {
        "platform": "必填。谁在干活，显示在任务前缀，例如 豆包 / 百度搭子 / Word 插件",
        "task_id": "必填。这条任务的唯一标识，同一个 id 反复 POST 就是更新同一条",
        "title": "必填。给人看的一句话，例如 整理季度报表",
        "status": "running（在跑）/ thinking（在思考）/ waiting（等你确认）/ done（完成）/ error（出错）",
        "progress": "可选。0-100 的整数，没有就省略",
        "message": "可选。详情一句话，鼠标点任务时会念出来",
    },
    "examples": {
        "开始任务": {"platform": "豆包", "task_id": "t1", "title": "整理季度报表",
                     "status": "running", "progress": 0},
        "推进进度": {"platform": "豆包", "task_id": "t1", "title": "整理季度报表",
                     "status": "running", "progress": 45},
        "等用户确认": {"platform": "豆包", "task_id": "t1", "title": "要覆盖原文件吗",
                       "status": "waiting", "ask": True},
        "完成": {"platform": "豆包", "task_id": "t1", "title": "整理季度报表",
                 "status": "done", "progress": 100},
        "整批同步": {"platform": "Codex", "type": "sync", "tasks": [
            {"task_id": "1", "title": "重构模块", "status": "running", "progress": 70}]},
        "删除任务": {"platform": "豆包", "type": "remove", "task_id": "t1"},
    },
    "other_endpoints": {
        "GET " + BASE + "/api/state": "当前所有任务的快照（JSON）",
        "GET " + BASE + "/api/health": "健康检查，返回 {ok:true}",
        "GET " + BASE + "/api/stream": "SSE 实时事件流",
        "GET " + BASE + "/help": "这份说明（网页版）",
    },
    "rules": [
        "任务 id 固定，不要在每次更新时换 id，否则会变成多条任务。",
        "完成了一定要发 done，否则托盘上的数字会一直留着。",
        "等待用户确认用 status=waiting + ask=true，桌宠会主动开口提示。",
        "进程崩溃前没发 done 也没关系：30 分钟没有任何新事件会自动移除。",
        "进度只能你给，宠物猜不到。不想给就省略 progress。",
        '开着代理（Clash / V2Ray）时命令要带 --noproxy "*"，否则 127.0.0.1 会被代理转成 502。',
        "PowerShell 里要用 curl.exe；只写 curl 会命中 Invoke-WebRequest 别名，参数全报错。",
        "PowerShell 5.1 会吞掉 JSON 里的双引号（{\"a\":\"b\"} 变成 {a:b}）；"
        "服务端已容错，照抄上面的命令也能通。想要严格就改用单引号包 JSON。",
    ],
    "curl_example": (
        'curl.exe --noproxy "*" -s -X POST ' + BASE + '/api/events '
        '-H "Content-Type: application/json" '
        '-d "{\\"platform\\":\\"豆包\\",\\"task_id\\":\\"t1\\",'
        '\\"title\\":\\"整理季度报表\\",\\"status\\":\\"running\\",\\"progress\\":40}"'
    ),
    "ai_prompt": AI_PROMPT,
    "ai_prompt_en": AI_PROMPT_EN,
    # 用 json.dumps 生成，省得手写 \" 转义（写错一次就变成非法 JSON）
    "curl_example_en": (
        'curl.exe --noproxy "*" -s -X POST ' + BASE + '/api/events '
        '-H "Content-Type: application/json" -d ' +
        json.dumps(json.dumps({"platform": "Doubao", "task_id": "t1", "title": "Weekly report",
                               "status": "running", "progress": 40},
                              ensure_ascii=False, separators=(",", ":")))
    ),
}

HELP_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>PetHub · 接入说明</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:40px 24px 60px;background:#0f1319;color:#e6edf6;
 font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:820px;margin:0 auto}
h1{margin:0 0 6px;font-size:26px;letter-spacing:.5px}
.sub{color:#8d9bb0;margin:0 0 28px}
h2{margin:34px 0 12px;font-size:16px;color:#9ecbff;font-weight:600}
p{margin:10px 0;color:#c3cedd}
code{background:#1b2230;border:1px solid #26303f;border-radius:4px;
 padding:1px 6px;font-family:Consolas,monospace;font-size:13px;color:#7fd6a8}
pre{background:#141a24;border:1px solid #26303f;border-left:3px solid #4f9cf9;
 border-radius:6px;padding:14px 16px;overflow:auto;margin:12px 0}
pre code{background:none;border:0;padding:0;color:#cfe4ff;
 white-space:pre-wrap;word-break:break-word;font-size:13px}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #222b39;vertical-align:top}
th{color:#8d9bb0;font-weight:500;width:120px}
td code{color:#ffd68a}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
 background:#41d68a;margin-right:8px;box-shadow:0 0 8px #41d68a}
.langbtn{position:fixed;top:18px;right:22px;background:#1b2230;border:1px solid #26303f;
 border-radius:6px;padding:5px 12px;color:#9ecbff;text-decoration:none;font-size:13px}
.langbtn:hover{border-color:#4f9cf9}
</style></head><body><a class="langbtn" href="__LANGSWITCH__">__LANGSWITCHTEXT__</a><div class="wrap">
<h1>PetHub · 接入说明</h1>
<p class="sub"><span class="dot"></span>本机接口在线 · __ENDPOINT__</p>
<p>任何软件往这个地址 POST 一条 JSON，桌宠就会念出来，任务栏图标也会亮起数字。
不用装插件，不用鉴权，只监听本机 127.0.0.1。</p>

<h2>一个接口就够</h2>
<table>
<tr><th>地址</th><td><code>__ENDPOINT__</code></td></tr>
<tr><th>方法</th><td><code>POST</code> &nbsp; <code>Content-Type: application/json</code></td></tr>
</table>
<pre><code>__CURL__</code></pre>
<p><b>三个坑</b>：① 写 <code>curl.exe</code> 而不是 <code>curl</code>——PowerShell 里 curl 是
Invoke-WebRequest 的别名，参数全不认；② 带 <code>--noproxy "*"</code>——开着 Clash / V2Ray
这类代理时，发往 127.0.0.1 的请求会被代理转走并返回 502，看着就像接口挂了；
③ PowerShell 5.1 会把 JSON 里的双引号吞掉（<code>{"a":"b"}</code> 变成 <code>{a:b}</code>）——
服务端已做容错，照抄上面的命令也能通；想严格一点就用单引号包 JSON。</p>

<h2>字段</h2>
<table>
<tr><th>platform</th><td>必填 · 谁在干活（<code>豆包</code> / <code>百度搭子</code> / <code>Word 插件</code>）</td></tr>
<tr><th>task_id</th><td>必填 · 任务唯一标识。同一个 id 反复 POST 就是更新同一条</td></tr>
<tr><th>title</th><td>必填 · 一句话任务名，<code>整理季度报表</code></td></tr>
<tr><th>status</th><td><code>running</code> 在跑 · <code>thinking</code> 在思考 ·
 <code>waiting</code> 等你确认 · <code>done</code> 完成 · <code>error</code> 出错</td></tr>
<tr><th>progress</th><td>可选 · 0-100 整数。不给也能用，但托盘上的百分比只能你自己报</td></tr>
<tr><th>message</th><td>可选 · 详情，点任务时念出来</td></tr>
</table>

<h2>给 AI 软件的提示词（直接粘进去）</h2>
<p>把下面这段发给豆包 / 百度搭子 / 任何能执行命令的 AI，它就会在干活时自动上报：</p>
<pre><code>__PROMPT__</code></pre>

<h2>支持 MCP 的客户端</h2>
<p>Claude Desktop、Cursor、Cline、Cherry Studio 这类能装 MCP 的，配一行就行：</p>
<pre><code>{"mcpServers":{"pethub":{"command":"python","args":["__MCP__"]}}}</code></pre>

<h2>约定</h2>
<ul>
<li>task_id 固定，别每次更新换 id，否则会变成好几条任务</li>
<li>完成了一定要发 <code>done</code>，否则托盘数字会一直留着</li>
<li>要用户拍板用 <code>status=waiting</code> + <code>ask=true</code>，桌宠会主动开口</li>
<li>崩溃了没发 done 也没事：30 分钟没新事件自动移除</li>
</ul>
</div></body></html>"""


HELP_HTML_EN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>PetHub · Integration Guide</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:40px 24px 60px;background:#0f1319;color:#e6edf6;
 font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:820px;margin:0 auto}
h1{margin:0 0 6px;font-size:26px;letter-spacing:.5px}
.sub{color:#8d9bb0;margin:0 0 28px}
h2{margin:34px 0 12px;font-size:16px;color:#9ecbff;font-weight:600}
p{margin:10px 0;color:#c3cedd}
code{background:#1b2230;border:1px solid #26303f;border-radius:4px;
 padding:1px 6px;font-family:Consolas,monospace;font-size:13px;color:#7fd6a8}
pre{background:#141a24;border:1px solid #26303f;border-left:3px solid #4f9cf9;
 border-radius:6px;padding:14px 16px;overflow:auto;margin:12px 0}
pre code{background:none;border:0;padding:0;color:#cfe4ff;
 white-space:pre-wrap;word-break:break-word;font-size:13px}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #222b39;vertical-align:top}
th{color:#8d9bb0;font-weight:500;width:120px}
td code{color:#ffd68a}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
 background:#41d68a;margin-right:8px;box-shadow:0 0 8px #41d68a}
.langbtn{position:fixed;top:18px;right:22px;background:#1b2230;border:1px solid #26303f;
 border-radius:6px;padding:5px 12px;color:#9ecbff;text-decoration:none;font-size:13px}
.langbtn:hover{border-color:#4f9cf9}
</style></head><body><a class="langbtn" href="__LANGSWITCH__">__LANGSWITCHTEXT__</a><div class="wrap">
<h1>PetHub · Integration Guide</h1>
<p class="sub"><span class="dot"></span>Local API online · __ENDPOINT__</p>
<p>POST one JSON event to this address and the desktop pet will announce it, with a badge
count on the tray icon. No plugin, no auth — the server only listens on 127.0.0.1.</p>

<h2>One endpoint is all you need</h2>
<table>
<tr><th>Endpoint</th><td><code>__ENDPOINT__</code></td></tr>
<tr><th>Method</th><td><code>POST</code> &nbsp; <code>Content-Type: application/json</code></td></tr>
</table>
<pre><code>__CURL__</code></pre>
<p><b>Three gotchas</b>: ① use <code>curl.exe</code>, not <code>curl</code> — in PowerShell,
curl is an alias for Invoke-WebRequest and the flags won't parse; ② keep
<code>--noproxy "*"</code> — with Clash / V2Ray style proxies running, requests to 127.0.0.1
get routed through the proxy and come back 502, which looks like the API is down; ③
PowerShell 5.1 strips the double quotes inside JSON (<code>{"a":"b"}</code> becomes
<code>{a:b}</code>) — the server repairs that, so copying the command as-is still works;
to be strict, wrap the JSON in single quotes.</p>

<h2>Fields</h2>
<table>
<tr><th>platform</th><td>Required · who is working (<code>Doubao</code> / <code>Word plugin</code> / <code>CI bot</code>)</td></tr>
<tr><th>task_id</th><td>Required · unique task id. POSTing the same id again updates that task</td></tr>
<tr><th>title</th><td>Required · one-line task name, e.g. <code>Quarterly report</code></td></tr>
<tr><th>status</th><td><code>running</code> working · <code>thinking</code> thinking ·
 <code>waiting</code> needs your confirmation · <code>done</code> finished · <code>error</code> failed</td></tr>
<tr><th>progress</th><td>Optional · integer 0-100. Omit it and things still work, but the tray
percentage is on you</td></tr>
<tr><th>message</th><td>Optional · details, spoken when the task is clicked</td></tr>
</table>

<h2>Prompt for AI apps (paste it in)</h2>
<p>Send the block below to Doubao / any AI that can run commands, and it will report
progress automatically while working:</p>
<pre><code>__PROMPT__</code></pre>

<h2>MCP-capable clients</h2>
<p>For Claude Desktop, Cursor, Cline, Cherry Studio and anything else that runs MCP
servers, one entry is enough:</p>
<pre><code>{"mcpServers":{"pethub":{"command":"python","args":["__MCP__"]}}}</code></pre>

<h2>Conventions</h2>
<ul>
<li>Keep task_id stable — changing the id on every update splits one task into many</li>
<li>Always send <code>done</code> when finished, or the tray badge will linger</li>
<li>For a human decision use <code>status=waiting</code> + <code>ask=true</code>; the pet will speak up</li>
<li>Crashed without done? Fine — tasks with no events for 30 minutes are removed automatically</li>
</ul>
</div></body></html>"""

def help_html(lang="zh"):
    """把真机路径填进模板（MCP 脚本路径每台机器不一样）；lang 选模板与提示词。"""
    mcp = Path(__file__).resolve().parent.parent / "connectors" / "pethub_mcp.py"
    mcp = str(mcp).replace("\\", "\\\\")
    if lang == "en":
        return (HELP_HTML_EN
                .replace("__ENDPOINT__", HELP["endpoint"])
                .replace("__CURL__", HELP["curl_example_en"])
                .replace("__PROMPT__", AI_PROMPT_EN)
                .replace("__MCP__", mcp)
                .replace("__LANGSWITCH__", "?lang=zh")
                .replace("__LANGSWITCHTEXT__", "中文"))
    return (HELP_HTML
            .replace("__ENDPOINT__", HELP["endpoint"])
            .replace("__CURL__", HELP["curl_example"])
            .replace("__PROMPT__", AI_PROMPT)
            .replace("__MCP__", mcp)
            .replace("__LANGSWITCH__", "?lang=en")
            .replace("__LANGSWITCHTEXT__", "EN"))


STATE = {}          # key -> task dict
LOCK = threading.Lock()        # 只保护 STATE
COMMIT_LOCK = threading.Lock()
SUBS = []
SUBS_LOCK = threading.Lock()
SSE_QUEUE_SIZE = 64
SSE_MAX_CONNECTIONS = 32
SSE_WRITE_TIMEOUT = 5.0
HEARTBEAT = 15
TTL = 1800          # 任务保鲜期（秒）：30 分钟无任何新事件即视为失联并移除
TERMINAL_TTL = 60   # 已完成/已失败的一行只再留 60 秒，不能当成任务一直挂着
PRUNE_SEC = 15


class Subscription:
    def __init__(self):
        self.queue = queue.Queue(maxsize=SSE_QUEUE_SIZE)
        self.closed = threading.Event()


def subscribe():
    # 快照与登记共用提交锁，避免夹入旧事件或漏掉新事件。
    with COMMIT_LOCK:
        with SUBS_LOCK:
            if len(SUBS) >= SSE_MAX_CONNECTIONS:
                return None, None
        snap = snapshot()
        sub = Subscription()
        with SUBS_LOCK:
            SUBS.append(sub)
        return sub, snap


def unsubscribe(sub):
    with SUBS_LOCK:
        sub.closed.set()
        if sub in SUBS:
            SUBS.remove(sub)


def snapshot():
    with LOCK:
        return {"tasks": sorted((dict(t) for t in STATE.values()),
                                key=lambda t: t["ts"]),
                "updated": time.time()}


def broadcast(event: dict):
    with SUBS_LOCK:
        for sub in SUBS:
            if sub.closed.is_set():
                continue
            try:
                sub.queue.put_nowait(event)
            except queue.Full:
                # 溢出后断流重连取快照；写超时前仍占连接名额。
                sub.closed.set()


APPWATCH = "应用接入"


def _app_base(task):
    """应用接入任务的 task_id 形如 app:qoder.exe -> "qoder"。"""
    tid = str(task.get("id") or "")
    if tid.lower().startswith("app:"):
        tid = tid[4:]
    return tid.rsplit(".", 1)[0].strip().lower()


def _native_platforms():
    """除"应用接入"以外、正在上报的平台名（小写）。**需持锁调用。**"""
    return {str(t["platform"]).strip().lower()
            for t in STATE.values() if t["platform"] != APPWATCH}


def _covered_by_native(base, owners=None):
    """这个软件是不是已经自己上报了？workbuddy.exe ↔ workbuddy。

    只做双向包含匹配：qoder ↔ qoder.exe 命中；豆包 ↔ doubao 不命中。
    """
    if not base:
        return False
    owners = _native_platforms() if owners is None else owners
    return any(o and (base in o or o in base) for o in owners)


def _drop_appwatch_dupes(outbox):
    """同一个软件既有粗粒度「应用接入」，又有它自己上报的精确任务时，只留精确那条。

    appwatch 会自动盯 KNOWN 里的 AI 客户端（Qoder / WorkBuddy / Cursor…），
    但这些软件本来就有 hook 或 MCP 在报，于是同一个活会出现两行。
    这里按"软件名"把粗的那条收掉，无论谁先到都能收敛。

    **需持锁调用**，且不直接广播——把事件塞进 outbox，由 apply_event
    在释放锁之后统一发出。
    """
    owners = _native_platforms()
    if not owners:
        return
    drop = [k for k, t in list(STATE.items())
            if t["platform"] == APPWATCH and _covered_by_native(_app_base(t), owners)]
    for k in drop:
        STATE.pop(k, None)
        outbox.append({"type": "removed", "key": k, "platform": APPWATCH})


def _trace_row_for(lname, tid, overview=False):
    for k, t in STATE.items():
        if (str(t.get("platform") or "").strip().lower() == lname
                and t.get("src") == "trace"
                and (t.get("id") == tid or (overview and tid == "main"))):
            return k
    return None


def _drop_trace_dupes(platform, tid, key, outbox):
    lname = str(platform).strip().lower()
    # Legacy hooks without a session use main; preserve their unresolved alerts.
    drop = [k for k, t in STATE.items()
            if k != key and str(t.get("platform") or "").strip().lower() == lname
            and t.get("src") != "trace" and (t.get("id") == tid or
                (t.get("id") == "main" and not t.get("src")
                 and t.get("status") not in ("waiting", "error") and not t.get("ask")))]
    for k in drop:
        STATE.pop(k, None)
        outbox.append({"type": "removed", "key": k})


def _mk_task(platform, tid, d, src, now):
    """统一构造任务字典。单事件与 sync 整批共用，免得两边字段对不上。"""
    return {
        "platform": platform,
        "id": tid,
        "title": d.get("title") or tid or "未命名任务",
        "status": d.get("status", "running"),
        "progress": d.get("progress"),
        "message": d.get("message", ""),
        "ask": bool(d.get("ask")),
        # phase：细粒度阶段（received / thinking / tool / asking / denied /
        # answering / done / stalled / error）。桥只认五个 status，更细的
        # 语义走这个字段，由状态栏渲染成徽标。可选，老客户端忽略即可。
        "phase": str(d.get("phase") or "")[:24],
        "tool": str(d.get("tool") or "")[:32],
        "turn": str(d.get("turn") or "")[:200],
        "terminal": bool(d.get("terminal")),
        "src": src or "",
        "ts": now,
    }


def apply_event(ev: dict):
    # 串行提交和入队，但不持状态锁做广播，也不持任何锁做网络 IO。
    with COMMIT_LOCK:
        return _apply_event(ev)


def _apply_event(ev: dict):
    ev = dict(ev)
    platform = ev.get("platform") or "unknown"
    etype = ev.get("type", "update")
    now = time.time()
    outbox = []
    note = None

    with LOCK:
        # 已经有精确来源在报这个软件了，应用接入就别再插一条
        if platform == APPWATCH and etype != "sync":
            base = _app_base({"id": ev.get("task_id") or ev.get("id") or ""})
            if _covered_by_native(base):
                return "dup-of-native"

        if etype == "sync":
            tasks = ev.get("tasks") or []
            keep = set()
            src = str(ev.get("src") or "")
            lname = str(platform).strip().lower()
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                tid = str(t.get("task_id") or t.get("id") or t.get("title", ""))
                key = f"{platform}::{tid}"
                keep.add(key)
                task = _mk_task(platform, tid, t, src, now)
                # 只有 done/error 冻结时间戳：它们不再变化，靠 TTL 自然退场。
                # waiting **必须**继续刷新 —— 一个真在等用户拍板的任务可能
                # 等上半小时，冻结 ts 会让它被 TTL 当成"失联"删掉，
                # 而那恰恰是最该留在状态栏上的一行。
                if task["status"] in ("done", "error"):
                    task["ts"] = STATE.get(key, {}).get("ts", now)
                trace_key = _trace_row_for(lname, tid, not src and
                    task["status"] not in ("waiting", "error") and not task["ask"]) if src != "trace" else None
                if trace_key and task["status"] not in ("waiting", "error"):
                    if key != trace_key and STATE.get(key, {}).get("src", "") == src:
                        if STATE.pop(key, None) is not None:
                            outbox.append({"type": "removed", "key": key})
                    continue
                if src == "trace":
                    _drop_trace_dupes(platform, tid, key, outbox)
                elif trace_key and trace_key != key:
                    STATE.pop(trace_key, None)
                    outbox.append({"type": "removed", "key": trace_key})
                STATE[key] = task
            # 空快照只能清理自己的来源，不能撤销其他连接器的观察。
            for k, t in list(STATE.items()):
                if t["platform"] != platform or t.get("src", "") != src or k in keep:
                    continue
                STATE.pop(k, None)
                outbox.append({"type": "removed", "key": k})
            _drop_appwatch_dupes(outbox)
            outbox.append({"type": "snapshot",
                           "tasks": sorted((dict(t) for t in STATE.values()),
                                           key=lambda t: t["ts"])})
        else:
            tid = str(ev.get("task_id") or ev.get("id") or ev.get("title", ""))
            key = f"{platform}::{tid}"

            if etype == "remove":
                STATE.pop(key, None)
                outbox.append({"type": "removed", "key": key,
                               "platform": platform})
                _drop_appwatch_dupes(outbox)
            else:
                status = ev.get("status", "running")
                src = str(ev.get("src") or "")
                lname = str(platform).strip().lower()
                trace_key = _trace_row_for(lname, tid, not src and
                    status not in ("waiting", "error") and not ev.get("ask"))
                if src == "trace":
                    _drop_trace_dupes(platform, tid, key, outbox)
                elif trace_key and status not in ("waiting", "error"):
                    if key != trace_key and STATE.get(key, {}).get("src", "") == src:
                        if STATE.pop(key, None) is not None:
                            outbox.append({"type": "removed", "key": key})
                    note = "dup-of-trace"
                elif trace_key and trace_key != key:
                    STATE.pop(trace_key, None)
                    outbox.append({"type": "removed", "key": trace_key})

                if note is None:
                    task = _mk_task(platform, tid, ev, src, now)
                    # src 粘住：hook 补发的更新不该把已有的来源标记洗掉
                    task["src"] = src or str(STATE.get(key, {}).get("src") or "")
                    # 完成/失败的时间戳同样冻结，重复上报不能无限推迟退场。
                    if task["status"] in ("done", "error"):
                        task["ts"] = STATE.get(key, {}).get("ts", now)
                    STATE[key] = task
                    outbox.append({"type": "task", "task": dict(task)})
                    _drop_appwatch_dupes(outbox)

    for e in outbox:
        broadcast(e)
    return note


# ---------------------------------------------------------------- 来源校验
# "只监听 127.0.0.1"不等于安全：浏览器里的任意网页都能 fetch 到本机端口，
# 而桌宠是一个全屏、透明、永远置顶的 WebView2，它渲染的内容直接来自这个
# 接口。旧实现对所有响应回 Access-Control-Allow-Origin: *，等于把这个
# 渲染面开放给了用户浏览的任何网站。
#
# 这里做两件事：
#   1. CORS 不再用 *，只回显白名单内的 Origin；
#   2. POST /api/events 直接拒绝白名单外的 Origin。
# 第 2 条才是真正的闸门——CORS 本身拦不住跨域"简单请求"被发出去，
# 浏览器只是不让读响应，而桌宠的副作用（显示什么）发生在服务端。
#
# 原生客户端（curl.exe / python 连接器 / MCP）不带 Origin 头，一律放行，
# README 里所有接入方式都不受影响。
ALLOWED_ORIGINS = frozenset({
    "null",                       # WebView2 以 file:// 加载的桌宠页面
    "file://",
    "http://127.0.0.1:8765",      # 浏览器直接打开 / 或 /help 页
    "http://localhost:8765",
})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 安静模式

    # -- 来源 ------------------------------------------------------
    def _origin(self):
        return (self.headers.get("Origin") or "").strip()

    def _origin_allowed(self):
        o = self._origin()
        if not o:
            return True                     # 非浏览器客户端（curl / python / MCP）
        if o in ALLOWED_ORIGINS:
            return True
        # 浏览器还会带 Sec-Fetch-Site，同源/无来源的导航放行
        return (self.headers.get("Sec-Fetch-Site") or "").lower() \
            in ("same-origin", "same-site", "none")

    def _cors(self):
        o = self._origin()
        if o in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not self._origin_allowed():
            self._json({"ok": False, "error": "origin not allowed"}, 403)
            return
        self._json({"ok": True})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/state":
            self._json(snapshot())
        elif p == "/api/health":
            self._json({"ok": True, "tasks": len(STATE), "ts": time.time()})
        elif p == "/api/help":
            self._json(HELP)
        elif p == "/help":
            # ?lang= 优先；其次看浏览器 Accept-Language；都没有默认中文
            al = self.headers.get("Accept-Language") or ""
            lang = ("en" if "lang=en" in self.path else
                    "zh" if "lang=zh" in self.path
                    else ("zh" if ("zh" in al or not al.strip()) else "en"))
            body = help_html(lang).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/stream":
            self._sse()
        elif p == "/":
            f = APP_DIR / "index.html"
            if f.exists():
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"ok": False, "error": "app/index.html 不存在"})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _sse(self):
        self.connection.settimeout(SSE_WRITE_TIMEOUT)
        self.close_connection = True
        sub, initial = subscribe()
        if sub is None:
            self._json({"ok": False, "error": "too many streams"}, 503)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            snap = json.dumps(initial, ensure_ascii=False)
            initial = None
            self.wfile.write(f"event: snapshot\ndata: {snap}\n\n".encode("utf-8"))
            self.wfile.flush()
            snap = None
            last_beat = time.monotonic()
            while not sub.closed.is_set():
                try:
                    ev = sub.queue.get(timeout=1.0)
                    if sub.closed.is_set():
                        break
                    data = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    ev = data = None
                except queue.Empty:
                    if time.monotonic() - last_beat > HEARTBEAT:
                        self.wfile.write(b": ping\n\n")
                        last_beat = time.monotonic()
                self.wfile.flush()
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        finally:
            unsubscribe(sub)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p != "/api/events":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        # 这道闸比 CORS 头重要：跨域"简单请求"照样能发出去，
        # 浏览器只是不让网页读响应——而桌宠的副作用发生在服务端。
        if not self._origin_allowed():
            self._json({"ok": False, "error": "origin not allowed"}, 403)
            return
        raw = ""
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8", "replace")
            ev = json.loads(raw)
        except Exception as e:
            ev = _loose_json(raw)           # PowerShell 剥引号的兜底
            if ev is None:
                self._json({"ok": False, "error": f"bad json: {e}"}, 400)
                return
        if not isinstance(ev, dict):
            self._json({"ok": False, "error": "body must be json object"}, 400)
            return
        note = apply_event(ev)
        self._json({"ok": True, "note": note} if note else {"ok": True})


def prune_once(now=None):
    now = time.time() if now is None else now
    with COMMIT_LOCK:
        with LOCK:
            dead = []
            for k, t in STATE.items():
                age = now - t.get("ts", now)
                limit = TERMINAL_TTL if t.get("status") in ("done", "error") else TTL
                if age > limit:
                    dead.append(k)
            for k in dead:
                STATE.pop(k, None)
        for k in dead:
            broadcast({"type": "removed", "key": k})


def prune_loop():
    """任务保鲜：完成/失败 60 秒退场；长时间无事件的按 30 分钟失联移除。

    这是"不说谎"的底线——应用可能崩溃/被关闭而永远不会发来 done，
    与其让桌宠一直显示"还在跑"，不如当作失联处理。反过来，已经
    done/error 的行也不该继续当成一个活任务留在托盘里。
    """
    while True:
        time.sleep(PRUNE_SEC)
        try:
            prune_once()
        except Exception:
            pass


def main():
    host, port = "127.0.0.1", 8765
    threading.Thread(target=prune_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"PetHub Bridge 运行中  http://{host}:{port}")
    print(f"  事件上报  POST http://{host}:{port}/api/events")
    print(f"  状态快照  GET  http://{host}:{port}/api/state")
    print("Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
