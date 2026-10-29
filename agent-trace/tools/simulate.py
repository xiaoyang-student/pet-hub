# -*- coding: utf-8 -*-
"""离线仿真自检：用各软件的真实表结构造样本，验证探针解析是否正确。

用途：你还没在这几款软件里跑过任务时，也能先确认探针是好的。
全程只在系统临时目录造文件，绝不碰各软件自己的数据。

    python tools/simulate.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters.base import ReadOnlyDB          # noqa: E402
from adapters.generic import SchemaProbe      # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "agent_trace_sim")
os.makedirs(TMP, exist_ok=True)

TEXT = "开始分析仓库结构，先找出所有入口文件"
TOOL = "Read"


def _mk(name, schema, inserts):
    p = os.path.join(TMP, name)
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass
    con = sqlite3.connect(p)
    for s in schema:
        con.execute(s)
    for sql, args in inserts:
        con.execute(sql, args)
    con.commit()
    con.close()
    return p


def case_zcode():
    """ZCode: ~/.zcode/cli/db/db.sqlite 的 session / message / part 结构。"""
    p = _mk("zcode.sqlite",
            ["create table session(id text primary key, title text)",
             "create table message(id text primary key, session_id text, seq integer,"
             " role text, parts text, created_at integer)",
             "create table tool_usage(id text primary key, session_id text, seq integer,"
             " tool text, created_at integer)"],
            [("insert into session values('s1','示例会话')", ()),
             ("insert into message values('m1','s1',1,'assistant',?,1000)" ,
              (json.dumps([{"type": "text", "text": TEXT}]),)),
             ("insert into message values('m2','s1',2,'assistant',?,2000)",
              (json.dumps([{"type": "tool_use", "name": TOOL,
                            "input": {"file_path": "/src/main.py"}}]),)),
             ("insert into tool_usage values('t1','s1',3,?,3000)", (TOOL,))])
    return p, ["text:" + TEXT, "tool:" + TOOL]


def case_qwen():
    """千问办公: data/agents.db 的 session_event_log / messages 结构。"""
    p = _mk("qwen.sqlite",
            ["create table session_event_log(id integer primary key, session_id text,"
             " seq integer, payload text, occurred_at integer)",
             "create table messages(id integer primary key, message_id text,"
             " sequence integer, role text, parts text, created_at integer)"],
            [("insert into session_event_log values(1,'s1',1,?,1000)",
              (json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "text", "text": TEXT}]}}),)),
             ("insert into session_event_log values(2,'s1',2,?,2000)",
              (json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                    "input": {"command": "ls -la"}}]}}),)),
             ("insert into messages values(1,'mm',1,'user',?,3000)",
              (json.dumps([{"type": "text", "text": "帮我看一下这个项目"}]),))])
    return p, ["text:" + TEXT, "tool:Bash"]


def case_generic():
    """通用结构：历史记录表 + 文本列。"""
    p = _mk("generic.sqlite",
            ["create table chat_history(id integer primary key, created_at integer,"
             " role text, content text)"],
            [("insert into chat_history values(1,1000,'assistant',?)", (TEXT,)),
             ("insert into chat_history values(2,2000,'assistant',?)",
              (json.dumps([{"type": "text", "text": "现在开始改文件"}]),))])
    return p, ["text:" + TEXT, "text:现在开始改文件"]


def run(name, path, expect):
    db = ReadOnlyDB(path)
    if not db.open():
        print("  [FAIL] %s 打不开" % name)
        return False
    probe = SchemaProbe(db, log=lambda *a: None)
    n = probe.build()
    if not n:
        print("  [FAIL] %s 没识别出表" % name)
        return False
    got = []
    for tp in probe.pollers:
        tp.rewind(1000)
    for kind, text, ts, meta in probe.poll():
        got.append("%s:%s" % ("text" if kind in ("answer", "user", "status")
                              else ("tool" if kind == "tool" else kind), text))
    ok = True
    for e in expect:
        hit = any(e in g for g in got)
        mark = "OK  " if hit else "MISS"
        if not hit:
            ok = False
        print("    %s %s  -> %s" % (mark, name, e))
    if not expect:
        print("    (无期望项) 实际: %s" % got[:3])
    return ok


def main():
    print("=== 离线仿真自检（样本写在 %s）===" % TMP)
    ok = True
    for fn in (case_zcode, case_qwen, case_generic):
        name = fn.__name__.replace("case_", "")
        path, expect = fn()
        print("  [%s] %s" % (name, os.path.basename(path)))
        ok = run(name, path, expect) and ok
    print("=== 结果: %s ===" % ("全部通过" if ok else "有未通过项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
