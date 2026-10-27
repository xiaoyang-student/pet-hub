"""
通用轮询连接器：把任意 JSON 文件同步成 PetHub 任务。
适合 Codex CLI / Gemini CLI / 自研系统 / CI：让它们往 tasks.json 写状态即可。
用法： python generic_poller.py <watch.json> [platform_name]
文件格式：
  [{"task_id":"1","title":"构建","status":"running","progress":30,"message":"..."}]
"""
import json
import os
import sys
import time
import urllib.request
# 本机回环绝不能被系统代理劫持：装了 Clash / V2Ray 的机器上，
# 127.0.0.1 的请求会被代理转走，返回 502，看起来就像"桥没开"。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BRIDGE = "http://127.0.0.1:8765/api/events"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    path = sys.argv[1]
    plat = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(os.path.basename(path))[0]
    last = None
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                tasks = json.load(f)
            if tasks != last:                      # 变化才上报
                last = tasks
                req = urllib.request.Request(BRIDGE,
                    data=json.dumps({"platform": plat, "type": "sync", "tasks": tasks},
                                    ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                _opener.open(req, timeout=2)
        except FileNotFoundError:
            pass
        except Exception as e:
            print("sync error:", e)
        time.sleep(3)


if __name__ == "__main__":
    main()
