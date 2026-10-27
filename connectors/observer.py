"""
observer.py —— PetHub 零接入观察器（第 3 层通用）

不需要任何平台配合：监测全机进程与项目目录的文件活动，
把"什么东西在忙"聚合成任务推给桌宠。

任务来源（自动）：
  - 可识别的 Agent/编译/开发进程（python/node/git/cargo/java/... 按进程名聚类）
  - 最近 2 分钟内有文件改动的项目目录（扫描用户目录下常见代码位置）

关闭：结束对应 pythonw 进程，或 pethub remove observer
"""
import json
import time
import urllib.request
# 本机回环绝不能被系统代理劫持：装了 Clash / V2Ray 的机器上，
# 127.0.0.1 的请求会被代理转走，返回 502，看起来就像"桥没开"。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

BRIDGE = "http://127.0.0.1:8765/api/events"
PLATFORM = "桌面观察"

# 聚类这些进程名为一个"工作进程"任务
BUSY_PROCS = {"python", "pythonw", "node", "npm", "git", "cargo", "rustc",
              "java", "javac", "gopls", "go", "dotnet", "clang", "gcc",
              "tsc", "vite", "webpack", "esbuild", "msbuild", "code"}

# 文件活动扫描根目录（存在才扫）
SCAN_ROOTS = ["Desktop", "Documents", "Downloads"]
ACTIVATE_SEC = 120          # 2 分钟内有改动算活跃
POLL = 10                   # 轮询间隔


def post(ev):
    try:
        req = urllib.request.Request(BRIDGE,
            data=json.dumps(ev, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        _opener.open(req, timeout=2)
    except Exception:
        pass


def busy_processes():
    """返回 (进程名 -> 数量)，仅统计 BUSY_PROCS"""
    out = {}
    try:
        import subprocess
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=8)
        for line in r.stdout.splitlines():
            if not line.startswith('"'):
                continue
            name = line.split('","')[0].strip('"').lower()
            base = name.removesuffix(".exe")
            if base in BUSY_PROCS:
                out[base] = out.get(base, 0) + 1
    except Exception:
        pass
    return out


def active_dirs():
    """最近 ACTIVATE_SEC 秒内有文件写入的目录（一层深度聚类）"""
    hot = []
    cutoff = time.time() - ACTIVATE_SEC
    home = Path.home()
    for root_name in SCAN_ROOTS:
        root = home / root_name
        if not root.exists():
            continue
        latest = {}
        try:
            for p in root.rglob("*"):
                try:
                    if p.is_file() and p.stat().st_mtime > cutoff:
                        # 聚到 root 的直接子目录
                        rel = p.relative_to(root)
                        key = str(root / rel.parts[0]) if len(rel.parts) > 1 else str(root)
                        m = p.stat().st_mtime
                        if m > latest.get(key, 0):
                            latest[key] = m
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue
        hot.extend(latest.keys())
    return hot


def main():
    last_sig = None
    while True:
        procs = busy_processes()
        dirs = active_dirs()
        # 同步整批任务（未出现的自动消失）
        tasks = []
        for name, cnt in sorted(procs.items()):
            tasks.append({"task_id": "proc-" + name,
                          "title": f"{name} × {cnt} 在工作",
                          "status": "running",
                          "message": "进程活动监测"})
        for d in dirs[:3]:                      # 最多报 3 个活跃目录
            tasks.append({"task_id": "dir-" + d,
                          "title": Path(d).name + " 有文件在更新",
                          "status": "running",
                          "message": "文件活动监测"})
        sig = json.dumps(tasks, ensure_ascii=False)
        if sig != last_sig:
            last_sig = sig
            post({"platform": PLATFORM, "type": "sync", "tasks": tasks})
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
