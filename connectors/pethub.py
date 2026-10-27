"""
pethub —— PetHub 桌宠通用接入 CLI

用法：
  python pethub.py list                  查看本机可接入的平台
  python pethub.py install claude-code   接入 Claude Code（自动合并 Hook）
  python pethub.py install workbuddy     接入 WorkBuddy（自动合并 Hook）
  python pethub.py install codex         接入 Codex CLI（会话文件监听）
  python pethub.py install generic       生成通用任务文件 + 轮询器
  python pethub.py status                查看当前任务状态
  python pethub.py say "正在跑" --platform Qoder --status running --progress 40
  python pethub.py say "是否允许删除" --platform Qoder --ask   真实"要授权"事件
  python pethub.py remove claude-code    卸载对应接入

设计原则：
  - 修改任何配置前先备份（*.pethub-backup）
  - 幂等：重复 install 不会产生重复 Hook
  - 卸载干净：只移除本工具写入的条目（以 pethub 标记识别）
"""
import json
import os
import shutil
import sys
import time
import urllib.request
# 本机回环绝不能被系统代理劫持：装了 Clash / V2Ray 的机器上，
# 127.0.0.1 的请求会被代理转走，返回 502，看起来就像"桥没开"。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

BRIDGE = "http://127.0.0.1:8765/api/events"
HOME = Path.home()
HERE = Path(__file__).resolve().parent
PYW = shutil.which("pythonw") or shutil.which("python") or "python"
MARK = "pethub_hook"          # 识别本工具 Hook 的标记


# ---------------- 桥接通信 ----------------
def post(ev):
    req = urllib.request.Request(BRIDGE,
        data=json.dumps(ev, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    _opener.open(req, timeout=3)


def status():
    try:
        with _opener.open("http://127.0.0.1:8765/api/state", timeout=3) as r:
            snap = json.load(r)
    except Exception:
        print("✗ 桥接服务未运行（python bridge/server.py）")
        return
    if not snap["tasks"]:
        print("桥接在线 · 当前 0 个任务")
        return
    cur = None
    for t in snap["tasks"]:
        if t["platform"] != cur:
            cur = t["platform"]
            print(f"\n[{cur}]")
        mark = {"waiting": "⏸", "done": "✓", "error": "✗"}.get(t["status"], "·")
        pct = f" {t['progress']}%" if t["progress"] is not None else ""
        print(f"  {mark} {t['title']}{pct}  {t['status']}")


def test():
    post({"platform": "PetHub 测试", "task_id": "t-" + str(int(time.time())),
          "title": "接入测试任务", "status": "running", "progress": 10})
    print("已发送测试事件 → 桌面 HUD 应出现「PetHub 测试」分组")


# ---------------- Hook 合并（Claude Code / WorkBuddy 通用） ----------------
HOOK_EVENTS = {
    "UserPromptSubmit": "start",     # 用户提问 → 任务开始
    "PostToolUse": "running",        # 工具执行 → 运行中
    "Notification": "waiting",       # 通知 → 通常为等待确认
    "Stop": "done",                  # 回合结束 → 完成
}

def hook_entries(platform_script):
    """生成要写入 settings.json 的 hooks 配置"""
    cmd = f'"{PYW}" "{platform_script}"'
    out = {}
    for ev, arg in HOOK_EVENTS.items():
        out[ev] = [{"hooks": [{"type": "command",
                               "command": f'{cmd} {arg}  # {MARK}:{ev}'}]}]
    return out


def merge_hooks(settings_path, script_path, remove=False):
    p = Path(settings_path)
    if not p.exists():
        print(f"  ✗ 未找到 {p}")
        return False
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ✗ 配置不是有效 JSON，拒绝修改：{e}")
        return False

    backup = p.with_suffix(".json.pethub-backup")
    if not backup.exists():
        shutil.copy2(p, backup)
        print(f"  已备份 → {backup.name}")

    hooks = cfg.setdefault("hooks", {})
    changed = False
    for ev, entries in hook_entries(script_path).items():
        lst = hooks.setdefault(ev, [])
        # 清除本工具旧条目
        cleaned = []
        for grp in lst:
            hs = grp.get("hooks", [])
            hs2 = [h for h in hs if MARK not in h.get("command", "")]
            if hs2:
                grp["hooks"] = hs2
                cleaned.append(grp)
            else:
                changed = True
        hooks[ev] = cleaned
        if not remove:
            hooks[ev].extend(entries)
            changed = True

    if not changed:
        print("  无需更改")
        return True
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ 已{'移除' if remove else '写入'} Hook → {p}")
    return True


# ---------------- Codex 会话监听 ----------------
CODEX_WATCHER = HERE / "codex_watcher.py"

def install_codex():
    if not (HOME / ".codex").exists():
        print("✗ 未检测到 ~/.codex")
        return
    start_detached([PYW, str(CODEX_WATCHER)])
    print("✓ Codex 会话监听已启动（后台，监听 ~/.codex/sessions）")

# ---------------- Generic ----------------
def install_generic():
    d = HOME / ".pethub"
    d.mkdir(exist_ok=True)
    tf = d / "tasks.json"
    if not tf.exists():
        tf.write_text(json.dumps([
            {"task_id": "1", "title": "示例任务（编辑这个文件）",
             "status": "running", "progress": 30, "message": "改我就能同步到桌宠"}
        ], ensure_ascii=False, indent=2), encoding="utf-8")
    start_detached([PYW, str(HERE / "generic_poller.py"), str(tf), "自定义系统"])
    print(f"✓ 已生成 {tf} 并启动轮询器（platform=自定义系统）")
    print("  任何程序写这个 JSON 文件即可同步状态到桌宠")

def start_detached(cmd):
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200       # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        import subprocess
        subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  ! 自启动失败（可手动运行: {' '.join(cmd)}）：{e}")


# ---------------- 自动发现（第 2 层：不内置平台名单） ----------------
def discover_hook_platforms():
    """扫描用户目录下所有 .*/settings.json，返回 (路径, 平台显示名) 列表。
    判定标准：是合法 JSON 对象 —— 不维护平台名单，任何遵循 hooks 约定的平台都会被发现。"""
    found = []
    for p in HOME.glob(".*/settings.json"):
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                name = p.parent.name.lstrip(".")
                found.append((p, name))
        except Exception:
            continue
    return found


def install_auto():
    targets = discover_hook_platforms()
    if not targets:
        print("未发现任何 settings.json（hooks 标准平台）。")
        print("第 1 层 HTTP 协议 / 第 3 层 observer 仍然可用：")
        print("  pethub install generic | pethub install observer")
        return
    print(f"自动发现 {len(targets)} 个可注入平台：")
    for p, name in targets:
        print(f"  · {name}  ({p})")
    script = HERE / "universal_hook.py"
    for p, name in targets:
        print(f"[{name}]")
        merge_universal(p, script, name)


def merge_universal(settings_path, script_path, platform_name, remove=False):
    """向任意 settings.json 注入/移除 universal_hook（platform 名由参数携带）"""
    p = Path(settings_path)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ✗ 配置非有效 JSON，跳过：{e}")
        return
    backup = p.with_suffix(".json.pethub-backup")
    if not backup.exists():
        shutil.copy2(p, backup)
        print(f"  已备份 → {backup.name}")

    hooks = cfg.setdefault("hooks", {})
    cmd = f'"{PYW}" "{script_path}"'
    changed = False
    for ev, arg in HOOK_EVENTS.items():
        lst = hooks.setdefault(ev, [])
        cleaned = []
        for grp in lst:
            hs = [h for h in grp.get("hooks", [])
                  if MARK not in h.get("command", "")]
            if hs:
                grp["hooks"] = hs
                cleaned.append(grp)
            else:
                changed = True
        hooks[ev] = cleaned
        if not remove:
            entry = {"type": "command",
                     "command": f'{cmd} {arg} "{platform_name}"  # {MARK}'}
            hooks[ev].append({"hooks": [entry]})
            changed = True
    if not changed:
        print("  无需更改")
        return
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ 已{'移除' if remove else '注入'} universal_hook")


# ---------------- 命令 ----------------
INSTALLERS = {
    "claude-code": lambda: merge_hooks(HOME / ".claude" / "settings.json",
                                       HERE / "claude_code_hook.py"),
    "workbuddy":   lambda: merge_hooks(HOME / ".workbuddy" / "settings.json",
                                       HERE / "workbuddy_hook.py"),
    "codex":       install_codex,
    "generic":     install_generic,
}

def remove_plat(name):
    if name == "claude-code":
        merge_hooks(HOME / ".claude" / "settings.json", HERE / "claude_code_hook.py", remove=True)
    elif name == "workbuddy":
        merge_hooks(HOME / ".workbuddy" / "settings.json", HERE / "workbuddy_hook.py", remove=True)
    elif name == "auto":
        script = HERE / "universal_hook.py"
        for p, pname in discover_hook_platforms():
            merge_universal(p, script, pname, remove=True)
    elif name == "observer":
        print("observer：结束 pythonw 进程即可（或用任务管理器搜 pythonw）")
    else:
        print("codex/generic：结束对应 pythonw 进程即可")

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        print("—— 已内置适配 ——")
        for name, path in [("claude-code", HOME/".claude"), ("workbuddy", HOME/".workbuddy"),
                           ("codex", HOME/".codex"), ("generic", None)]:
            ok = path is None or path.exists()
            print(f"  {'✓' if ok else '✗'} {name:12s} {'可接入' if ok else '未检测到'}")
        print("—— 自动发现（hooks 标准，非内置）——")
        found = discover_hook_platforms()
        if found:
            for p, name in found:
                print(f"  ✓ {name:12s} {p}")
        else:
            print("  （未发现 settings.json 类平台）")
        print("  · observer   零接入观察器，任何平台都可用")
    elif cmd == "install":
        name = sys.argv[2] if len(sys.argv) > 2 else ""
        if name == "auto":
            install_auto()
            print("完成。新开的会话/任务会自动出现在桌宠 HUD 上。")
            return
        if name == "observer":
            start_detached([PYW, str(HERE / "observer.py")])
            print("✓ 零接入观察器已启动：进程/文件活动将出现在「桌面观察」分组")
            return
        if name not in INSTALLERS:
            print(f"未知平台 {name}，可选：auto, observer, {', '.join(INSTALLERS)}")
            return
        print(f"接入 {name} …")
        INSTALLERS[name]()
        print("完成。新开的会话/任务会自动出现在桌宠 HUD 上。")
    elif cmd == "remove":
        remove_plat(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "status":
        status()
    elif cmd == "test":
        test()
    elif cmd == "say":
        # 一行式上报：任何应用/脚本/CI 都能调用
        #   pethub say "正在重构登录模块" --platform Qoder --status running --progress 40
        #   pethub say "是否允许执行 rm -rf build/" --platform Qoder --ask
        #   pethub say "重构完成" --platform Qoder --done
        args = sys.argv[2:]
        text = next((a for a in args if not a.startswith("--")), "")
        def val(k, d=None):
            return next((args[i+1] for i, a in enumerate(args) if a == k), d)
        platform = val("--platform", "外部应用")
        if "--ask" in args:
            post({"platform": platform, "task_id": val("--id", "ask-" + platform),
                  "title": text[:24] or "等你拍板", "status": "waiting", "ask": True,
                  "message": text})
            print("已上报「等待用户决定」→ 桌宠会喊人")
            return
        # 注意别叫 status：它会把模块函数 status() 遮住，
        # 导致上面 `elif cmd == "status": status()` 抛 UnboundLocalError。
        st = "done" if "--done" in args else ("error" if "--error" in args
             else val("--status", "running"))
        prog = val("--progress")
        post({"platform": platform, "task_id": val("--id", "cli-" + platform),
              "title": text[:40] or platform, "status": st,
              "progress": int(prog) if prog and prog.isdigit() else None,
              "message": val("--message", ""), "ask": False})
        print(f"已上报 {platform} · {st}")
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
