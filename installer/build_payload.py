#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 dist/PetHub 打成 installer/setup/payload.zip（安装包内嵌载荷），
并同时产出 dist 里的便携 zip。

用法：  python installer/build_payload.py [版本号]   (默认 1.2.1)
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dist", "PetHub")
SETUP = os.path.join(ROOT, "installer", "setup")
DIST = os.path.join(ROOT, "dist")

# 运行时生成的临时数据目录不是发布内容
SKIP_DIRS = {"PetHubLight.exe.WebView2", "__pycache__", ".git"}
SKIP_EXT = {".log"}


def build(version: str):
    entries = []
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, SRC).replace("\\", "/")
            entries.append((full, rel))
    entries.sort(key=lambda x: x[1])

    payload = os.path.join(SETUP, "payload.zip")
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, rel in entries:
            z.write(full, rel)
    print("payload.zip:", len(entries), "个文件 ->", payload)

    portable = os.path.join(DIST, f"PetHub-Setup-v{version}.zip")
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, rel in entries:
            z.write(full, rel)
    size = os.path.getsize(portable) / 1024 / 1024
    print("便携 zip:", f"{size:.1f} MB ->", portable)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "1.2.1")
