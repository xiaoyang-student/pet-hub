# AgentTrace — one tracker for 7 AI office apps

> A unified probe for **WorkBuddy / Qoder / ZCode / Qwen Work / Doubao Work / Doubao / Baidu Dazi** (+ChatGPT).
> **Whatever is installed is tracked automatically.** No manual import, and nothing is installed into the monitored apps.

## 0. Get & start

- **Shipped with PetHub v1.1+**: after installing PetHub, double-click `agent-trace\start-tracer.bat` inside the install folder (it uses PetHub's bundled Python 3.13 — install nothing else); every captured step is pushed to the desktop pet's bubble in real time.
- **Standalone** (this folder): `python trace.py watch --pethub` (requires Python 3.8+).
- Pure Python stdlib, **zero third-party dependencies**; reads the apps' data files only and never writes to them.

---

## 1. What it actually watches (not guesswork)

It does **not** infer state from CPU, memory or window titles. It reads the records **these apps write themselves**:

| App | Real data source (verified on this machine) | What you get | Tier |
|---|---|---|---|
| **WorkBuddy** | `~/.workbuddy/traces/<pid>/trace_*.jsonl` (live span stream)<br>`~/.workbuddy/projects/<dir>/<session>.jsonl` | `span_start` / `span_end` for every tool call (name, input, duration, success/failure), user prompts, assistant answers | **A** |
| **Qoder** | `AppData\Roaming\com.qoder.app.stable\chat-session-turn-payload-buffer.sqlite` (live turn buffer)<br>`main.sqlite` in the same folder | Every step of the turn (text / tool use / thinking / hook events such as PostToolUse), which files changed with added/removed lines | **A** |
| **ZCode** | `~/.zcode/cli/db/db.sqlite` (`session` / `message` / `part` / `tool_usage`)<br>`~/.zcode/v2/`, `~/.zcode/cli/log/` | Sessions, message bodies, parts, tool usage | **A** |
| **Qwen Work** | `~/.qwenworkcn/logs/sessions/<project>/<session>/segments/<run>.jsonl` (the run event stream it writes line by line)<br>`~/.qwenworkcn/logs/runs/<run>/manifest.json` | Prompt received, thinking, tool calls and results, **permission request / resolution**, turn finish, phase success/failure; only segment files written to in the last 30 minutes are followed — idle sessions and stubs don't hold slots | **A** |
| **Doubao Work** | `AppData\Local\DoubaoWork\User Data\sdk_storage\log\saman_*.log` and `saman_frontend_*.log` (its own office-api log; newer builds write lifecycle events in the frontend file) | Session start/end (`session_success` / `agent_error` / `cancel_source`), receiving start/end, `flow_message_time` stages, tool calls/commands (`toolcall` / `command`); duplicate lines for the same event collapse within 10s | **A** |
| **Doubao** | `~/.doubao/chats`, Chromium Local Storage | Conversation text | **B** |
| **Baidu Dazi (DuMate / Qianfan Desktop)** | `%APPDATA%\qianfan-desktop-app\log\desktop-main\AgtEvent.log` (event stream of its embedded opencode agent) | `session.status`, tool parts (`message.part.updated`: name / input / running→completed / duration), answers, reasoning summaries, `todo.updated` plans, `file.edited` | **A** |
| **ChatGPT** | Store edition (MSIX `OpenAI.Codex`) → `~/.codex/logs_2.sqlite`, `thread_history_1.sqlite` (the `thread_items` table, 670+ step records) | Log events + every step of a turn | **A** |

> **What the tiers mean**
> - **A**: structured records written by the app itself — accurate down to "step N, tool X, file Y, +a/-b lines, n seconds".
> - **B**: real conversation text, but without a reliable speaker label.

### Live test on this machine (2026-10-28 / 10-29)

| App | Result | Evidence |
|---|---|---|
| WorkBuddy | **captured live** | Real spans pulled while working: `Edit`, `Bash …(2.164s)`, `tool_done` |
| Qoder | **real history read** | Real prompt "如果我升级客户端，怎么实现不用卸载就可以升级" plus model thinking text |
| ChatGPT | **captured live** | Codex engine logs: real message "先不要管你之前的，先将文档里面的任务执行"; 444 valid steps (`turn_input` / `thread/start` / `thread/resume`) filtered out of 13,925 log rows |
| ZCode | empty DB | `session` / `message` / `part` are 0 rows — installed but never used. Passed a takeover test against **its own schema** ✓ |
| Qwen Work | **live loop verified** | 2026-10-29, two live turns: 16 steps each with no duplicates; a second turn in the same conversation stayed on the same row; `done` left the status bar after ~73s. `agents.db` now holds 1,056 `agent_trajectory_log` rows and 37 `messages` rows, but it is not this adapter's event source |
| Doubao Work | **live loop verified** | 2026-10-29 replay of the 10-28 log: 56 steps, 4 `on_message_session_end` terminals, 0 asks. On the same day, live messages to 2.25.18 produced `user → tool → think → answer → status → result`, 16 steps per turn with no duplicates, and `result` only once from `on_message_session_end.session_success`; ask still has no native evidence |
| Doubao | empty DB | no session files under `~/.doubao` |
| Baidu Dazi | empty DB | window title only |

> **"Empty DB" does not mean "cannot connect".** It means those apps have not actually run a task yet. As soon as they do, the probe attaches within 5 minutes and starts emitting steps.
> To verify right now: run `python tools/realtest.py` — it copies each app's own database to a temp folder, writes samples using **its own columns**, and checks that the probe parses them. Your original files are never touched.
> - **C**: confirms the app is running and active; text comes from the storage layer and may be fragmentary.
>
> Nothing is glossed over. If there is nothing to read, `scan` says so.

---

## 2. Zero-config auto discovery

Four read-only checks on every run:

1. **Registry** uninstall keys (DisplayName + InstallLocation);
2. **Known install dirs**: `D:\`, `%LOCALAPPDATA%\Programs`, `C:\Program Files`, …;
3. **User data dirs**: `~/.workbuddy`, `~/.qoder`, `~/.zcode`, `~/.qwenworkcn`, `~/.doubao`, `AppData\…`;
4. **Process enumeration** (ctypes `EnumProcesses`, no child process spawned).

Any hit counts as "installed". The scan **repeats every 5 minutes** — install a new app and it is picked up without restarting the tracker.

```bash
python trace.py scan
```

---

## 2.1 Lifecycle of one status-bar row

The status bar must never lie, so every row has explicit rules:

| Moment | What you see | Driven by |
| --- | --- | --- |
| A step is running | app name + current step (e.g. `read Program.cs`) | updated on every real event |
| A turn finished | ✅ **Done** (bubble: "the task you gave X is done") | only an explicit turn-finished / session-end from the app; without one, 150s of silence marks the row *stalled*, never done |
| 60s later | the row disappears | the UI keeps done/error rows for 60s; the bridge drops them after 60s too (15s sweep, up to ~75s) |
| App closed | the row disappears within 20s | tracer notices all its processes are gone and sends `remove` — no waiting for the 30-minute TTL |
| App crashed / never emitted | gone within 30 minutes at most | bridge TTL backstop |

**De-duplication**: if an app reports through its own hook *and* through the tracer,
only the **tracer row** is kept (it carries real steps). Its own "needs your approval" /
"error" states still pass through and are shown.

**One row per conversation**: a row is identified by the conversation, not by a tool call
or a single turn. WorkBuddy spans and chat events share the conversation session id; Qwen
Work rows use the session directory id that owns the segment file, so a second or third
turn in the same conversation still occupies exactly one row.

**Only active conversations are followed**: Qwen Work creates a new empty segment file every
2–3 minutes and keeps old conversations on disk forever. The tracker follows segment files
written to in the last 30 minutes and keeps at most the newest stub; a conversation that is
already running or sitting on a permission prompt stays mounted even without new writes.

## 3. Usage

```bash
# 1) What did it find?
python trace.py scan

# 2) Self-test: replay each app's most recent real records
python trace.py selftest --replay

# 3) Track every step (print them)
python trace.py watch

# 4) Log to file + push to the desktop pet
python trace.py watch --out events.jsonl --pethub

# 5) Current status only (app / phase / title / last step)
python trace.py status

# 6) Offline simulation using the apps' real table schemas
python tools/simulate.py
```

Options:

| Flag | Meaning |
|---|---|
| `--lang zh\|en` | Output language (default: Chinese) |
| `--only workbuddy,qoder` | Track only these apps |
| `--out path` | Append to a JSONL file |
| `--pethub` | Push to the PetHub bridge `http://127.0.0.1:8765/api/events` |
| `--seconds N` | Stop after N seconds (for testing) |

Sample output (captured live, not simulated):

```
17:56:43 [workbuddy] tool_done Bash | cd "/…/agent-trace" && rm -f sample-events.jsonl … (2.228s)
17:57:00 [workbuddy] tool_done Bash | sleep 4 && echo "活动1 …" (9.475s)
17:57:07 [workbuddy] tool      TaskOutput | {"task_id": "wmxndv", "full": true}
```

---

## 4. Footprint (deliberately tiny)

| Item | How | Result |
|---|---|---|
| CPU | Single thread + precise sleep until the next due point; no I/O when idle | ≈ 0% |
| Memory | Incremental reads only (new rows by sequence, new bytes by offset); no history cached | **~15–25 MB** |
| Disk | Writes only when you pass `--out` | 0 by default |
| Polling | Active **0.5s** / quiet **3s** / app not running **20s** | adaptive |
| Dependencies | Python standard library only (os / sqlite3 / ctypes / json / urllib) | no pip install |
| Monitored apps | Never written to, never locked, registry untouched, no injection | 0 side effects |

---

## 5. Stability

- Any read failure is **swallowed on the spot**; a probe failing 6 times in a row is **disabled automatically** and the others keep working.
- SQLite is always opened `file:…?mode=ro` with `PRAGMA query_only=1` — **no** `-wal` / `-shm` files are left behind.
- If the target app rewrites or truncates a file, the cursor reopens instead of hanging.
- Files that look like config/cache noise are filtered by filename scoring (`config/cache/server/record/…` are excluded).
- No threads outside the main loop, so no concurrency races.

---

## 6. Layout

```
agent-trace/
  trace.py              CLI entry (scan / selftest / status / watch)
  core/
    discover.py         Auto discovery (registry + disk + user dirs + processes)
    engine.py           Adaptive polling engine
    model.py            Unified event model (Step / Status)
    sinks.py            Output: JSONL / console / desktop pet
  adapters/
    base.py             Read-only helpers (incremental tail / read-only SQLite / string extraction)
    generic.py          Schema-detecting probe (works whatever the table/column names are)
    structured.py       Shared tier-A adapter
    workbuddy.py        WorkBuddy (span stream)
    qoder.py            Qoder (turn buffer + main db)
    zcode.py            ZCode
    qwen_work.py        Qwen Work
    doubao_work.py      Doubao Work (office-api event log, tier A)
    chat_apps.py        Doubao / Baidu Dazi / ChatGPT
  tools/
    simulate.py         Offline simulation self-test
  README.en.md          This file
  README.md             中文版
```

---

## 7. Event fields

```json
{"ts":1793093823.123, "iso":"17:57:03", "app":"workbuddy", "kind":"tool_done",
 "text":"Edit | chat_apps.py (0.42s)", "session":"...", "turn":"...",
 "seq":1024, "tool":"Edit", "ms":420}
```

`kind` values: `user` / `think` / `tool` / `tool_done` / `edit` / `answer` / `result` / `error` / `status`.

---

## 8. Known limits (stated plainly)

1. **Qoder's live buffer is cleared after each turn** — by design. Reading 0 rows while it is idle is **normal**; rows appear the moment it starts a task.
2. **Tier B/C apps** expose storage-layer text, so the speaker cannot always be labelled. For tier-A accuracy, enable each app's own CLI/Hook (below).
3. **ChatGPT** only shows up in `scan` when the desktop app is installed.
4. The tracker reads only **new** activity; it does not replay history into the stream.

---

## 9. Going further: hook into the apps directly

The default external read-only mode is already accurate. To push accuracy to the maximum, enable each app's own mechanism:

| App | Official mechanism | Effect |
|---|---|---|
| WorkBuddy | Hooks (PreToolUse / PostToolUse / Stop) | Reports at the **instant** a step happens, zero delay |
| Qoder | `qodercli` (`~/.qoder-cn/.bin`) + session db | Full event stream for CLI tasks |
| ZCode | `~/.zcode/cli` + `db.sqlite` | Same source as the probe |
| Qwen Work | segment event stream (`logs/sessions/*/*/segments/*.jsonl`) | Same source as the probe |
| Others | Their user data dirs | Same source as the probe |

---

## 10. FAQ

**Q: The self-test shows 0 steps.**
A: Only **new** activity is tracked. Use `python trace.py selftest --replay` to replay recent real records, or run a task in the app and watch.

**Q: Will it slow down my seven apps?**
A: No. It never writes to their files, reads incrementally by offset/sequence, and holds no file locks.

**Q: Apps installed on drive D are detected?**
A: Yes. Besides the registry it also scans `D:\` / `E:\` by main executable name.

**Q: One app never produces data.**
A: Make sure it has run at least once and produced a session; check `python trace.py scan` for the data path.

---

## 11. License

This project is **read-only observation**. It contains no code or data from the monitored apps.
