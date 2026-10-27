# PetHub 连接器 —— 任意平台接入协议

任何能发 HTTP POST 的东西都能接入：桌面 Agent、CI 脚本、游戏、智能家居……
接入点统一是本机桥接服务：`http://127.0.0.1:8765/api/events`

## 状态语义（宠物和进度条都按这个理解）

| status | 含义 | HUD 表现 | 宠物反应 |
|---|---|---|---|
| `thinking` | Agent 在思考 | 虚线动画条 | 平静 |
| `running` | 执行中（progress 可选） | 进度条 / 不定进度 | 平静 |
| `waiting` | **等用户确认** | 琥珀色高亮 + 宠物头顶 ❗ | 播报"在等你确认" |
| `done` | 完成 | 绿色，60 秒后消失 | 播报"完成啦"，心情 +3 |
| `error` | 出错 | 红色，60 秒后消失 | 播报"出错了" |

## 1. 单事件上报（事件驱动型，推荐 Claude Code / WorkBuddy 的 Hook 用）

```bash
curl -X POST http://127.0.0.1:8765/api/events \
  -H "Content-Type: application/json" \
  -d '{"platform":"Claude Code","task_id":"sess-1","title":"修复登录bug",
       "status":"running","progress":40,"message":"编辑 auth.py"}'
```

## 2. 整批同步（轮询型：每次上报该平台的全部活跃任务，未上报的自动移除）

```bash
curl -X POST http://127.0.0.1:8765/api/events \
  -H "Content-Type: application/json" \
  -d '{"platform":"Codex","type":"sync","tasks":[
        {"task_id":"a","title":"重构模块","status":"running","progress":70},
        {"task_id":"b","title":"写测试","status":"waiting","message":"需要确认"}]}'
```

## 3. 删除任务

```bash
curl -X POST http://127.0.0.1:8765/api/events \
  -H "Content-Type: application/json" \
  -d '{"platform":"Codex","type":"remove","task_id":"a"}'
```

字段说明：`platform` 平台名（决定 HUD 分组与颜色）；`task_id` 同平台内唯一；
`progress` 0–100，缺省时进度条显示流动动画；`message` 一行摘要，显示在进度条下方。

---

## 4. Claude Code 接入（官方 Hook 协议）

把 `claude_code_hook.py` 配到 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "PostToolUse":  [{"hooks": [{"type": "command", "command": "python C:/.../pet-hub/connectors/claude_code_hook.py running"}]}],
    "Notification": [{"hooks": [{"type": "command", "command": "python C:/.../pet-hub/connectors/claude_code_hook.py waiting"}]}],
    "Stop":         [{"hooks": [{"type": "command", "command": "python C:/.../pet-hub/connectors/claude_code_hook.py done"}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "python C:/.../pet-hub/connectors/claude_code_hook.py done"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python C:/.../pet-hub/connectors/claude_code_hook.py start"}]}]
  }
}
```

Hook 脚本从 stdin 读取 Claude Code 的事件 JSON（含 session_id），映射为 PetHub 事件。

## 5. WorkBuddy 接入

WorkBuddy 同样支持 hooks（配置位于 `~/.workbuddy/settings.json`），方式与上面完全一致：
把命令换成 `workbuddy_hook.py` 即可，脚本读取 stdin 的会话事件并上报。
> 注意：修改 settings.json 前备份；在 WorkBuddy 内允许该 hook。

## 6. 其他平台（Cursor / Codex CLI / Gemini CLI / CI / 自研系统）

轮询它们的日志或调用其 API，然后整理成上面的 `sync` 整批协议上报即可。
参考 `connectors/generic_poller.py`（每 3 秒把任意 JSON 文件同步到 PetHub）。
