# PetHub Connectors — Universal Integration Protocol

Anything that can send an HTTP POST can plug in: desktop agents, CI scripts, games, smart-home devices…
The single integration point is the local bridge service: `http://127.0.0.1:8765/api/events`

## Status semantics (this is how the pet and the HUD read them)

| status | Meaning | HUD | Pet reaction |
|---|---|---|---|
| `thinking` | Agent is thinking | dashed animated bar | calm |
| `running` | working (progress optional) | progress bar / indeterminate | calm |
| `waiting` | **waiting for user confirmation** | amber highlight + ❗ above the pet | announces "waiting for you" |
| `done` | finished | green, fades after 60 s | announces "done!", mood +3 |
| `error` | failed | red, fades after 60 s | announces "something broke" |

## 1. Single event (event-driven — recommended for Claude Code / WorkBuddy hooks)

```bash
curl -X POST http://127.0.0.1:8765/api/events \
  -H "Content-Type: application/json" \
  -d '{"platform":"Claude Code","task_id":"sess-1","title":"Fix login bug",
       "status":"running","progress":40,"message":"editing auth.py"}'
```

## 2. Batch sync (polling style: report ALL active tasks for the platform each time; unreported ones are auto-removed)

```bash
curl -X POST http://127.0.0.1:8765/api/events \
  -H "Content-Type: application/json" \
  -d '{"platform":"Codex","type":"sync","tasks":[
        {"task_id":"a","title":"Refactor module","status":"running","progress":70},
        {"task_id":"b","title":"Write tests","status":"waiting","message":"needs approval"}]}'
```

## 3. Remove a task

```bash
curl -X POST http://127.0.0.1:8765/api/events \
  -H "Content-Type: application/json" \
  -d '{"platform":"Codex","type":"remove","task_id":"a"}'
```

Fields: `platform` — platform name (decides HUD grouping and color); `task_id` — unique within the platform;
`progress` — 0–100, shows a flowing animation when omitted; `message` — one-line summary under the progress bar.

---

## 4. Claude Code integration (official hook protocol)

Wire `claude_code_hook.py` into `~/.claude/settings.json`:

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

The hook script reads Claude Code's event JSON from stdin (includes session_id) and maps it to PetHub events.

## 5. WorkBuddy integration

WorkBuddy supports hooks the same way (`~/.workbuddy/settings.json`): same shape as above,
just point the command at `workbuddy_hook.py`. The script reads session events from stdin and reports them.
> Note: back up settings.json before editing; allow the hook inside WorkBuddy.

## 6. Other platforms (Cursor / Codex CLI / Gemini CLI / CI / in-house systems)

Poll their logs or call their APIs, then report via the batch `sync` protocol above.
See `connectors/generic_poller.py` (syncs any JSON file into PetHub every 3 seconds).
