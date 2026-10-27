"""
三平台演示模拟器：向 bridge 持续上报 WorkBuddy / Claude Code / Codex 的任务事件。
用法：  python simulate.py            # 需要 bridge/server.py 已在运行
"""
import json
import random
import time
import urllib.request

URL = "http://127.0.0.1:8765/api/events"

PLATS = {
    "WorkBuddy": [
        {"id": "w1", "title": "整理桌面文件并归档", "progress": 30},
        {"id": "w2", "title": "撰写产品周报", "progress": 10},
        {"id": "w3", "title": "部署前端更新", "progress": 100, "status": "done"},
    ],
    "Claude Code": [
        {"id": "c1", "title": "重构 pet_kernel 状态机", "progress": 60},
        {"id": "c2", "title": "修复 SSE 断线重连", "progress": None, "status": "thinking"},
    ],
    "Codex": [
        {"id": "x1", "title": "编写连接器 SDK 文档", "progress": 20},
    ],
}


def post(ev):
    req = urllib.request.Request(URL, data=json.dumps(ev, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        r.read()


def sync_all():
    for plat, tasks in PLATS.items():
        post({"platform": plat, "type": "sync", "tasks": tasks})
    print("已同步初始任务")


def main():
    sync_all()
    tick = 0
    print("模拟中，Ctrl+C 退出…")
    while True:
        tick += 1
        time.sleep(1.2)
        for plat, tasks in PLATS.items():
            for t in tasks:
                if t.get("status") in ("done", "error", "waiting"):
                    continue
                if t["progress"] is None:
                    continue
                t["progress"] = min(100, t["progress"] + random.uniform(1.5, 5))
                if t["progress"] >= 100:
                    t["status"] = "error" if random.random() < 0.12 else "done"
                    if t["status"] == "error":
                        t["message"] = "命令执行失败，等待处理"
                    post({"platform": plat, "task_id": t["id"], "title": t["title"],
                          "status": t["status"], "progress": 100, "message": t.get("message", "")})
                    continue
                post({"platform": plat, "task_id": t["id"], "title": t["title"],
                      "status": "running", "progress": round(t["progress"], 1)})
        # 演示"等待确认"
        if tick == 12:
            w2 = PLATS["WorkBuddy"][1]
            w2["status"] = "waiting"
            post({"platform": "WorkBuddy", "task_id": "w2", "title": w2["title"],
                  "status": "waiting", "message": "需要确认：覆盖 3 个同名文件"})
        if tick == 28:
            w2 = PLATS["WorkBuddy"][1]
            w2["status"] = None
            post({"platform": "WorkBuddy", "task_id": "w2", "title": w2["title"],
                  "status": "running", "message": "确认完成，继续执行", "progress": w2["progress"]})
        # 完成任务 60 秒后重启一轮
        if tick % 55 == 0:
            for plat, tasks in PLATS.items():
                for t in tasks:
                    if t.get("status") in ("done", "error"):
                        t["status"] = None
                        t["progress"] = 5
                        t.pop("message", None)
                        post({"platform": plat, "task_id": t["id"], "title": t["title"],
                              "status": "running", "progress": 5})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
