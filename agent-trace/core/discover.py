# -*- coding: utf-8 -*-
"""自动发现：扫注册表 / 磁盘 / 用户目录 / 进程，自动认出 SPECS 里的软件。

原则：
  1. 零手工导入 —— 装了就自动挂上对应探针；
  2. 只读、不写、不启动任何被监控的软件；
  3. 全部 stdlib。

关于耗时：注册表 + 已知路径这一段确实很快（几十毫秒量级）；
但"磁盘兜底扫主程序"会对整盘做 glob，很贵，因此它被限制为
每个 key 在进程生命周期内只跑一次（见 _find_by_exe）。
进程枚举结果全局缓存 10 秒（见 _cached），所有调用方共用。
"""
from __future__ import annotations

import ctypes
import glob
import os
import re
import time
from ctypes import wintypes

# ---------------------------------------------------------------
# 环境
# ---------------------------------------------------------------
HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
LOCALAPP = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))
PROGRAM_FILES = os.environ.get("ProgramFiles", r"C:\Program Files")
PROGRAM_FILES_X86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
PROGRAMS = os.path.join(LOCALAPP, "Programs")


def _reg_subkeys(base, path):
    """读注册表子键，失败静默。"""
    out = []
    try:
        import winreg
        with winreg.OpenKey(base, path) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
                if i > 400:
                    break
    except Exception:
        pass
    return out


def _reg_value(base, path, name):
    try:
        import winreg
        with winreg.OpenKey(base, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def registry_installed(name_re: str):
    """按显示名模糊匹配已安装程序，返回 [(名称, 版本, 安装目录)]。"""
    import winreg
    hits = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    rx = re.compile(name_re, re.I)
    for base, path in roots:
        for sub in _reg_subkeys(base, path):
            try:
                with winreg.OpenKey(base, path + "\\" + sub) as k:
                    disp = winreg.QueryValueEx(k, "DisplayName")[0]
            except Exception:
                continue
            if not disp or not rx.search(disp):
                continue
            try:
                with winreg.OpenKey(base, path + "\\" + sub) as k:
                    ver = winreg.QueryValueEx(k, "DisplayVersion")[0]
            except Exception:
                ver = ""
            try:
                with winreg.OpenKey(base, path + "\\" + sub) as k:
                    loc = winreg.QueryValueEx(k, "InstallLocation")[0]
            except Exception:
                loc = ""
            hits.append((disp, ver, (loc or "").strip('"')))
    return hits


def msix_packages(name_re: str):
    """按包名匹配 MSIX/商店应用（ChatGPT 桌面版就是这种，装在 WindowsApps 里，
    按 exe 搜是搜不到的）。返回 [(名称, 版本, 包族名)]。"""
    import winreg
    hits = []
    base = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppModel\Repository\Packages")
    rx = re.compile(name_re, re.I)
    for sub in _reg_subkeys(winreg.HKEY_CURRENT_USER, base):
        # 子键形如 OpenAI.Codex_26.1021.2667.0_x64__2p2nqsd0c76g0
        name = sub.split("_")[0]
        if not name or not rx.search(name):
            continue
        parts = [p for p in sub.split("_") if p]
        ver = parts[1] if len(parts) > 2 else ""
        family = parts[0] + "_" + parts[-1] if len(parts) > 2 else sub
        hits.append((name, ver, family))
    return hits


# ---------------------------------------------------------------
# 进程枚举（ctypes，不启子进程）
#
# 两个必须注意的点：
#   1. OpenProcess 返回 64 位 HANDLE。ctypes 默认 restype=c_int，
#      高位置位的句柄会被截断：可能变成 0（误判"打不开"）或负数
#      （再拿这个错误值去 CloseHandle）。必须显式声明 restype/argtypes。
#   2. 枚举一次要对上千个进程逐个 OpenProcess + GetModuleBaseNameW，
#      成本远不止旧注释里写的"<2ms"。旧实现每个适配器各自缓存，
#      8 个适配器 + reap_dead 合计每 10 秒要扫约 16 次全机进程。
#      这里改成**全局共享缓存**，所有调用方共用一份。
# ---------------------------------------------------------------
_psapi = None
_k32 = None
_PROC_TTL = 10.0
_proc_pids = None          # (时间戳, {映像名: [pid, ...]})
_proc_paths = None         # (时间戳, {映像名: [完整映像路径, ...]})


def _load_psapi():
    global _psapi, _k32
    if _psapi is None:
        try:
            _psapi = ctypes.WinDLL("psapi")
            _k32 = ctypes.WinDLL("kernel32")
            _k32.OpenProcess.restype = wintypes.HANDLE
            _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
            _k32.CloseHandle.argtypes = [wintypes.HANDLE]
            _psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                             wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
            _psapi.GetModuleBaseNameW.restype = wintypes.DWORD
            _psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE,
                                                  wintypes.HMODULE,
                                                  wintypes.LPWSTR,
                                                  wintypes.DWORD]
            # 完整映像路径：软件装在哪儿不确定时，这是最可靠的证据
            _psapi.GetModuleFileNameExW.restype = wintypes.DWORD
            _psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE,
                                                    wintypes.HMODULE,
                                                    wintypes.LPWSTR,
                                                    wintypes.DWORD]
        except Exception:
            _psapi = False
            _k32 = False
    return _psapi


def _snapshot(paths_out=None):
    """扫一次全机进程，返回 {映像名小写: [pid, ...]}。失败返回 {}。

    paths_out 传一个 dict 进来，会顺带填成 {映像名: [完整映像路径, ...]}。
    句柄已经打开了，再问一次路径几乎不额外花钱——而"软件装在哪儿"这件事，
    运行中的进程给出的答案比任何预设目录都准。
    """
    if not _load_psapi():
        return {}
    arr = (wintypes.DWORD * 8192)()
    cb = wintypes.DWORD()
    try:
        if not _psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(cb)):
            return {}
    except Exception:
        return {}
    n = min(cb.value // ctypes.sizeof(wintypes.DWORD), 8192)
    out = {}
    buf = ctypes.create_unicode_buffer(261)
    pbuf = ctypes.create_unicode_buffer(1024)
    for i in range(n):
        pid = arr[i]
        if not pid:
            continue
        h = None
        try:
            h = _k32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION|VM_READ
            if not h:
                continue
            if _psapi.GetModuleBaseNameW(h, None, buf, 261):
                nm = buf.value.lower()
                out.setdefault(nm, []).append(pid)
                if paths_out is not None:
                    try:
                        if _psapi.GetModuleFileNameExW(h, None, pbuf, 1024):
                            p = pbuf.value
                            if p:
                                lst = paths_out.setdefault(nm, [])
                                if p not in lst:
                                    lst.append(p)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if h:
                try:
                    _k32.CloseHandle(h)
                except Exception:
                    pass
    return out


def _cached(ttl=_PROC_TTL):
    """共享的进程表。ttl=0 表示强制刷新。"""
    global _proc_pids, _proc_paths
    now = time.time()
    if _proc_pids is not None and now - _proc_pids[0] < ttl:
        return _proc_pids[1]
    paths = {}
    m = _snapshot(paths)
    if not m:
        # 失败也缓存重试时间，避免八个适配器同时重复枚举。
        m = _proc_pids[1] if _proc_pids else {}
        paths = _proc_paths[1] if _proc_paths else {}
    _proc_pids = (now, m)
    _proc_paths = (now, paths)
    return m


def process_names():
    """返回当前运行的进程映像名集合（小写，如 qoder.exe）。强制刷新。"""
    return set(_cached(0))


def process_names_cached(ttl=_PROC_TTL):
    """带全局缓存的版本。适配器判活请用这个，别自己再缓存一份。"""
    return set(_cached(ttl))


def process_paths_cached(ttl=_PROC_TTL):
    """运行中进程的完整路径 {映像名: [路径, ...]}，与进程名共用同一次枚举。

    软件装在哪儿是不确定的，但"它正在从哪个 exe 跑起来"是确定的事实，
    所以定位安装目录时它比任何预设目录都可靠。
    """
    _cached(ttl)
    return _proc_paths[1] if _proc_paths else {}


def install_dir_of_running(exes):
    """某软件正在运行时，从它的进程映像反推安装目录；没在跑返回 ""。

    主程序常常并不直接躺在安装根目录里：
      * Electron：...\\Qoder\\app-1.2.3\\Qoder.exe
      * 带版本管理器：D:\\Qoder\\.qoder-versions\\0.4.9\\Qoder.exe
    所以拿到目录后要往上退，直到它看起来像真正的安装根。
    """
    paths = process_paths_cached()
    for exe in exes or []:
        for p in paths.get((exe or "").lower(), []):
            d = os.path.dirname(p)
            if not d or not os.path.isdir(d):
                continue
            for _ in range(3):
                base = os.path.basename(d).lower()
                if not base:
                    break
                versionish = (base.startswith("app-") or base.startswith(".")
                              or base in ("bin", "current", "resources", "app")
                              or bool(re.match(r"^\d+(\.\d+)*$", base)))
                if not versionish:
                    break
                parent = os.path.dirname(d)
                if not parent or not os.path.isdir(parent) or len(parent) <= 3:
                    break
                d = parent
            return d
    return ""


def process_pids_by_name(name: str):
    """返回匹配进程名的 PID 列表。"""
    return list(_cached().get((name or "").lower(), []))


# ---------------------------------------------------------------
# 本机可用盘符 / 常见安装父目录（运行时探测，不写死）
#
# 以前这里写死过 r"D:\Qoder"、r"D:\百度搭子" 这类路径，兜底扫描也写死成
# ["D:\\", "E:\\", "C:\\"]。可 AI 软件装在哪儿根本不确定：
#   * 只有 C 盘的机器 —— D/E 全是无效路径，白扫；
#   * 装在 E:\\ / F:\\ / 某个自建目录的机器 —— 一样扫不到。
# 所以改成运行时枚举本机真实存在的固定盘，再叠加 Windows 常见安装父目录。
# ---------------------------------------------------------------
_DRIVE_FIXED = 3          # Win32 GetDriveType：固定硬盘
_drives_cache = None
_parents_cache = None


def fixed_drives():
    """本机可用的固定盘根目录，如 ['C:\\\\', 'D:\\\\']。

    只认固定硬盘：光驱（空盘会卡住）和网络盘（整盘 glob 极慢）一律跳过。
    结果缓存，进程内只探测一次。
    """
    global _drives_cache
    if _drives_cache is not None:
        return _drives_cache
    out = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        get_type.restype = ctypes.c_uint
        get_type.argtypes = [ctypes.c_wchar_p]
        for i in range(26):
            if not (mask & (1 << i)):
                continue
            root = "%s:\\" % chr(ord("A") + i)
            try:
                if get_type(root) != _DRIVE_FIXED:
                    continue
                if os.path.isdir(root):
                    out.append(root)
            except Exception:
                continue
    except Exception:
        out = []
    if not out:
        # 探测失败（非 Windows / API 不可用）：退回系统盘，至少不为空
        sd = os.environ.get("SystemDrive", "")
        if sd and os.path.isdir(sd + os.sep):
            out = [sd + os.sep]
        else:
            drv = os.path.splitdrive(os.path.abspath(HOME))[0]
            if drv:
                out = [drv + os.sep]
    _drives_cache = out
    return out


def install_parents():
    """装软件最常见的父目录：每个固定盘根 + %LOCALAPPDATA%\\Programs
    + Program Files（含 x86）。已去重，只保留真实存在的。"""
    global _parents_cache
    if _parents_cache is not None:
        return _parents_cache
    seen, out = set(), []
    for p in list(fixed_drives()) + [PROGRAMS, os.path.join(LOCALAPP, "Programs"),
                                     PROGRAM_FILES, PROGRAM_FILES_X86]:
        if not p:
            continue
        try:
            k = os.path.normcase(os.path.abspath(p))
        except Exception:
            continue
        if k in seen or not os.path.isdir(p):
            continue
        seen.add(k)
        out.append(p)
    _parents_cache = out
    return out


def _dirs(names):
    """把文件夹名展开成"每个父目录下"的候选安装目录。"""
    out = []
    for p in install_parents():
        for n in names:
            out.append(os.path.join(p, n))
    return out


# ---------------------------------------------------------------
# 7 款软件的指纹表
#   roots: 数据目录候选（存在即用）
#   exes : 进程 / 安装目录识别用的主程序名
#   tier : A=结构化数据库/日志  B=半结构化  C=浏览器壳(读 LocalStorage)
#   installs 一律由 _dirs() 生成，不写死某个盘符
# ---------------------------------------------------------------
SPECS = [
    dict(key="workbuddy", zh="WorkBuddy", en="WorkBuddy", tier="A",
         exes=["WorkBuddy.exe"],
         installs=_dirs(["WorkBuddy", "workbuddy"]),
         roots=[os.path.join(HOME, ".workbuddy")],
         reg=r"WorkBuddy"),

    dict(key="qoder", zh="Qoder", en="Qoder", tier="A",
         exes=["Qoder.exe", "Qoder IDE.exe"],
         installs=_dirs(["Qoder", "Qoder IDE"]),
         roots=[os.path.join(HOME, ".qoder"), os.path.join(HOME, ".qoder-cn"),
                os.path.join(APPDATA, "com.qoder.app.stable"), os.path.join(APPDATA, "Qoder")],
         reg=r"^Qoder"),

    dict(key="zcode", zh="ZCode", en="ZCode", tier="A",
         exes=["ZCode.exe"],
         installs=_dirs(["ZCode", "zcode"]),
         roots=[os.path.join(HOME, ".zcode"), os.path.join(APPDATA, "ZCode")],
         reg=r"^ZCode"),

    dict(key="qwen_work", zh="千问办公", en="Qwen Work", tier="A",
         exes=["QwenWorkCN.exe"],
         installs=_dirs(["QwenWorkCN", "千问办公"]),
         roots=[os.path.join(HOME, ".qwenworkcn"), os.path.join(APPDATA, "QwenWorkCN")],
         reg=r"^千问办公|QwenWork"),

    dict(key="doubao_work", zh="豆包工作", en="Doubao Work", tier="A",
         exes=["DoubaoWork.exe"],
         installs=_dirs(["DoubaoWork"]),
         roots=[os.path.join(LOCALAPP, "DoubaoWork"), os.path.join(APPDATA, "DoubaoWork")],
         reg=r"^豆包工作"),

    dict(key="doubao", zh="豆包", en="Doubao", tier="B",
         exes=["Doubao.exe"],
         installs=_dirs(["Doubao"]),
         roots=[os.path.join(HOME, ".doubao"), os.path.join(LOCALAPP, "Doubao"),
                os.path.join(APPDATA, "Doubao")],
         reg=r"^豆包$"),

    # 百度搭子 = 千帆桌面应用 (qianfan-desktop-app) + 内嵌 opencode 智能体。
    # 数据不在 %APPDATA%\DuMate（那个目录不存在），真实事件流在
    # %APPDATA%\qianfan-desktop-app\log\desktop-main\AgtEvent.log —— A 级精度。
    dict(key="baidu_dazi", zh="百度搭子", en="Baidu Dazi (DuMate)", tier="A",
         exes=["DuMate.exe", "dumate-main-server.exe", "dumate-opencode.exe"],
         installs=_dirs(["DuMate", "百度搭子", os.path.join("百度搭子", "DuMate")]),
         roots=[os.path.join(APPDATA, "qianfan-desktop-app"),
                os.path.join(HOME, ".qianfan"),
                os.path.join(LOCALAPP, "qianfan-desktop-app")],
         reg=r"DuMate|百度搭子|qianfan"),

    dict(key="chatgpt", zh="ChatGPT / Codex", en="ChatGPT / Codex", tier="B",
         exes=["ChatGPT.exe", "Codex.exe"],
         installs=_dirs(["ChatGPT", "Codex"]),
         roots=[os.path.join(HOME, ".codex"), os.path.join(APPDATA, "ChatGPT"),
                os.path.join(LOCALAPP, "ChatGPT")],
         reg=r"^ChatGPT",
         msix=r"OpenAI\.Codex|ChatGPT"),
]

# 兜底：在常见盘符里按 exe 名找安装目录。
# 这一步很贵——它对整盘做 depth<=2 的 glob（C:\*\*\Qoder.exe 这种模式
# 实际会遍历 C:\Users\*\*、C:\Windows\*\*、C:\Program Files\*\*）。
# 旧实现没有任何限制，而 engine 每 300 秒重扫一次机器，于是机器上
# **没装**的那几款软件每 5 分钟就触发一轮跨盘扫描，在长驻进程里
# 持续制造磁盘 IO。这里改成每个 key 在进程生命周期内只扫一次，
# 结果（包括"没找到"）缓存下来。
# 另：原来写的是 r"D:\\"，那是原始字符串，字面值是两个反斜杠；
# 而且写死 D/E/C 只对特定机器成立。现在用 install_parents()：
# 只有 C 盘的机器不再白扫 D/E，装在 E:\\ F:\\ 的机器也不会漏。
_FALLBACK_SCAN_DIRS = install_parents()
_fallback_cache = {}


def _find_by_exe(spec):
    """装了但不在预设位置时，扫常见目录找主程序。每个 key 只扫一次。"""
    key = spec["key"]
    if key in _fallback_cache:
        return _fallback_cache[key]
    found = ""
    for d in _FALLBACK_SCAN_DIRS:
        if not d or not os.path.isdir(d):
            continue
        for exe in spec["exes"]:
            for depth in (0, 1, 2):
                pat = os.path.join(d, *(["*"] * depth), exe)
                try:
                    hits = glob.glob(pat)
                except Exception:
                    hits = []
                if hits:
                    found = os.path.dirname(hits[0])
                    break
            if found:
                break
        if found:
            break
    _fallback_cache[key] = found
    return found


def detect(spec, procset):
    """识别单个软件，返回信息字典。"""
    info = dict(key=spec["key"], zh=spec["zh"], en=spec["en"], tier=spec["tier"],
                installed=False, install_path="", version="", roots=[], running=False,
                processes=0)

    # 1) 注册表
    for disp, ver, loc in registry_installed(spec["reg"]):
        info["installed"] = True
        info["version"] = ver or info["version"]
        if loc and os.path.isdir(loc):
            info["install_path"] = info["install_path"] or loc

    # 2) 预设安装目录
    if not info["install_path"]:
        for d in spec["installs"]:
            if os.path.isdir(d):
                info["installed"] = True
                info["install_path"] = d
                break

    # 2.5) MSIX / 商店应用（ChatGPT 桌面版就是这种）
    if spec.get("msix"):
        for name, ver, family in msix_packages(spec["msix"]):
            info["installed"] = True
            info["version"] = ver or info["version"]
            pd = os.path.join(LOCALAPP, "Packages", family)
            if os.path.isdir(pd) and pd not in info["roots"]:
                info["roots"].append(pd)

    # 3) 数据目录（只要数据目录在，说明真装过/用过）
    for r in spec["roots"]:
        if os.path.isdir(r):
            info["installed"] = True
            info["roots"].append(r)

    if spec["key"] == "chatgpt":
        info["codex_logs"] = any(glob.glob(os.path.join(r, "logs_*.sqlite"))
                                  for r in info["roots"]
                                  if os.path.basename(r).lower() == ".codex")

    # 4) 磁盘兜底扫主程序
    if not info["installed"]:
        p = _find_by_exe(spec)
        if p:
            info["installed"] = True
            info["install_path"] = p

    # 5) 进程
    for exe in spec["exes"]:
        if exe.lower() in procset:
            info["running"] = True
            info["installed"] = True
            info["processes"] += 1

    # 5.5) 正在跑但还没定位到装在哪 → 直接问进程自己的映像路径。
    # 装在 C 盘 / D 盘 / 任意深层目录 / 便携版都一样拿得到。
    if info["running"] and not info["install_path"]:
        d = install_dir_of_running(spec["exes"])
        if d:
            info["install_path"] = d

    if not info["install_path"]:
        for r in spec["roots"]:
            if os.path.isdir(r):
                info["install_path"] = r
                break
    return info


def detect_all():
    """扫一遍全部 SPECS（当前 8 款），返回 {key: info}。"""
    procset = process_names_cached()
    out = {}
    for spec in SPECS:
        try:
            out[spec["key"]] = detect(spec, procset)
        except Exception:
            out[spec["key"]] = dict(key=spec["key"], zh=spec["zh"], en=spec["en"],
                                    tier=spec["tier"], installed=False,
                                    install_path="", version="", roots=[],
                                    running=False, processes=0, error=True)
    return out


def is_running_cached(spec_key, ttl=_PROC_TTL):
    """某个软件是否在运行。走全局进程缓存，reap 循环请一律用这个。"""
    spec = next((s for s in SPECS if s["key"] == spec_key), None)
    if not spec:
        return False
    names = _cached(ttl)
    return any(e.lower() in names for e in spec["exes"])


def is_running(spec_key):
    """兼容旧调用：等价于 is_running_cached。"""
    return is_running_cached(spec_key)


def pid_alive(pid):
    """某个 pid 是否还在。不枚举全机进程，只开一次句柄。

    供追踪器的单实例判定使用。旧实现在 trace.py 里直接调
    ctypes.windll.kernel32.OpenProcess，没声明 restype —— 默认 c_int
    会把 64 位 HANDLE 截断，可能误判"进程已死"，于是两个追踪器实例
    同时跑起来抢同一批文件游标。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True              # 判断不了就当它活着，别抢
    if pid <= 0:
        return False
    if not _load_psapi():
        return True
    h = None
    try:
        h = _k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        return bool(h)
    except Exception:
        return True
    finally:
        if h:
            try:
                _k32.CloseHandle(h)
            except Exception:
                pass
