from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import discover
import trace


class DiscoveryTests(unittest.TestCase):
    def test_browser_configuration_databases_are_not_sources(self):
        from adapters.base import name_score
        for name in ("first_party_sets.db", "heavy_ad_intervention_opt_out.db",
                     "trust_layer.db", "gecko2.db"):
            self.assertEqual(name_score(name), -100)
        self.assertGreater(name_score("chat_messages.sqlite"), 0)

    def test_actual_process_enumeration(self):
        if os.name != "nt":
            self.skipTest("Windows process API")
        snapshot = discover._snapshot()
        self.assertIn(os.getpid(), [pid for pids in snapshot.values() for pid in pids])
        self.assertTrue(discover.pid_alive(os.getpid()))

    def test_enum_processes_pointer_contract(self):
        enum = ctypes.CFUNCTYPE(wintypes.BOOL, ctypes.POINTER(wintypes.DWORD),
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        calls = []
        def empty(pointer, size, count):
            calls.append(size)
            count[0] = 0
            return True
        mock = type("Psapi", (), {"EnumProcesses": enum(empty)})()
        with patch.object(discover, "_psapi", mock), patch.object(discover, "_load_psapi", return_value=True):
            self.assertEqual(discover._snapshot(), {})
        self.assertEqual(len(calls), 1)

    def test_failure_retry_is_cached(self):
        with patch.object(discover, "_proc_pids", None), patch.object(discover, "_snapshot", return_value={}) as scan:
            for _ in range(8):
                self.assertEqual(discover._cached(), {})
            self.assertEqual(scan.call_count, 1)

    def test_failure_preserves_previous_names(self):
        with patch.object(discover, "_proc_pids", (0, {"qoder.exe": [42]})), patch.object(discover, "_snapshot", return_value={}) as scan:
            for _ in range(8):
                self.assertEqual(discover._cached(), {"qoder.exe": [42]})
            self.assertEqual(scan.call_count, 1)

    def test_codex_opt_in_requires_its_log_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".codex"
            root.mkdir()
            spec = dict(key="chatgpt", zh="ChatGPT / Codex", en="ChatGPT / Codex",
                        tier="B", reg="unused", installs=[], roots=[str(root)], exes=[])
            with patch.object(discover, "registry_installed", return_value=[]):
                self.assertFalse(discover.detect(spec, set())["codex_logs"])
                (root / "logs_1.sqlite").touch()
                self.assertTrue(discover.detect(spec, set())["codex_logs"])

    def test_install_candidates_only_use_drives_this_machine_has(self):
        """安装候选必须来自本机真实存在的盘（以前写死过 D:\\，只有 C 盘的机器全落空）。"""
        drives = discover.fixed_drives()
        self.assertTrue(drives, "至少应识别出系统盘")
        allowed = {os.path.splitdrive(d)[0].upper() for d in drives}
        for spec in discover.SPECS:
            for d in spec["installs"]:
                drv = os.path.splitdrive(d)[0].upper()
                self.assertIn(drv, allowed,
                              "%s 的候选用了本机没有的盘：%s" % (spec["key"], d))

    def test_install_dir_from_running_process(self):
        """软件装在哪儿不确定，但正在跑的进程一定知道：从映像路径反推安装目录。

        主程序常常不在安装根里（Electron 的 app-1.2.3、版本管理器的 .qoder-versions），
        所以要退到真正的安装根。
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            # 版本管理器布局：...\Qoder\.qoder-versions\0.4.9\Qoder.exe
            deep = base / "Qoder" / ".qoder-versions" / "0.4.9"
            deep.mkdir(parents=True)
            with patch.object(discover, "process_paths_cached",
                              return_value={"qoder.exe": [str(deep / "Qoder.exe")]}):
                self.assertEqual(discover.install_dir_of_running(["Qoder.exe"]),
                                 str(base / "Qoder"))

            # Electron 布局：...\Programs\Qoder\app-1.2.3\Qoder.exe
            app = base / "Programs" / "Qoder" / "app-1.2.3"
            app.mkdir(parents=True)
            with patch.object(discover, "process_paths_cached",
                              return_value={"qoder.exe": [str(app / "Qoder.exe")]}):
                self.assertEqual(discover.install_dir_of_running(["Qoder.exe"]),
                                 str(base / "Programs" / "Qoder"))

            # 用户自己建的深层目录：原样返回，不要多退
            plain = base / "我的工具" / "ZCode"
            plain.mkdir(parents=True)
            with patch.object(discover, "process_paths_cached",
                              return_value={"zcode.exe": [str(plain / "ZCode.exe")]}):
                self.assertEqual(discover.install_dir_of_running(["ZCode.exe"]), str(plain))

    def test_running_app_is_locatable_without_any_preset_dir(self):
        """完全不知道装在哪（候选全空）时，只要它在跑，仍能定位到安装目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "somewhere" / "ZCode"
            d.mkdir(parents=True)
            spec = dict(key="zcode", zh="ZCode", en="ZCode", tier="A",
                        exes=["ZCode.exe"], installs=[], roots=[], reg="unused")
            with patch.object(discover, "process_paths_cached",
                              return_value={"zcode.exe": [str(d / "ZCode.exe")]}), \
                 patch.object(discover, "registry_installed", return_value=[]), \
                 patch.object(discover, "_find_by_exe", return_value=""):
                info = discover.detect(spec, {"zcode.exe"})
                self.assertTrue(info["running"])
                self.assertEqual(info["install_path"], str(d))

    def test_process_lock_not_heartbeat_controls_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ("import sys;sys.path.insert(0,sys.argv[1]);import trace;"
                      "trace.RUNTIME_DIR=sys.argv[2];print(trace.claim_single_instance(),flush=True);"
                      "sys.stdin.readline()")
            proc = subprocess.Popen([sys.executable, "-B", "-c", script, str(ROOT), tmp],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "True")
                heartbeat = Path(tmp) / "heartbeat.json"
                heartbeat.write_text(json.dumps({"pid": proc.pid, "ts": 0}), encoding="utf-8")
                with patch.object(trace, "RUNTIME_DIR", tmp), patch.object(trace, "_instance_file", None):
                    self.assertFalse(trace.claim_single_instance())
                proc.communicate("exit\n", timeout=5)
                with patch.object(trace, "RUNTIME_DIR", tmp), patch.object(trace, "_instance_file", None):
                    self.assertTrue(trace.claim_single_instance())
                    trace._instance_file.close()
                    trace._instance_file = None
            finally:
                if proc.poll() is None:
                    proc.communicate("exit\n", timeout=5)


if __name__ == "__main__":
    unittest.main()
