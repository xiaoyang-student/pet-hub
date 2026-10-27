"""
appwatch.py —— PetHub 通用应用观察器（"导入任意软件"的运行端）

和 observer.py 的区别：
  observer.py 猜的是"哪些进程像在干活"（python/node/git…），噪声大、分不清是哪个软件；
  本文件只盯**用户明确导入的那几个应用**，用「CPU 活动 + 窗口标题变化」判断它是不是在处理任务，
  所以对任何软件都成立，不需要那个软件配合、不需要它支持 hook、不需要联网。

配置：%LOCALAPPDATA%\\PetHub\\apps.json
  {"watch":[{"name":"Qoder","exe":"Qoder.exe","enabled":true}]}
  这个文件由托盘菜单「接入应用 → 导入应用…」写入，本进程每 2 秒检查一次改动，无需重启。

产出：向 bridge（127.0.0.1:8765）推送任务，platform = "应用接入"
  task_id = "app:<exe>"；标题 = 「应用名 · 当前窗口标题」
  忙 → running；安静 8 秒 → done；进程退出 → 直接移除

用法：
  pythonw appwatch.py            # 常驻（壳层启动时自动拉起）
  python  appwatch.py --once     # 只跑一轮，打印判定结果（排查用）
  python  appwatch.py --list     # 打印当前配置
零第三方依赖：只用 ctypes 调 Win32。
"""
import ctypes
import ctypes.wintypes as wt
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
PLATFORM = "应用接入"
POLL = 2.0                 # 轮询间隔（秒）

# 判定阈值。经验值：
#   CPU 增量 > 0.30 秒 / 2 秒 = 真的在算（大于 15% 单核）
#   标题变了                     = 在换活干（哪怕 CPU 不高，比如等接口回包）
CPU_BUSY = 0.30
QUIET_DONE = 22.0          # 安静这么久才算这一轮干完（AI 应用等接口时 CPU 是闲的，给足余量）
DONE_REMOVE = 60.0         # done 之后留这么久再从面板上撤掉

# 首次运行自动纳入观察的 AI 客户端（只认这些，别把 VS Code 当成"在跑任务"）
KNOWN = {
    "qoder.exe": "Qoder",
    "cursor.exe": "Cursor",
    "trae.exe": "Trae",
    "windsurf.exe": "Windsurf",
    "claude.exe": "Claude",
    "chatgpt.exe": "ChatGPT",
    "codex.exe": "Codex",
    "workbuddy.exe": "WorkBuddy",
    "kiro.exe": "Kiro",
    "void.exe": "Void",
    "zed.exe": "Zed",
    "aider.exe": "Aider",
    "lmstudio.exe": "LM Studio",
    "ollama.exe": "Ollama",
}

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]


def list_procs():
    """{exe 小写名: [pid, ...]}"""
    out = {}
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == 0xFFFFFFFF:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            name = e.szExeFile.lower()
            out.setdefault(name, []).append(int(e.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def proc_times(pid):
    """(累计 CPU 秒数, 进程启动时刻)；拿不到返回 (None, None)"""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None, None
    try:
        c, e, k, u = (wt.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            return None, None
        to_i = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        cpu = (to_i(k) + to_i(u)) / 1e7
        started = to_i(c) / 1e7 - 11644473600      # FILETIME -> Unix 时间
        return cpu, started
    finally:
        k32.CloseHandle(h)


# 这些窗口的标题不是"在干什么"，是系统壳/输入法/桌面自己在用的
JUNK_TITLE = {"program manager", "default ime", "msctfime ui",
              "windows input experience", "popuphost", "gdi+ window",
              "nvidia geforce overlay", "clock", "textinputhost"}
JUNK_CLASS = {"progman", "workerw", "shell_traywnd", "shelldll_defview",
              "window", "ime", "msctfimeuihwnd"}


def window_titles():
    """{pid: 该进程最长的可见窗口标题}"""
    res = {}
    buf = ctypes.create_unicode_buffer(512)
    cbuf = ctypes.create_unicode_buffer(128)

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        u32.GetClassNameW(hwnd, cbuf, 128)
        if cbuf.value.strip().lower() in JUNK_CLASS:
            return True
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        u32.GetWindowTextW(hwnd, buf, 512)
        t = buf.value.strip()
        # 进度条、输入法、桌面这些窗口的标题没意义
        if not t or t.lower() in JUNK_TITLE:
            return True
        pid = int(pid.value)
        if len(t) > len(res.get(pid, "")):
            res[pid] = t
        return True

    u32.EnumWindows(cb, 0)
    return res


def post(ev):
    try:
        req = urllib.request.Request(
            BRIDGE, data=json.dumps(ev, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        _opener.open(req, timeout=2)
    except Exception:
        pass          # 桥没起来就静默，绝不打扰宿主应用


def cfg_path():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "PetHub" / "apps.json"


def load_cfg():
    p = cfg_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("watch") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        exe = str(it.get("exe") or "").strip().lower()
        if not exe:
            continue
        if not exe.endswith(".exe"):
            exe += ".exe"
        out.append({"name": it.get("name") or exe.rsplit(".", 1)[0],
                    "exe": exe, "enabled": it.get("enabled", True) is not False})
    return out


def seed_known(cfg, procs):
    """第一次运行：把正在跑的 AI 客户端自动纳入观察（用户可随时在菜单里关掉）"""
    if cfg:
        return cfg
    seeded = []
    for exe, name in KNOWN.items():
        if exe in procs:
            seeded.append({"name": name, "exe": exe, "enabled": True})
    if seeded:
        try:
            p = cfg_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"watch": seeded}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception:
            pass
    return seeded


class Watcher:
    """每个应用一份状态机"""

    def __init__(self):
        self.st = {}       # exe -> dict(cpu, title, busy_since, quiet_at, status, posted_title)

    def step(self, app, procs, titles, now):
        exe, name = app["exe"], app["name"]
        s = self.st.setdefault(exe, {"cpu": 0.0, "title": "", "quiet": now, "alive": False,
                                     "status": "idle", "tid": "app:" + exe})
        pids = procs.get(exe) or []
        if not pids:
            s["alive"] = False
            if s["status"] != "idle":
                post({"platform": PLATFORM, "type": "remove", "task_id": s["tid"]})
                s["status"] = "idle"
            return

        # 进程刚起来 = 刚开始干活。这一条很重要：
        # 光看 CPU 会漏掉"启动完就安静下来"的应用（比如打开一个文档），
        # 但那恰恰是用户最想看到"它在动"的时刻。
        appeared = not s["alive"]
        s["alive"] = True

        cpu = 0.0
        known = False
        fresh = False
        for pid in pids:
            v, started = proc_times(pid)
            if v is None:
                continue
            cpu += v
            known = True
            if started and 0 < now - started < 120:
                fresh = True
        title = ""
        for pid in pids:
            t = titles.get(pid, "")
            if len(t) > len(title):
                title = t

        dcpu = cpu - s["cpu"] if known and s["cpu"] else 0.0
        # 标题变化 = 换了活干。第一轮只当基准，不然观察器每次启动都会把
        # 一堆闲置应用当成"刚开始处理任务"，托盘数字莫名其妙跳一下。
        changed = bool(title) and bool(s["title"]) and title != s["title"]
        busy = dcpu > CPU_BUSY or changed or (appeared and fresh)
        s["cpu"], s["title"] = cpu, title or s["title"]

        if busy:
            s["quiet"] = now
            body = {"platform": PLATFORM, "task_id": s["tid"], "status": "running",
                    "title": (name + " · " + title)[:48] if title else (name + " · 正在处理"),
                    "message": title[:70]}
            post(body)
            s["status"] = "running"
        elif s["status"] in ("running", "done") and now - s["quiet"] > QUIET_DONE:
            if s["status"] == "running":
                post({"platform": PLATFORM, "task_id": s["tid"], "status": "done",
                      "title": (name + " · " + s["title"])[:48] if s["title"] else (name + " · 空闲")})
                s["status"] = "done"
            elif now - s["quiet"] > QUIET_DONE + DONE_REMOVE:
                post({"platform": PLATFORM, "type": "remove", "task_id": s["tid"]})
                s["status"] = "idle"


def main():
    once = "--once" in sys.argv
    if "--list" in sys.argv:
        for a in load_cfg():
            print(f"{'on ' if a['enabled'] else 'off'}  {a['name']:20s} {a['exe']}")
        return

    w = Watcher()
    last_cfg_mtime = 0.0
    apps = []
    while True:
        try:
            try:
                m = cfg_path().stat().st_mtime
            except OSError:
                m = 0.0
            if m != last_cfg_mtime:
                last_cfg_mtime = m
                apps = load_cfg()
            procs = list_procs()
            apps = seed_known(apps, procs)
            titles = window_titles()
            now = time.time()
            for a in apps:
                if a["enabled"]:
                    w.step(a, procs, titles, now)
            if once:
                for exe, s in w.st.items():
                    print(f"{exe:24s} status={s['status']:8s} title={s['title'][:40]}")
                return
        except Exception as e:      # 观察器出问题绝不能影响桌宠
            log_err(e)
            if once:
                print("err", e)
        time.sleep(POLL)


def log_err(e):
    """只记错，不记内容：出问题时能查，平时是空文件。"""
    try:
        p = Path(os.environ.get("TEMP", ".")) / "pethub-appwatch.log"
        if p.exists() and p.stat().st_size > 200_000:
            return
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s  %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
