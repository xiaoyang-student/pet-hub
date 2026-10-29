# -*- coding: utf-8 -*-
"""千问办公适配器的回归测试。

跑法（在 agent-trace 目录下）：
    ../vendor/python/python.exe tools/test_qwen_work.py
    ../vendor/python/python.exe tools/test_qwen_work.py --real   # 追加真实日志回放

两组用例：
  A. 合成事件序列 —— 按本机实测的真实 schema 逐条喂给适配器，
     断言 9 个状态各自落在正确的 kind / phase 上。确定性、不依赖本机装没装千问办公。
  B. 真实日志回放（--real）—— 拿 ~/.qwenworkcn 下最大那个 segments/*.jsonl
     整份重放，统计各状态命中数，用来确认没有"认不出来"的情况。

全部 stdlib，不用 pytest。退出码 0 = 全通过。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from adapters.qwen_work import QwenWorkAdapter          # noqa: E402
from core.model import (K_ASK, K_ANSWER, K_DENIED, K_ERROR, K_RESULT,        # noqa: E402
                        K_STATUS, K_THINK, K_TOOL, K_TOOL_DONE, K_USER,
                        P_ASKING, P_DENIED, P_DONE, P_RECEIVED, P_THINKING,
                        P_TOOL)

PASS, FAIL = 0, 0


def check(name, cond, got=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s   -> %r" % (name, got))


# ------------------------------------------------------------------ 造数据
def ev(typ, turn_id="T1", tcid="", seq=None, data=None, ts=None):
    o = {"type": typ, "ts": ts or (time.time()), "level": "info",
         "turn_id": turn_id, "data": data or {}}
    if seq is not None:
        o["seq"] = seq
    if tcid:
        o["tool_call_id"] = tcid
    return o


class Harness:
    """搭一个假的 ~/.qwenworkcn 目录树，让适配器以为自己在读真实数据。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="pethub-qw-test-")
        self.segdir = os.path.join(self.root, "logs", "sessions",
                                   "PROJ", "SESS", "segments")
        os.makedirs(self.segdir)
        self.seg = os.path.join(self.segdir, "2026-01-01T00-00-00-000+08-00-aaaaaa-p1.jsonl")
        open(self.seg, "w", encoding="utf-8").close()
        # manifest：适配器靠它取项目名做副标题
        rundir = os.path.join(self.root, "logs", "runs",
                              "2026-01-01T00-00-00-000+08-00-aaaaaa-p1")
        os.makedirs(rundir)
        with open(os.path.join(rundir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"run_id": "aaaaaa", "pid": os.getpid(),
                       "cwd": r"D:\我的项目\demo-workspace",
                       "cli_version": "1.1.71"}, f)
        self.ad = QwenWorkAdapter(
            {"key": "qwen_work", "roots": [self.root], "exes": [],
             "install_path": ""}, log=lambda *a: None)
        self.now = 1_800_000_000.0
        assert self.ad.setup(), "setup() 应该成功找到段文件"

    def feed(self, *events):
        """追加事件并轮询一次，返回本轮产出的 Step。"""
        with open(self.seg, "a", encoding="utf-8") as f:
            for e in events:
                self.now += 0.05
                e = dict(e)
                e["ts"] = self.now
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return self.ad.tick(self.now)

    def kinds(self, steps):
        return [s.kind for s in steps]

    def phases(self, steps):
        return [s.meta.get("phase") for s in steps]

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


# ------------------------------------------------------------------ A 组
def test_synthetic():
    print("\n=== A. 合成事件序列（按本机实测 schema）===")
    h = Harness()
    try:
        # ---- 1. 已接题 ------------------------------------------------
        s = h.feed(ev("input.prompt.submitted",
                      data={"text_preview": "帮我修复登录模块的空指针",
                            "text_length": 15}))
        check("已接题 -> K_USER", K_USER in h.kinds(s), h.kinds(s))
        check("已接题 phase=received", P_RECEIVED in h.phases(s), h.phases(s))
        check("标题带上用户原话预览",
              any("空指针" in x.text for x in s), [x.text for x in s])

        # ---- 2. 本轮开始 ----------------------------------------------
        s = h.feed(ev("turn.started", data={"model": "flash", "prompt_length": 15}))
        check("turn.started 记为第 1 轮", h.ad.turns["T1"].turn_no == 1,
              h.ad.turns["T1"].turn_no)

        # ---- 3. 思考中 ------------------------------------------------
        s = h.feed(ev("model.request.started"))
        check("思考中 -> K_THINK", K_THINK in h.kinds(s), h.kinds(s))
        check("思考中 phase=thinking", P_THINKING in h.phases(s), h.phases(s))

        # ---- 4. 模型要求调工具 ----------------------------------------
        h.feed(ev("model.response.completed", data={"stop_reason": "tool_use"}))
        check("stop_reason=tool_use 不算回答完毕",
              h.ad.turns["T1"].phase == P_THINKING, h.ad.turns["T1"].phase)

        # ---- 5. 执行工具 ----------------------------------------------
        s = h.feed(ev("tool.requested", tcid="c1",
                      data={"tool_name": "Bash",
                            "args": {"command": "ls -la src/"}}))
        check("执行工具 -> K_TOOL", K_TOOL in h.kinds(s), h.kinds(s))
        check("执行工具 phase=tool", P_TOOL in h.phases(s), h.phases(s))
        check("工具标题含工具名与命令摘要",
              any("Bash" in x.text and "ls -la" in x.text for x in s),
              [x.text for x in s])

        # ---- 6. 需要用户选择（核心） ----------------------------------
        s = h.feed(ev("permission.requested", tcid="c1",
                      data={"tool_name": "Bash",
                            "ask_message": "Allow Bash to run: ls -la src/",
                            "suggestion_count": 1}))
        check("等用户拍板 -> K_ASK", K_ASK in h.kinds(s), h.kinds(s))
        check("等用户拍板 phase=asking", P_ASKING in h.phases(s), h.phases(s))
        check("ask=True 已置位", any(x.meta.get("ask") for x in s),
              [x.meta for x in s])
        check("用软件自己的 ask_message 原文，不自己编",
              any("Allow Bash to run" in x.text for x in s), [x.text for x in s])
        t = h.ad.turns["T1"]
        check("pending 记录了这个 tool_call_id", "c1" in t.pending, t.pending)
        check("ask_since 已记下（用于计算已等待多久）", t.ask_since > 0, t.ask_since)

        # ---- 6b. 等待期间的定时重发 -----------------------------------
        before = len([1 for x in h.ad.turns["T1"].pending])
        h.now += 6.0                      # 超过 ASK_REEMIT=5 秒
        s = h.ad.tick(h.now)
        check("等待期间会重发（让计时走动）", K_ASK in h.kinds(s), h.kinds(s))
        check("重发文案带已等待时长",
              any("已等待" in x.text for x in s), [x.text for x in s])
        check("重发不会误清 pending", len(h.ad.turns["T1"].pending) == before)

        # ---- 7. 用户批准 ----------------------------------------------
        s = h.feed(ev("permission.resolved", tcid="c1",
                      data={"tool_name": "Bash", "allowed": True,
                            "source": "user", "outcome": "prompt",
                            "decision_behavior": "allow", "duration_ms": 68}))
        check("批准后 pending 清空", not h.ad.turns["T1"].pending,
              h.ad.turns["T1"].pending)
        check("批准后回到 tool 阶段", h.ad.turns["T1"].phase == P_TOOL,
              h.ad.turns["T1"].phase)
        check("批准事件有播报", any("已批准" in x.text for x in s),
              [x.text for x in s])

        # ---- 8. 策略自动放行：根本没问过用户，不该播报 ----------------
        s = h.feed(ev("permission.resolved", tcid="c9",
                      data={"tool_name": "Read", "allowed": True,
                            "source": "policy", "decision_behavior": "allow"}))
        check("source=policy 的自动放行不播报（它没打扰用户）",
              not any(x.kind == K_STATUS for x in s), h.kinds(s))

        # ---- 9. 工具返回 ----------------------------------------------
        h.feed(ev("tool.shell.started", tcid="c1", data={"command": "ls -la src/"}))
        s = h.feed(ev("tool.execution.finished", tcid="c1",
                      data={"tool_name": "Bash", "status": "success",
                            "text_chars": 1007}))
        check("工具成功 -> K_TOOL_DONE", K_TOOL_DONE in h.kinds(s), h.kinds(s))

        # ---- 10. 工具失败 -> 出错 -------------------------------------
        s = h.feed(ev("tool.requested", tcid="c2",
                      data={"tool_name": "Bash", "args": {"command": "false"}}))
        s = h.feed(ev("tool.execution.finished", tcid="c2",
                      data={"tool_name": "Bash", "status": "error"}))
        check("工具失败 -> K_ERROR", K_ERROR in h.kinds(s), h.kinds(s))

        # ---- 11. 组织回答 --------------------------------------------
        h.feed(ev("model.request.started"))
        s = h.feed(ev("model.response.completed", data={"stop_reason": "end_turn"}))
        check("stop_reason=end_turn -> K_ANSWER", K_ANSWER in h.kinds(s),
              h.kinds(s))

        # ---- 12. 本轮完成（真实证据，不是超时猜的） -------------------
        s = h.feed(ev("turn.finished",
                      data={"reason": "end_turn", "num_turns": 11,
                            "duration_ms": 164190}))
        check("本轮完成 -> K_RESULT", K_RESULT in h.kinds(s), h.kinds(s))
        check("本轮完成 phase=done", P_DONE in h.phases(s), h.phases(s))
        check("完成文案带步数与耗时",
              any("11 步" in x.text and "2 分 44 秒" in x.text for x in s),
              [x.text for x in s])

        # ---- 13. 用户拒绝授权 ----------------------------------------
        h.feed(ev("turn.started"))
        h.feed(ev("tool.requested", tcid="c3",
                  data={"tool_name": "Write", "args": {"file_path": "a.txt"}}))
        h.feed(ev("permission.requested", tcid="c3",
                  data={"tool_name": "Write", "ask_message": "Allow Write a.txt?"}))
        s = h.feed(ev("permission.resolved", tcid="c3",
                      data={"tool_name": "Write", "allowed": False,
                            "source": "user", "decision_behavior": "deny"}))
        check("拒绝授权 -> K_DENIED", K_DENIED in h.kinds(s), h.kinds(s))
        check("拒绝授权 phase=denied", P_DENIED in h.phases(s), h.phases(s))
        check("拒绝文案含工具名", any("Write" in x.text for x in s),
              [x.text for x in s])

        # ---- 14. 多会话：不同 turn_id 各自独立 ------------------------
        h.feed(ev("input.prompt.submitted", turn_id="T2",
                  data={"text_preview": "另一个窗口的问题"}))
        h.feed(ev("permission.requested", turn_id="T2", tcid="d1",
                  data={"tool_name": "Bash", "ask_message": "Allow rm?"}))
        check("两个会话各自记状态", set(h.ad.turns) >= {"T1", "T2"},
              sorted(h.ad.turns))
        check("T2 在等拍板、T1 不受影响",
              h.ad.turns["T2"].phase == P_ASKING and h.ad.turns["T1"].phase != P_ASKING,
              (h.ad.turns["T1"].phase, h.ad.turns["T2"].phase))
        st = h.ad.status()
        check("status() 优先显示正在等拍板的那个会话",
              st.phase == P_ASKING, st.phase)
        check("status() 的 detail 带已等待时长", "已等待" in st.detail, st.detail)

        # ---- 15. 噪音事件不产出步骤 ----------------------------------
        n0 = h.ad.steps
        h.feed(ev("hook.started"), ev("hook.finished"),
               ev("attachment.generator.finished"),
               ev("loop.iteration.started"), ev("loop.iteration.finished"),
               ev("tool.read.diagnostic"), ev("session.phase.started",
                                              data={"phase": "mcp_context.refresh"}))
        check("hook/attachment/loop/diagnostic 全部忽略",
              h.ad.steps == n0, (n0, h.ad.steps))

        # ---- 16. session 字段落到 Step 上（状态栏按会话分行靠它）------
        s = h.feed(ev("tool.requested", turn_id="T2", tcid="d2",
                      data={"tool_name": "Read", "args": {"file_path": "b.py"}}))
        check("Step.session = 段文件所属会话，而不是轮次 id",
              all(x.session == "SESS" and x.turn == h.ad.turns["T2"].uid for x in s),
              [(x.session, x.turn) for x in s])
        check("cur_session 跟着最新活动走", h.ad.cur_session == "SESS",
              h.ad.cur_session)

        # ---- 17. 阶段失败 -> 出错 ------------------------------------
        s = h.feed(ev("session.phase.finished",
                      data={"phase": "mcp_context.refresh", "success": False}))
        check("阶段 success=false -> K_ERROR", K_ERROR in h.kinds(s), h.kinds(s))

        # ---- 18. 项目名副标题来自 manifest.cwd -----------------------
        check("项目名取自 manifest 的 cwd 末级目录",
              h.ad.project == "demo-workspace", h.ad.project)
    finally:
        h.close()


# ------------------------------------------------------------------ B 组
def test_real():
    print("\n=== B. 真实日志回放（本机 ~/.qwenworkcn）===")
    home = os.path.expanduser("~")
    pats = [os.path.join(home, ".qwenworkcn", "logs", "sessions",
                         "*", "*", "segments", "*.jsonl")]
    files = []
    for p in pats:
        files.extend(glob.glob(p))
    if not files:
        print("  skip 本机没有千问办公的段文件")
        return
    path = max(files, key=os.path.getsize)
    print("  回放：%s (%d KB)" % (os.path.basename(path),
                                  os.path.getsize(path) // 1024))

    root = tempfile.mkdtemp(prefix="pethub-qw-real-")
    try:
        segdir = os.path.join(root, "logs", "sessions", "P", "S", "segments")
        os.makedirs(segdir)
        dst = os.path.join(segdir, os.path.basename(path))
        open(dst, "w", encoding="utf-8").close()
        ad = QwenWorkAdapter({"key": "qwen_work", "roots": [root], "exes": [],
                              "install_path": ""}, log=lambda *a: None)
        assert ad.setup()
        counts, phases, samples = {}, {}, {}
        now = time.time()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                with open(dst, "a", encoding="utf-8") as g:
                    g.write(line + "\n")
                for s in ad.tick(now):
                    counts[s.kind] = counts.get(s.kind, 0) + 1
                    ph = s.meta.get("phase")
                    if ph:
                        phases[ph] = phases.get(ph, 0) + 1
                    samples.setdefault(s.kind, s.text)
        print("  命中 kind ：", dict(sorted(counts.items(),
                                            key=lambda kv: -kv[1])))
        print("  命中 phase：", dict(sorted(phases.items(),
                                            key=lambda kv: -kv[1])))
        for k in sorted(samples):
            print("    %-10s %s" % (k, samples[k][:72]))
        check("真实数据里认出了用户提问", counts.get(K_USER, 0) > 0, counts)
        check("真实数据里认出了思考", counts.get(K_THINK, 0) > 0, counts)
        check("真实数据里认出了工具调用", counts.get(K_TOOL, 0) > 0, counts)
        check("真实数据里认出了工具返回", counts.get(K_TOOL_DONE, 0) > 0, counts)
        check("真实数据里认出了本轮完成", counts.get(K_RESULT, 0) > 0, counts)
        # 授权事件只有在真实会话里确实弹过窗才会出现
        if counts.get(K_ASK, 0):
            check("等拍板事件的文案来自 ask_message",
                  len(samples.get(K_ASK, "")) > 0, samples.get(K_ASK))
        else:
            print("  note 这份日志里没有 permission.requested（未弹窗）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_regressions():
    from adapters.qwen_work import _Reader, SINGLE_READ

    h = Harness()
    try:
        newseg = os.path.join(h.segdir, "new-run.jsonl")
        with open(newseg, "w", encoding="utf-8") as f:
            f.write(json.dumps(ev("input.prompt.submitted", turn_id="NEW",
                                 data={"text_preview": "first batch"})) + "\n")
        h.ad._rescan_at = 0
        steps = h.ad.tick(h.now)
        check("新段发现前首批不丢", any(s.session == "SESS" and s.kind == K_USER
                                    for s in steps))
        check("新段空闲不重复", not h.ad.tick(h.now + 1))

        for allowed in (True, False):
            h.feed(ev("turn.started"))
            h.feed(ev("permission.requested", tcid="a",
                      data={"tool_name": "Read", "ask_message": "read first?"}),
                   ev("permission.requested", tcid="b",
                      data={"tool_name": "Write", "ask_message": "write second?"}))
            t = h.ad.turns["T1"]
            h.feed(ev("permission.resolved", tcid="unrelated",
                      data={"allowed": True, "source": "policy"}))
            check("无关 policy 不删除 pending", set(t.pending) == {"a", "b"}, t.pending)
            h.feed(ev("permission.resolved", data={"allowed": True, "source": "user"}))
            check("缺失 tool_call_id 不猜配对", set(t.pending) == {"a", "b"}, t.pending)
            steps = h.feed(ev("permission.resolved", tcid="a",
                              data={"allowed": allowed, "source": "user"}))
            check("解决一个授权后保持 asking", t.phase == P_ASKING and
                  set(t.pending) == {"b"} and all(s.meta["phase"] == P_ASKING for s in steps))
            check("剩余授权保留自身文案与时间", t.ask_msg == "write second?" and
                  t.ask_since == t.pending["b"] and t.ask_tool == "Write")
            h.feed(ev("model.response.completed", data={"stop_reason": "tool_use"}),
                   ev("tool.execution.finished", tcid="other", data={"status": "success"}))
            check("并行工具事件不能遮盖待授权", t.phase == P_ASKING)
            h.now += 6
            steps = h.ad.tick(h.now)
            check("剩余授权继续定时重发", any(s.kind == K_ASK and "write second?" in s.text
                                         and s.meta.get("pending") == 1 for s in steps))
            h.feed(ev("turn.finished", data={"reason": "end_turn"}))
            check("终态释放授权及工具缓存", not t.pending and not t.ask_details and not t.tools)

        with open(h.seg, "w", encoding="utf-8") as f:
            f.write(json.dumps(ev("permission.requested", tcid="cold",
                                 data={"ask_message": "cold pending?"})) + "\n")
        os.utime(h.seg, (h.now + 100, h.now + 100))
        cold = QwenWorkAdapter({"roots": [h.root], "exes": []})
        cold.setup()
        check("冷启动静默重建授权", cold.steps == 0 and cold.turns["T1"].phase == P_ASKING)
        check("冷启动默认零回溯仍从 EOF", not _Reader(h.seg).read_lines())

        path = os.path.join(h.root, "reader.jsonl")
        with open(path, "wb") as f:
            f.write(b"old\n")
        rd = _Reader(path, from_start=True)
        check("显式从头读取首行", rd.read_lines() == ["old"])
        replacement = path + ".new"
        with open(replacement, "wb") as f:
            f.write(b"new\n")
        os.replace(replacement, path)
        check("同大小替换读取新代际", rd.read_lines() == ["new"])
        with open(path, "wb") as f:
            f.write(b"replacement-longer\n")
        check("截断再增长超过游标仍重读", rd.read_lines() == ["replacement-longer"])
        with open(path, "wb") as f:
            f.write(b"x\n")
        check("截断首行不丢", rd.read_lines() == ["x"])
        with open(path, "ab") as f:
            f.write(b"partial")
        check("半行暂存不输出", rd.read_lines() == [])
        with open(path, "ab") as f:
            f.write(b"-done\n")
        check("半行跨轮拼接", rd.read_lines() == ["partial-done"])
        with open(path, "ab") as f:
            f.write(b"x" * (SINGLE_READ * 3) + b"\nok\n")
        result = []
        for _ in range(5):
            result.extend(rd.read_lines())
            check("超长未完成行内存有界", len(rd.buf) <= SINGLE_READ)
        check("超长行丢弃后下一行可读", result == ["ok"], result[:1])
        from unittest.mock import patch
        with patch("builtins.open", side_effect=AssertionError("idle opened file")):
            check("千问空闲 100 轮不打开文件", all(rd.read_lines() == [] for _ in range(100)))
        names = [f.__name__ for f in _test_funcs()]
        check("用例集合自动扫描且排除 test_real",
              "test_real" not in names and "test_activity_window" in names, names)
    finally:
        h.close()


def test_terminal_barrier():
    print("\n=== C. 轮次身份、去重与完成屏障 ===")
    h = Harness()
    try:
        steps = h.feed(ev("input.prompt.submitted", seq=0),
                       ev("turn.started", seq=1),
                       ev("tool.requested", seq=2, tcid="c",
                          data={"tool_name": "Read"}),
                       ev("turn.finished", seq=3))
        t = h.ad.turns["T1"]
        first = t.uid
        check("提交、开始、工具、完成使用同一非零轮次键",
              len(steps) == 4 and {s.turn for s in steps} == {first}
              and all(s.session == "SESS" for s in steps))
        check("完成保留轮次且明确 terminal", t.turn_no == 1 and t.closed
              and steps[-1].meta.get("terminal") is True)
        before = (t.last_ts, h.ad.steps, t.label)
        late = [ev("input.prompt.submitted", seq=0), ev("turn.started", seq=1),
                ev("turn.finished", seq=3), ev("turn.finished", seq=4),
                ev("input.prompt.received", data={"text_preview": "late"}),
                ev("model.request.started"),
                ev("model.response.completed", data={"stop_reason": "end_turn"}),
                ev("tool.requested", tcid="late"), ev("tool.shell.started"),
                ev("tool.execution.finished", data={"status": "error"}),
                ev("permission.requested", tcid="late"),
                ev("permission.resolved", data={"allowed": False}),
                ev("session.phase.finished", data={"success": False})]
        check("重复开始/完成与迟到事件不重开", h.feed(*late) == [])
        check("屏障不更新活动时间或残留工具授权", before ==
              (t.last_ts, h.ad.steps, t.label) and not t.tools and not t.pending
              and h.ad.status().phase == P_DONE and not h.ad._snapshot_steps(h.now))
        steps = h.feed(ev("input.prompt.submitted", seq=5), ev("turn.started", seq=6))
        second = t.uid
        check("明确新提交重开且 started 不重复递增",
              {s.turn for s in steps} == {second} and second != first
              and all(s.session == "SESS" for s in steps) and not t.closed)
        check("重复 submitted/started 去重", h.feed(ev("input.prompt.submitted", seq=5),
              ev("turn.started", seq=6)) == [] and t.turn_no == 2)
        steps = h.feed(ev("tool.execution.finished", seq=7, data={"status": "error"}))
        check("工具错误不是整轮终态", len(steps) == 1 and
              steps[0].meta.get("terminal") is False and not t.closed)
        check("工具出错仍可继续模型", K_THINK in h.kinds(h.feed(ev("model.request.started", seq=8))))
        steps = h.feed(ev("turn.finished", seq=9, data={"reason": "error"}))
        check("异常整轮结束 terminal 且保留键", steps[0].kind == K_ERROR and
              steps[0].meta.get("terminal") is True and steps[0].turn == second and t.closed)
        check("异常完成后也忽略迟到工具", h.feed(ev("tool.requested")) == [])
        steps = h.feed(ev("turn.started", seq=10))
        third = t.uid
        check("独立 started 开第三轮", len(steps) == 1 and steps[0].turn == third
              and third not in (first, second))
        check("同 seq 仅处理一次", len(h.feed(ev("tool.requested", seq=11),
              ev("tool.requested", seq=11))) == 1)
        # seq 未在旧测试中使用；补齐 0、缺失、乱序、来源/会话隔离语义。
        check("不同 seq 即使小于已见序号仍处理", len(h.feed(ev("tool.requested", seq=20),
              ev("tool.requested", seq=19))) == 2)
        check("缺失 seq 不误去重", len(h.feed(ev("tool.requested"), ev("tool.requested"))) == 2)
        check("不同会话同 seq 不冲突", len(h.feed(ev("turn.started", turn_id="T2", seq=11))) == 1)
        check("新段可重新编号", len(h.ad._feed(json.dumps(ev("turn.started", seq=0)),
              source="another-segment")) == 1)
        for seq in range(1000, 2000):
            h.ad._feed(json.dumps(ev("hook.finished", seq=seq)))
        check("seq 去重缓存有界", len(t.seen_seq) <= 512)

        cold = QwenWorkAdapter({"roots": [], "exes": []})
        for e in (ev("turn.started", seq=1), ev("turn.finished", seq=2)):
            cold._feed(json.dumps(e), emit=False)
        check("冷重建记住完成且保持静默", cold.steps == 0 and cold.turns["T1"].closed)
        check("冷重建后迟到记录不重开", cold._feed(json.dumps(ev("tool.requested", seq=3))) == []
              and cold.status().phase == P_DONE)
    finally:
        h.close()


def test_native_questions():
    from adapters.qwen_work import MAX_NATIVE_CALLS, NATIVE_ASK

    print("\n=== D. 原生问题在授权落盘前等待 ===")
    h = Harness()
    call = "call_5812b034f6d5492382df4aa4"
    args = {"questions": [{"question": "请选择桌宠的展示风格：",
                           "header": "展示风格", "multiSelect": False,
                           "options": [{"label": "简洁版"}, {"label": "详细版"}]}],
            "answers": {"请选择桌宠的展示风格：": "简洁版"}}

    def request(tcid=call, seq=None):
        return ev("tool.requested", tcid=tcid, seq=seq,
                  data={"tool_name": "AskUserQuestion", "args": args})

    def finish(tcid=call, seq=None, status="success"):
        return ev("tool.execution.finished", tcid=tcid, seq=seq,
                  data={"tool_name": "AskUserQuestion", "status": status})

    try:
        h.feed(ev("input.prompt.submitted", seq=1,
                  data={"text_preview": "普通任务"}), ev("turn.started", seq=2))
        steps = h.feed(request(seq=41))
        t = h.ad.turns["T1"]
        since = t.ask_since
        check("原生 tool.requested 立即发选择等待而不是通用工具",
              K_ASK in h.kinds(steps) and K_TOOL not in h.kinds(steps)
              and all(s.text.startswith(NATIVE_ASK) and s.meta.get("ask")
                      and s.meta["phase"] == P_ASKING for s in steps))
        check("非空 args.answers 不是完成证据", set(t.pending) == {call}
              and t.native_calls[call] and t.ask_msg == NATIVE_ASK)
        before = t.nsteps
        check("重复 seq 与重复调用均不重新发问",
              h.feed(request(seq=41), request(seq=42)) == []
              and t.nsteps == before and t.ask_since == since)
        h.feed(ev("model.response.completed", seq=45, data={"stop_reason": "tool_use"}))
        check("真实 seq45 tool_use 仍保持选择等待", t.phase == P_ASKING)
        steps = h.feed(ev("model.request.started"),
                       ev("model.response.completed", data={"stop_reason": "end_turn"}),
                       ev("tool.requested", tcid="other", data={"tool_name": "Read"}),
                       finish("other"), finish("old"), finish(""),
                       ev("tool.shell.finished", tcid=call, data={"status": "success"}))
        check("模型和无关/缺 ID/旧/非 execution 结果不解除问题",
              set(t.pending) == {call} and t.phase == P_ASKING
              and all(s.meta["phase"] == P_ASKING for s in steps))
        h.now += 100
        steps = h.ad.tick(h.now)
        check("100秒未答仍重发固定选择文案", any(s.kind == K_ASK
              and s.text.startswith(NATIVE_ASK) and "已等待 1 分" in s.text
              for s in steps) and t.ask_since == since)
        check("状态和快照保持选择文案", h.ad.status(h.now).detail.startswith(NATIVE_ASK)
              and h.ad.status(h.now).phase == P_ASKING
              and h.ad._snapshot_steps(h.now)[0].text.startswith(NATIVE_ASK))
        cold = QwenWorkAdapter({"roots": [h.root], "exes": []})
        check("原生等待冷启动静默重建", cold.setup() and cold.steps == 0
              and cold.turns["T1"].pending.keys() == t.pending.keys()
              and cold.turns["T1"].ask_msg == NATIVE_ASK
              and cold.turns["T1"].phase == P_ASKING)
        check("冷重建后仍能刷新等待", any(s.kind == K_ASK and
              s.text.startswith(NATIVE_ASK) for s in cold.tick(h.now + 6)))
        steps = h.feed(ev("permission.requested", tcid=call, seq=49,
                          data={"tool_name": "AskUserQuestion",
                                "ask_message": "Answer questions?", "args": args}))
        check("延迟 permission.requested 不改选择文案或开始时间",
              t.ask_msg == NATIVE_ASK and t.ask_since == since
              and all(s.text.startswith(NATIVE_ASK) for s in steps))
        resolved = ev("permission.resolved", tcid=call, seq=50,
                      data={"tool_name": "AskUserQuestion", "allowed": True,
                            "source": "user", "duration_ms": 101092,
                            "decision_reason": {
                                "decisionPoint": "AskUserQuestion.interaction.ask"}})
        steps = h.feed(resolved)
        check("选择完成不用批准授权文案，继续思考而非终态",
              not t.pending and t.phase == P_THINKING and not t.closed
              and any("已收到选择" in s.text and s.meta.get("duration_ms") == 101092
                      for s in steps) and all("批准" not in s.text for s in steps))
        check("重复解决事件静默去重", h.feed(resolved) == [])
        h.feed(finish(seq=59))
        check("选择工具返回仍非整轮完成", t.phase == P_THINKING and not t.closed)
        check("已解决调用不因重复请求或迟到授权重开",
              h.feed(request(seq=60), ev("permission.requested", tcid=call, seq=61,
                     data={"tool_name": "AskUserQuestion", "ask_message": "Answer questions?"}))
              == [] and not t.pending)
        h.feed(request("unanswered"), ev("turn.finished", seq=76,
                                        data={"reason": "end_turn"}))
        check("终态清空全部原生缓存", t.closed and t.phase == P_DONE
              and not t.pending and not t.ask_details and not t.native_calls
              and not t.tools and not t.ask_since)
        check("终态屏障挡住迟到原生事件", h.feed(request("late"), finish("unanswered"),
              ev("permission.requested", tcid="late", data={"tool_name": "AskUserQuestion"})) == [])
        steps = h.feed(ev("input.prompt.submitted", seq=80), ev("turn.started", seq=81),
                       request("next", seq=82), finish(call, seq=83))
        check("新轮次独立等待，旧结果不能解除", t.turn_no == 2 and not t.closed
              and set(t.pending) == {"next"} and t.phase == P_ASKING
              and {s.turn for s in steps} == {t.uid})
        h.feed(ev("turn.started", seq=84))
        check("新轮开始也清理尚未解决的原生缓存", not t.native_calls
              and not t.pending and not t.ask_details and not t.tools)

        for permission_first in (True, False):
            h.feed(ev("turn.started"))
            permission = ev("permission.requested", tcid="permission",
                            data={"tool_name": "Bash", "ask_message": "Allow Bash?"})
            h.feed(*([permission, request()] if permission_first else [request(), permission]))
            check("授权和原生问题同时保留", set(t.pending) == {call, "permission"})
            if permission_first:
                h.feed(finish())
                check("原生 fallback 只解除自身，保留授权内容和等待",
                      set(t.pending) == {"permission"} and t.ask_msg == "Allow Bash?"
                      and t.ask_since == t.pending["permission"] and t.phase == P_ASKING)
            steps = h.feed(ev("permission.resolved", tcid="permission",
                              data={"allowed": True, "source": "user"}))
            check("普通授权仍使用批准文案", any("已批准 Bash" in s.text for s in steps))
            if not permission_first:
                check("授权解决不解除原生问题", set(t.pending) == {call}
                      and t.ask_msg == NATIVE_ASK and t.phase == P_ASKING)
                steps = h.feed(finish())
                check("缺失 permission.resolved 时精确工具完成解除原生等待",
                      not t.pending and t.phase == P_THINKING and not t.closed
                      and K_TOOL_DONE in h.kinds(steps) and K_RESULT not in h.kinds(steps))

        h.feed(ev("turn.started"), request(), ev("permission.requested", tcid="keep",
               data={"tool_name": "Read", "ask_message": "Keep permission?"}))
        h.feed(ev("permission.resolved", tcid="old", data={"allowed": True, "source": "user"}),
               ev("permission.resolved", data={"allowed": True, "source": "user"}))
        check("旧或缺ID的授权结果不能解除原生选择", set(t.pending) == {call, "keep"})
        steps = h.feed(ev("permission.resolved", tcid=call,
                         data={"allowed": True, "source": "user"}))
        check("原生选择解决仍保留并行授权等待", set(t.pending) == {"keep"}
              and t.ask_msg == "Keep permission?" and t.ask_since == t.pending["keep"]
              and all(s.meta["phase"] == P_ASKING for s in steps))
        h.feed(request("fallback"), finish("fallback"))
        check("fallback之后迟到授权和重复调用不会重开",
              h.feed(request("fallback"), ev("permission.requested", tcid="fallback",
                     data={"tool_name": "AskUserQuestion", "ask_message": "Answer questions?"})) == []
              and set(t.pending) == {"keep"})

        h.feed(ev("turn.started"), request(), request("second"))
        h.feed(finish())
        check("一个原生完成不影响另一个原生", set(t.pending) == {"second"}
              and t.ask_msg == NATIVE_ASK and t.ask_since == t.pending["second"])
        h.feed(finish("second", status="error"))
        check("原生工具失败也解除等待但不关闭整轮", not t.pending
              and t.phase == P_THINKING and not t.closed)
        h.feed(request("cancel"))
        steps = h.feed(ev("permission.resolved", tcid="cancel",
                          data={"allowed": False, "source": "user"}))
        check("取消选择不误写为拒绝授权", not t.pending and t.phase == P_THINKING
              and any("已取消选择" in s.text for s in steps)
              and all("授权" not in s.text for s in steps))

        h.feed(ev("turn.started"), ev("input.prompt.received",
               data={"text_preview": "AskUserQuestion 等待用户选择"}))
        for name, tcid in (("askuserquestion", "lower"), ("OtherAskUserQuestion", "prefix"),
                           ("AskUserQuestion", ""), ("AskUserQuestion", "   "),
                           ("AskUserQuestion", 123), ("Bash", "normal")):
            steps = h.feed(ev("tool.requested", tcid=tcid,
                              data={"tool_name": name, "args": args}))
            check("只认精确原生工具名和有效字符串ID: %r/%r" % (name, tcid),
                  K_TOOL in h.kinds(steps) and K_ASK not in h.kinds(steps) and not t.pending)

        h.feed(ev("turn.started"), ev("permission.requested", tcid=call,
               data={"tool_name": "AskUserQuestion", "ask_message": "Answer questions?"}))
        check("冷窗口只有原生授权请求也使用选择文案", t.ask_msg == NATIVE_ASK)
        h.feed(finish())
        check("只有原生授权请求仍支持完成兜底", not t.pending and t.phase == P_THINKING)
        for completed in (False, True):
            h.feed(ev("turn.started"), ev("permission.requested", tcid="keep",
                   data={"tool_name": "Read", "ask_message": "Keep permission?"}))
            for i in range(MAX_NATIVE_CALLS + 20):
                h.ad._feed(json.dumps(request("bounded-%d" % i)), emit=False)
                if completed:
                    h.ad._feed(json.dumps(ev("permission.resolved", tcid="bounded-%d" % i,
                               data={"allowed": True, "source": "user"})), emit=False)
            check("原生缓存和关联字段有界，保留其他授权 (%s)" % completed,
                  len(t.native_calls) <= MAX_NATIVE_CALLS and len(t.tools) <= MAX_NATIVE_CALLS
                  and len(t.pending) <= MAX_NATIVE_CALLS + 1
                  and len(t.ask_details) <= MAX_NATIVE_CALLS + 1 and "keep" in t.pending)
    finally:
        h.close()


def test_segment_selection():
    from adapters.qwen_work import MAX_TRACKED, STUB_BYTES

    print("\n=== E. 空桩段文件不挤占活跃文件 ===")
    h = Harness()
    try:
        with open(h.seg, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev("input.prompt.submitted", turn_id="LIVE",
                                  data={"text_preview": "真实会话"}), ensure_ascii=False) + "\n")
            f.write(" " * (STUB_BYTES + 64) + "\n")
        now = time.time()
        os.utime(h.seg, (now - 5, now - 5))
        for i in range(5):
            d = os.path.join(h.root, "logs", "sessions", "PROJ", "STUB%d" % i, "segments")
            os.makedirs(d)
            p = os.path.join(d, "stub-%d.jsonl" % i)
            with open(p, "w", encoding="utf-8") as f:
                for typ in ("session.config.loaded", "cli.route.entered"):
                    f.write(json.dumps({"type": typ, "ts": h.now, "turn_id": "",
                                        "data": {}}) + "\n")
            os.utime(p, (now - 4 + i, now - 4 + i))       # 空桩比活跃文件新得多
        h.ad._rescan_at = 0
        h.ad.tick(h.now)
        paths = set(h.ad.readers)
        check("最新空桩仍被保留（新 run 会写入它）",
              any("STUB4" in p for p in paths), paths)
        check("有内容的活跃文件不会被空桩挤掉", h.seg in paths, paths)
        check("同时跟的段文件数有界", len(paths) <= MAX_TRACKED, len(paths))
        check("更早的空桩不占名额",
              not any(("STUB%d" % i) in p for i in range(4) for p in paths), paths)
        steps = h.feed(ev("turn.started", turn_id="LIVE"))
        check("被保留的活跃文件仍能实时产出",
              any(x.kind == K_STATUS and x.session == "SESS" for x in steps),
              h.kinds(steps))
    finally:
        h.close()


def make_seg(root, sid, name, size, age):
    d = os.path.join(root, "logs", "sessions", "PROJ", sid, "segments")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "turn.started", "turn_id": "",
                            "ts": time.time(), "data": {}}) + "\n")
        if size > 0:
            f.write(" " * size + "\n")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_activity_window():
    print("\n=== F. 活跃窗口：旧会话不占名额 ===")
    h = Harness()
    try:
        fresh = make_seg(h.root, "S-FRESH", "fresh.jsonl", 30000, 5)
        old = make_seg(h.root, "S-OLD", "old.jsonl", 40000, 40 * 60)
        older = make_seg(h.root, "S-OLDER", "older.jsonl", 50000, 6 * 3600)
        files = h.ad._seg_files()
        chosen = h.ad._choose(files)
        check("近期有内容的会话被选中", fresh in chosen, chosen)
        check("40 分钟前的旧会话不进候选", old not in chosen, chosen)
        check("6 小时前的旧会话不进候选", older not in chosen, chosen)
    finally:
        h.close()


def test_full_slots():
    print("\n=== F2. 满额时空桩不抢真实窗口 ===")
    h = Harness()
    try:
        os.utime(h.seg, (time.time() - 7200,) * 2)
        stub = make_seg(h.root, "S-STUB", "stub.jsonl", 0, 5)
        wins = [make_seg(h.root, "S-W%d" % i, "w%d.jsonl" % i,
                         12000 + i * 2000, 10 + i * 10) for i in range(1, 5)]
        chosen = h.ad._choose(h.ad._seg_files())
        check("4 个真实窗口全部保住", all(p in chosen for p in wins), chosen)
        check("满额时空桩让位", stub not in chosen, chosen)
        check("名额不超过上限", len(chosen) <= 4, len(chosen))
    finally:
        h.close()

    h2 = Harness()
    try:
        os.utime(h2.seg, (time.time() - 7200,) * 2)
        stub = make_seg(h2.root, "S-STUB", "stub.jsonl", 0, 5)
        w1 = make_seg(h2.root, "S-W1", "w1.jsonl", 20000, 10)
        w2 = make_seg(h2.root, "S-W2", "w2.jsonl", 18000, 20)
        chosen = h2.ad._choose(h2.ad._seg_files())
        check("有空位时仍接住最新文件", stub in chosen and w1 in chosen and w2 in chosen,
              chosen)
    finally:
        h2.close()


def test_open_turn_kept():
    print("\n=== G. 等授权时旧文件不被踢 ===")
    h = Harness()
    try:
        h.feed(ev("input.prompt.submitted", data={"text_preview": "x"}))
        h.feed(ev("permission.requested", tcid="c1",
                  data={"tool_name": "Bash", "ask_message": "Allow?"}))
        t = time.time() - 2 * 3600
        os.utime(h.seg, (t, t))
        chosen = h.ad._choose(h.ad._seg_files())
        check("还有未结束轮次（等授权）的文件仍在候选", h.seg in chosen, chosen)
        h.ad._rescan()
        check("rescan 也没把它踢掉", h.seg in h.ad.readers, set(h.ad.readers))
        h.feed(ev("turn.finished", data={"reason": "end_turn"}))
        os.utime(h.seg, (t, t))
        check("轮次结束后不再按等授权保留", h.seg not in h.ad._open_paths())
        h.ad.readers[h.seg].last_event_at = 0.0
        check("轮次结束且长期无写入后退出候选",
              h.seg not in h.ad._choose(h.ad._seg_files()))
    finally:
        h.close()

    h2 = Harness()
    try:
        h2.feed(ev("input.prompt.submitted", data={"text_preview": "x"}))
        t2 = h2.ad.turns["T1"]
        t2.last_ts = time.time() - 2 * 3600
        os.utime(h2.seg, (t2.last_ts, t2.last_ts))
        h2.ad.readers[h2.seg].last_event_at = 0.0    # 进程也很久没读到它了
        check("没有待确认项且长期无动作的残局不永久占名额",
              h2.seg not in h2.ad._choose(h2.ad._seg_files()))
    finally:
        h2.close()


def test_idle_then_new_session():
    print("\n=== H. 旧会话全部超窗后仍能接住新会话 ===")
    h = Harness()
    try:
        t = time.time() - 2 * 3600
        os.utime(h.seg, (t, t))
        ad = QwenWorkAdapter({"key": "qwen_work", "roots": [h.root], "exes": [],
                              "install_path": ""}, log=lambda *a: None)
        check("全部超窗时 setup 仍保持挂载（等新会话）", ad.setup(), ad.readers)
        check("超窗的旧会话一个都不读", not ad.readers, set(ad.readers))
        newp = make_seg(h.root, "S-NEW", "new.jsonl", 30000, 0)
        ad._rescan()
        check("新会话一出现就被挂上", newp in ad.readers, set(ad.readers))
        with open(newp, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev("input.prompt.submitted", turn_id="N1",
                                  data={"text_preview": "x"}),
                               ensure_ascii=False) + "\n")
        steps = ad.tick(time.time())
        check("新会话的事件立刻产出", any(x.kind == K_USER for x in steps),
              [x.kind for x in steps])
    finally:
        h.close()


def test_replay_ignores_window():
    print("\n=== I. 自检回放不受活跃窗口限制 ===")
    h = Harness()
    try:
        with open(h.seg, "a", encoding="utf-8") as f:
            for _ in range(30):
                f.write(json.dumps(ev("model.request.started"), ensure_ascii=False) + "\n")
        t = time.time() - 2 * 3600
        os.utime(h.seg, (t, t))
        ad = QwenWorkAdapter({"key": "qwen_work", "roots": [h.root], "exes": [],
                              "install_path": ""}, log=lambda *a: None)
        check("超窗时 setup 不挂任何段", ad.setup() and not ad.readers, set(ad.readers))
        ad.replay(10)
        check("replay 临时挂上超窗段", h.seg in ad.readers, set(ad.readers))
        steps = ad.tick(time.time())
        check("replay 仍能产出步骤", 0 < len(steps) <= 10, len(steps))
        check("回放内容是真事件", any(x.kind == K_THINK for x in steps),
              [x.kind for x in steps])
    finally:
        h.close()


_TEST_EXCLUDE = {"test_real"}


def _test_funcs():
    """自动收集模块里的 test_*，新增用例不会忘记登记。"""
    return [obj for name, obj in sorted(globals().items())
            if name.startswith("test_") and callable(obj)
            and name not in _TEST_EXCLUDE]


def _case(fn):
    def run():
        before = FAIL
        fn()
        if FAIL > before:
            raise AssertionError("%s: %d checks failed"
                                 % (fn.__name__, FAIL - before))
    run.__name__ = fn.__name__
    return unittest.FunctionTestCase(run, description=fn.__name__)


def load_tests(loader, tests, pattern):
    """收进 unittest discover；自定义 check() 的失败转成断言。"""
    return unittest.TestSuite(_case(fn) for fn in _test_funcs())


def main():
    for fn in _test_funcs():
        fn()
    if "--real" in sys.argv:
        test_real()
    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
