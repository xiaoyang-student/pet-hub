---
name: pethub-notify
description: 把当前任务进度实时推送到桌面宠物 PetHub（HUD + 桌宠播报）。当用户在执行多步骤任务、需要长时间等待、或需要用户授权/拍板时，用本 Skill 上报状态，让用户不必盯着终端。关键词：桌宠上报、任务进度推送、通知桌宠、pethub、告诉桌宠我在忙。
---

# PetHub 桌宠上报

桌宠（PetHub）在桌面上显示各平台的任务状态。你执行任务时**主动上报**，用户抬眼就能看到进度。

## 什么时候上报

| 时机 | 命令 |
|---|---|
| 开始一个有步骤的任务 | `say` status=running |
| 阶段性进展 | `say` status=running --progress N |
| **需要用户授权 / 选择 / 拍板** | `say --ask`（这是真实事件，桌宠会喊人） |
| 任务完成 | `say --done` |
| 出错 | `say --error` |

## 命令

```bash
PY="python"
CLI="%LOCALAPPDATA%\PetHub\connectors\pethub.py"

# 执行中
"$PY" "$CLI" say "正在重构登录模块" --platform WorkBuddy --status running --progress 40

# 需要用户拍板（真实事件：桌宠会弹提示 + 喊人）
"$PY" "$CLI" say "是否允许删除 build/ 目录" --platform WorkBuddy --ask

# 完成
"$PY" "$CLI" say "重构完成" --platform WorkBuddy --done
```

可选参数：`--platform 名字`、`--progress 0-100`、`--message 补充说明`、`--id 任务id`。

## MCP 工具（若客户端支持 MCP）

工具服务已注册在 `~/.workbuddy/mcp.json` 与 `~/.qoder/mcp.json`，名为 `pethub`：
`pethub_report` / `pethub_ask` / `pethub_done` / `pethub_remove` / `pethub_state`。

## 硬规则

1. **绝不伪造"要授权/卡住了"**。只有你真的在等用户决定时才用 `--ask`。
2. 上报失败（桥接未运行）时静默跳过，**绝不打断主任务**。
3. 一句话说清在做什么，别把大段内容塞进去（title 会被截断到 40 字）。
