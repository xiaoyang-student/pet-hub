# AgentTrace（七合一 AI 办公软件逐步追踪器）

> 给 **WorkBuddy / Qoder / ZCode / 千问办公 / 豆包工作 / 豆包 / 百度搭子**（+ChatGPT）七款 AI 办公软件做的统一探针。
> **装了哪款就自动挂哪款**，不需要手工导入、不需要在被监控的软件里装任何东西。

## 0. 获取与启动

- **随 PetHub v1.1+ 分发**：安装 PetHub 后，双击安装目录里的 `agent-trace\start-tracer.bat`（自动用 PetHub 自带的 Python 3.13，无需额外安装），追踪到的每一步实时推送到桌宠气泡。
- **独立运行**（本目录）：`python trace.py watch --pethub`（前提：装了 Python 3.8+）。
- 纯 Python 标准库，**零第三方依赖**；只读各软件的数据文件，绝不写入。

---

## 1. 它到底在看什么（不是猜的）

追踪的**不是 CPU、不是内存、不是窗口标题猜状态**，而是这些软件**自己写下来的记录**：

| 软件 | 真实数据源（本机实测路径） | 拿到的内容 | 精度 |
|---|---|---|---|
| **WorkBuddy** | `~/.workbuddy/traces/<pid>/trace_*.jsonl`（实时 span 流）<br>`~/.workbuddy/projects/<目录>/<会话>.jsonl` | 每一次工具调用的 `span_start` / `span_end`（名字、入参、耗时、成功失败）、用户提问、助手回答 | **A 级** |
| **Qoder** | `AppData\Roaming\com.qoder.app.stable\chat-session-turn-payload-buffer.sqlite`（当前回合实时缓冲）<br>同目录 `main.sqlite` | 回合内每一步（文本 / 工具调用 / 思考 / hook 事件 PostToolUse）、改了哪些文件及增删行数 | **A 级** |
| **ZCode** | `~/.zcode/cli/db/db.sqlite`（`session` / `message` / `part` / `tool_usage`）<br>`~/.zcode/v2/`、`~/.zcode/cli/log/` | 会话、消息正文、部件、工具用量 | **A 级** |
| **千问办公** | `~/.qwenworkcn/logs/sessions/<项目>/<会话>/segments/<run>.jsonl`（它自己逐条写的运行事件流）<br>`~/.qwenworkcn/logs/runs/<run>/manifest.json` | 接题、思考、工具调用与返回、**授权请求 / 选择结果**、轮次结束、阶段成功/失败；只跟最近 30 分钟有写入的段文件，长期不用的会话和空桩不占名额 | **A 级** |
| **豆包工作** | `AppData\Local\DoubaoWork\User Data\sdk_storage\log\saman_*.log` 与 `saman_frontend_*.log`（它自己的 office-api 事件日志；新版会话事件在 frontend 文件） | 消息会话开始/结束（`session_success`/`agent_error`/`cancel_source`）、接收开始/结束、`flow_message_time` 细粒度阶段、工具调用/命令（`toolcall`/`command`）；同一事件重复写行按 10s 语义去重 | **A 级** |
| **豆包** | `~/.doubao/chats`、Chromium Local Storage | 对话文本 | **B 级** |
| **百度搭子(DuMate / 千帆桌面)** | `%APPDATA%\qianfan-desktop-app\log\desktop-main\AgtEvent.log`（内嵌 opencode 智能体的事件流） | `session.status`、`message.part.updated` 的 tool（工具名 / 入参 / running→completed / 耗时）、回答正文、思考摘要、`todo.updated` 计划、`file.edited` 产物落盘 | **A 级** |
| **ChatGPT** | 商店版（MSIX 包 `OpenAI.Codex`）→ `~/.codex/logs_2.sqlite`、`thread_history_1.sqlite`（`thread_items` 表 670+ 条逐步记录） | 日志事件 + 会话每一步（turn / item） | **A 级** |

> **精度等级怎么理解**
> - **A 级**：软件自己写的结构化记录，能精确到「第几步、调了什么工具、改了哪个文件、增删多少行、耗时多少」。
> - **B 级**：读到了真实对话文本，但没有严格区分「谁说的」。
> - **C 级**：能确认软件在运行、窗口在活动，文本来自存储层，可能有碎片段落。
>
> 文档不做任何美化：抓不到就是抓不到，会在 `scan` 里如实显示。

### 2026-10-28 / 10-29 本机实测结果（真跑了一遍）

| 软件 | 实测状态 | 证据 |
|---|---|---|
| WorkBuddy | **实时抓到** | 边操作边抓出 `Edit`、`Bash …(2.164s)`、`tool_done` 等真实 span |
| Qoder | **读到真实历史** | 抓出真实提问「如果我升级客户端，怎么实现不用卸载就可以升级」及模型思考正文 |
| ChatGPT | **实时抓到** | Codex 引擎日志：真实消息「先不要管你之前的，先将文档里面的任务执行」、`turn_input` / `thread/start` / `thread/resume` 共 444 条有效步骤（从 13925 行日志里筛出） |
| ZCode | 库是空的 | `db.sqlite` 的 session/message/part 全 0 行——装了但没用过。已用**它自己的表结构**做接管测试，注入即抓到 ✓ |
| 千问办公 | **实时闭环已验证** | 2026-10-29 实机两轮：每轮 16 步、无重复；同一会话的第二轮仍落在同一行；`done` 约 73 秒后从状态栏退场。`agents.db` 现在有 1056 行 `agent_trajectory_log`、37 行 `messages`，但它不是本适配器的事件源 |
| 豆包工作 | **实时闭环已验证** | 2026-10-29 回放 10-28 日志：56 步、4 条 `on_message_session_end` 终态、0 ask。同日在 2.25.18 实机连发消息：`user → tool → think → answer → status → result`，每轮 16 步无重复，`result` 仅在 `on_message_session_end.session_success` 出现一次；ask 仍无原生证据 |
| 豆包 | 库是空的 | `~/.doubao` 无会话文件 |
| 百度搭子 | **实时抓到** | 它其实是「千帆桌面应用 + 内嵌 opencode」，事件流在 `AgtEvent.log`：抓到 `read Program.cs`、`read uninstall.ps1`、思考摘要「验证安装程序安全项」、`开始处理` 等真实步骤（回放 10 步） |

> **「库是空的」不是接不上**，是这些软件还没真正跑过任务。它们一开始干活，探针 5 分钟内自动挂上并开始输出。
> 想立刻验证：跑 `python tools/realtest.py`（复制软件自己的库到临时目录、按它自己的字段写样本、看探针能否解析——全程不碰你的原始文件）。

---

## 2. 自动发现（零配置）

运行时会同时做四件事来认机器上的软件，全部只读：

1. **扫注册表**卸载项（DisplayName + InstallLocation）；
2. **扫预设目录**：`D:\`、`%LOCALAPPDATA%\Programs`、`C:\Program Files` 等；
3. **扫用户数据目录**：`~/.workbuddy`、`~/.qoder`、`~/.zcode`、`~/.qwenworkcn`、`~/.doubao`、`AppData\…`；
4. **枚举进程**（ctypes 直调 `EnumProcesses`，不启子进程）。

命中任意一条即认定「已安装」。之后**每 5 分钟重扫一次**——新装一款，不用重启追踪器就自动挂上。

```bash
python trace.py scan
```

---

## 2.1 一条任务的生命周期（什么时候出现 / 变完成 / 消失）

状态栏“不说谎”是硬要求，所以每一行的生老病死都有明确规则：

| 时刻 | 状态栏显示 | 触发方式 |
| --- | --- | --- |
| 软件正在做某一步 | 平台名 + 当前这一步（如 `read Program.cs`） | 读到一条真实事件就更新一次 |
| 一轮任务做完了 | ✅ **已完成**（气泡：“交给 X 的任务完成啦”） | 只认应用自己报的“回合完成 / 会话结束”；没等到终态时，追踪器在 150 秒无新动作后只标“疑似停滞”，不报完成 |
| 完成后 60 秒 | 这一行自动消失 | 前端把已完成 / 报错保留 60 秒；桥接侧同样按 60 秒清理（每 15 秒巡检，最迟约 75 秒） |
| 软件被关掉 | 这一行 20 秒内消失 | 追踪器发现该软件进程全部退出 → 主动发 remove，不等 30 分钟超时 |
| 软件崩了 / 一条步骤都没发过 | 最多 30 分钟后消失 | 桥接 TTL 兜底 |

**去重**：同一款软件如果既有它自己的 hook 在报、又有追踪器在报，**只留追踪器那一行**
（带真实步骤）。但它自己 hook 报的“等你确认 / 出错”仍然会透传显示，不会丢。

**行按会话合并**：状态栏一行对应一个会话，不对应一次工具调用或一轮问答。WorkBuddy 的 span 和对话事件统一落到对话会话 ID；千问办公按段文件所属的会话目录 ID 落行，同一会话里第二轮、第三轮仍然只占一行。

**只跟活跃会话**：千问办公每 2~3 分钟就会新建一个空段文件，历史会话也会长期留在磁盘上。追踪器只跟最近 30 分钟有写入的段文件，空桩最多保留最新的一个；已经开跑或正卡在授权弹窗上的会话即使长时间没有新写入也会留着，不会被时间窗踢掉。

## 3. 用法

```bash
# 1) 看看认出了哪几款
python trace.py scan

# 2) 通电自检：重放每款最近的真实记录，确认探针读得懂
python trace.py selftest --replay

# 3) 开始逐步追踪（打印每一步）
python trace.py watch

# 4) 落盘 + 推给桌宠
python trace.py watch --out events.jsonl --pethub

# 5) 只看当前状态（软件 / 阶段 / 标题 / 最近一步）
python trace.py status

# 6) 离线仿真自检：用这些软件的真实表结构造样本，验证解析逻辑
python tools/simulate.py
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--lang zh\|en` | 输出语言（默认中文） |
| `--only workbuddy,qoder` | 只盯指定的几款 |
| `--out 文件路径` | 追加写 JSONL |
| `--pethub` | 推送到 PetHub 桌宠桥 `http://127.0.0.1:8765/api/events` |
| `--seconds N` | 跑 N 秒后自动结束（自测用） |

输出示例（真实抓取，非模拟）：

```
17:56:43 [workbuddy] tool_done Bash | cd "/…/agent-trace" && rm -f sample-events.jsonl … (2.228s)
17:57:00 [workbuddy] tool_done Bash | sleep 4 && echo "活动1 …" (9.475s)
17:57:07 [workbuddy] tool      TaskOutput | {"task_id": "wmxndv", "full": true}
```

---

## 4. 资源占用（刻意做得很轻）

| 项 | 做法 | 结果 |
|---|---|---|
| CPU | 单线程 + 精确睡眠（睡到下一个到期点），空闲时不做任何 I/O | ≈ 0% |
| 内存 | 只读增量（按序号取新行 / 按字节偏移取新内容），不缓存历史 | 约 **15–25 MB** |
| 磁盘 | 只有你显式 `--out` 时才写 | 0（默认） |
| 轮询节奏 | 活跃 **0.5s** / 安静 **3s** / 软件没运行 **20s** | 自适应 |
| 无依赖 | 全部 Python 标准库（os / sqlite3 / ctypes / json / urllib） | 无需 pip install |
| 被监控软件 | 绝不写入、不锁定、不改注册表、不注入进程 | 0 副作用 |

---

## 5. 稳定性设计

- 任何一次读取失败都**就地吞掉**，连续失败 6 次的探针**自动停用**，其余几款不受影响；
- SQLite 一律以 `file:…?mode=ro` 打开 + `PRAGMA query_only=1`，**不会**在对方目录里留下 `-wal` / `-shm`；
- 目标软件正在写、文件被重建/截断 → 自动重开游标，不会卡死；
- 数据源文件被配置/缓存噪音污染时，按文件名打分淘汰（`config/cache/server/record/…` 一律排除）；
- 主循环外没有线程，不存在并发竞争。

---

## 6. 目录结构

```
agent-trace/
  trace.py              命令行入口（scan / selftest / status / watch）
  core/
    discover.py         自动发现（注册表 + 磁盘 + 用户目录 + 进程）
    engine.py           自适应轮询引擎
    model.py            统一事件模型（Step / Status）
    sinks.py            输出：JSONL / 控制台 / 桌宠
  adapters/
    base.py             通用只读工具（增量尾读 / 只读 SQLite / 取串）
    generic.py          自动识表探针（表名列名叫什么都能认）
    structured.py       A 级通用适配器
    workbuddy.py        WorkBuddy（span 流）
    qoder.py            Qoder（回合缓冲 + 主库）
    zcode.py            ZCode
    qwen_work.py        千问办公
    doubao_work.py      豆包工作（office-api 事件日志，A 级）
    chat_apps.py        豆包 / 百度搭子 / ChatGPT
  tools/
    simulate.py         离线仿真自检
  README.md             本文件
  README.en.md          English version
```

---

## 7. 事件字段

```json
{"ts":1793093823.123, "iso":"17:57:03", "app":"workbuddy", "kind":"tool_done",
 "text":"Edit | chat_apps.py (0.42s)", "session":"...", "turn":"...",
 "seq":1024, "tool":"Edit", "ms":420}
```

`kind` 取值：`user`（用户下达）/`think` / `tool`（开始执行工具）/`tool_done`（工具返回）/`edit`（文件改动）/`answer` / `result`（本轮结束）/`error` / `status`。

---

## 8. 已知边界（如实说明）

1. **Qoder 的实时缓冲每回合结束会清空**——这是它的设计。空闲时读到 0 条是**正常的**，一旦它开始跑任务，缓冲里会立刻出现逐步记录。
2. **B/C 级软件**读到的是存储层文本，无法 100% 区分说话人；想要 A 级精度，可在这些软件里启用它们各自的 CLI/Hook（见下）。
3. **ChatGPT 桌面版未安装**时不会出现在 `scan` 结果里，装上后会自动出现。
4. 探针只读**新发生**的内容，不会回灌历史。

---

## 9. 想更进一步：在软件内部直接挂钩子

追踪器默认是「外部只读」，已经足够真实。若想把精度拉到最高，可在各软件内部启用它们自己的机制：

| 软件 | 官方机制 | 效果 |
|---|---|---|
| WorkBuddy | Hooks（PreToolUse / PostToolUse / Stop） | 每一步**触发瞬间**上报，零延迟 |
| Qoder | `qodercli`（`~/.qoder-cn/.bin`）+ 会话库 | 可拿到 CLI 任务的完整事件流 |
| ZCode | `~/.zcode/cli` + `db.sqlite` | 与探针同源 |
| 千问办公 | 段文件事件流（`logs/sessions/*/*/segments/*.jsonl`） | 与探针同源 |
| 其余 | 各自的用户数据目录 | 与探针同源 |

---

## 10. 常见问题

**Q：为什么自检是 0 步？**
A：探针只跟踪**新发生**的动作。用 `python trace.py selftest --replay` 可以重放最近的真实记录；或直接在对应软件里跑一个任务再观察。

**Q：会拖慢我这七款软件吗？**
A：不会。探针从不写入它们的文件，只读自己的副本视角（按偏移/序号增量读），且不持有文件锁。

**Q：装在 D 盘的软件能认出来吗？**
A：能。除了注册表，还会在 `D:\` / `E:\` 等常见位置按主程序名兜底扫描。

**Q：某款软件一直没有数据？**
A：先确认它至少完整运行并产生过一次会话；`python trace.py scan` 看数据源路径是否正确。

---

## 11. 许可

本项目只做**只读观测**，不包含任何被监控软件的代码或数据。
