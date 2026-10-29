# -*- coding: utf-8 -*-
"""统一事件模型。

所有适配器把各自私有格式归一化成 Step / Status，
上层（引擎、输出、桌宠）只认这两个结构。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

# ---- 步骤类型（归一化后的“每一步”）-------------------------------
K_USER = "user"            # 用户下达任务
K_PLAN = "plan"            # 计划 / 待办
K_THINK = "think"          # 模型思考
K_TOOL = "tool"            # 工具调用（含命令、读写、搜索）
K_TOOL_DONE = "tool_done"  # 工具返回
K_EDIT = "edit"            # 文件改动
K_ANSWER = "answer"        # 文本回答（可流式累积）
K_RESULT = "result"        # 本轮结束 / 结果（必须有"结束"的真实证据）
K_ERROR = "error"          # 出错
K_STATUS = "status"        # 状态变化
K_ASK = "ask"              # 需要用户拍板 / 授权 / 选择（必须有真实的询问事件）
K_DENIED = "denied"        # 用户拒绝了本次授权
K_STALLED = "stalled"      # 长时间无新动作，但没有任何"已结束"的证据
K_APP_EXIT = "app_exit"    # 软件进程已退出（撤销状态栏的幽灵任务）

# ---- 应用阶段（用于一眼看清“在干嘛”）-----------------------------
P_IDLE = "idle"            # 空闲
P_RECEIVED = "received"    # 已接题，尚未开始推理
P_THINKING = "thinking"    # 思考中
P_TOOL = "tool"            # 执行工具
P_EDITING = "editing"      # 改文件
P_ANSWERING = "answering"  # 输出回答
P_ASKING = "asking"        # 等用户拍板
P_DENIED = "denied"        # 授权被拒
P_DONE = "done"            # 完成
P_STALLED = "stalled"      # 疑似停滞（绝不等同于完成）
P_ERROR = "error"

# 阶段 -> 中文 / English
PHASE_TEXT = {
    P_IDLE: ("空闲", "Idle"),
    P_RECEIVED: ("已接题", "Prompt received"),
    P_THINKING: ("思考中", "Thinking"),
    P_TOOL: ("执行工具", "Running tool"),
    P_EDITING: ("改文件", "Editing files"),
    P_ANSWERING: ("回答中", "Answering"),
    P_ASKING: ("等你拍板", "Needs your call"),
    P_DENIED: ("已拒绝授权", "Permission denied"),
    P_DONE: ("已完成", "Done"),
    P_STALLED: ("疑似停滞", "Possibly stalled"),
    P_ERROR: ("出错", "Error"),
}

# 步骤类型 -> 阶段。适配器 emit 之后由 Adapter._note 统一推进，
# 免得每个适配器各写一遍映射（漏写就会出现"状态永远停在 idle"）。
PHASE_OF_KIND = {
    K_USER: P_RECEIVED,
    K_PLAN: P_THINKING,
    K_THINK: P_THINKING,
    K_TOOL: P_TOOL,
    K_TOOL_DONE: P_TOOL,
    K_EDIT: P_EDITING,
    K_ANSWER: P_ANSWERING,
    K_ASK: P_ASKING,
    K_DENIED: P_DENIED,
    K_RESULT: P_DONE,
    K_STALLED: P_STALLED,
    K_ERROR: P_ERROR,
}


@dataclass
class Step:
    """一次真实发生的动作。"""
    ts: float                    # 时间戳（秒，epoch）
    app: str                     # 来源应用 key
    kind: str                    # 见 K_*
    text: str = ""               # 人可读摘要
    session: str = ""            # 会话 ID
    turn: str = ""               # 轮次 ID
    seq: int = 0                 # 应用内序号，用于去重
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = round(self.ts, 3)
        d["iso"] = time.strftime("%H:%M:%S", time.localtime(self.ts))
        if not d["meta"]:
            d.pop("meta")
        return d


@dataclass
class Status:
    """应用当前状态快照（供 HUD / 面板显示）。"""
    app: str = ""
    phase: str = P_IDLE
    title: str = ""             # 会话/任务标题
    detail: str = ""            # 最近一步摘要
    progress: int = -1          # 0-100；-1 表示未知（默认未知，不与"0%"混淆）
    steps: int = 0              # 本轮已产生步骤数
    active: bool = False        # 进程是否在跑
    updated: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["updated"] = round(self.updated, 3)
        return d


def short(s: str, n: int = 160) -> str:
    """把长文本压成单行摘要，避免内存和日志膨胀。"""
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def first_line(s: str, n: int = 160) -> str:
    if not s:
        return ""
    for ln in str(s).splitlines():
        if ln.strip():
            return short(ln, n)
    return short(s, n)
