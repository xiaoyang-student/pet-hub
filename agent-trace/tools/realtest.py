# -*- coding: utf-8 -*-
"""真实表结构接管测试（不改用户任何文件）。

一些软件装了但还没真正用过（表是空的，0 行），这时“抓不到步骤”是正确结果，
并不代表接不上。为了证明它们一旦跑起来就能被跟踪，这里：

  1. 把软件**自己建的真实库**复制到临时目录（表结构、字段、索引全部照搬）；
  2. 按它**自己的字段**写入几条样本行；
  3. 让探针指向临时目录，看它能不能解析出步骤。

全程只读用户的库、只写 %TEMP%，绝不碰原文件。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOME = os.path.expanduser("~")
APPDATA = os.path.join(HOME, "AppData", "Roaming")
OK, BAD = 0, 0


def copy_db(src):
    """复制一个只读库到临时目录，返回可写路径。"""
    if not os.path.exists(src):
        return None
    d = tempfile.mkdtemp(prefix="atrace-real-")
    dst = os.path.join(d, os.path.basename(src))
    shutil.copy2(src, dst)
    return dst


def case_zcode():
    """ZCode：~/.zcode/cli/db/db.sqlite 的 message / part 表。"""
    from adapters.zcode import ZCodeAdapter
    src = os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite")
    dst = copy_db(src)
    if not dst:
        return None, "库不存在"
    con = sqlite3.connect(dst)
    now = int(time.time() * 1000)
    con.execute("insert into session(id,project_id,workspace_id,slug,title,directory,"
                "version,time_created,time_updated) "
                "values('sess_rt','proj_rt','ws_rt','real-test','真实结构接管测试',"
                "'D:/tmp','1.0',?,?)", (now, now))
    for i, (role, txt) in enumerate([
        ("user", "帮我把这个仓库的 README 改成中英双语"),
        ("assistant", "我先看一下现有 README 的结构"),
    ]):
        con.execute(
            "insert into message(id,session_id,time_created,time_updated,data,sequence) "
            "values(?,?,?,?,?,?)",
            ("msg_rt%d" % i, "sess_rt", now + i, now + i,
             json.dumps({"role": role, "content": txt}, ensure_ascii=False), i))
    con.execute(
        "insert into part(id,message_id,session_id,time_created,time_updated,data,sequence) "
        "values('p_rt','msg_rt1','sess_rt',?,?,?,0)",
        (now, now, json.dumps({"type": "tool", "tool": "Read",
                               "state": {"input": {"filePath": "README.md"}}},
                              ensure_ascii=False)))
    con.commit()
    con.close()
    info = {"key": "zcode", "zh": "ZCode", "en": "ZCode", "installed": True,
            "roots": [os.path.dirname(dst)], "exes": [], "install_path": ""}
    return ZCodeAdapter(info, lambda *a: None), dst


def case_qwen():
    """千问办公：agents.db 的 messages / session_event_log 表。"""
    from adapters.qwen_work import QwenWorkAdapter
    src = os.path.join(APPDATA, "QwenWorkCN", "data", "agents.db")
    dst = copy_db(src)
    if not dst:
        return None, "库不存在"
    con = sqlite3.connect(dst)
    now = int(time.time() * 1000)
    for i, (role, txt) in enumerate([
        ("user", "整理一份本周的工作汇报"),
        ("assistant", "我来汇总一下本周的提交记录"),
    ]):
        con.execute(
            "insert into messages(id,message_id,chat_id,sub_chat_id,sequence,role,parts,"
            "metadata,searchable_text,search_status,created_at,updated_at) "
            "values(?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (i + 1, "m_rt%d" % i, "chat_rt", "sub_rt", i, role,
             json.dumps([{"type": "text", "text": txt}], ensure_ascii=False),
             "{}", txt, now + i, now + i))
    con.execute(
        "insert into session_event_log(id,session_id,event_stream_id,runtime_id,event_id,"
        "source_event_id,seq,seq_ids,turn_id,payload,occurred_at,acked_at,created_at) "
        "values(1,'sess_rt','es_rt','rt_rt','e_rt','',1,'','turn_rt',?,?,?,?)",
        (json.dumps({"type": "tool_call", "name": "write_file",
                     "arguments": {"path": "汇报.md"}}, ensure_ascii=False),
         now, now, now))
    con.commit()
    con.close()
    info = {"key": "qwen_work", "zh": "千问办公", "en": "QwenWork", "installed": True,
            "roots": [os.path.dirname(dst)], "exes": [], "install_path": ""}
    return QwenWorkAdapter(info, lambda *a: None), dst


def run(name, builder):
    global OK, BAD
    print("\n=== %s ===" % name)
    try:
        ad, dst = builder()
    except Exception as e:
        print("  构建失败: %s" % e)
        BAD += 1
        return
    if ad is None:
        print("  跳过：%s" % dst)
        return
    if not ad.setup():
        print("  未挂上数据源（表结构可能变了）")
        BAD += 1
        return
    print("  数据源: %s" % ad.source)
    ad.replay(20)
    got = []
    for _ in range(4):
        got.extend(ad.tick(time.time()) or [])
    if got:
        for s in got[:6]:
            print("  %-9s %s" % (s.kind, s.text[:78]))
        print("  -> 抓到 %d 条 ✓" % len(got))
        OK += 1
    else:
        print("  -> 0 条 ✗（探针没能解析出步骤）")
        BAD += 1
    try:
        shutil.rmtree(os.path.dirname(dst), ignore_errors=True)
    except Exception:
        pass


def main():
    print("真实表结构接管测试：复制软件自己的库 → 写样本 → 探针解析")
    print("（全程只写临时目录，不碰用户的原始文件）")
    run("ZCode", case_zcode)
    run("千问办公 QwenWork", case_qwen)
    print("\n结果: 通过 %d / 失败 %d" % (OK, BAD))
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
