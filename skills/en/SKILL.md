---
name: pethub-notify
description: Push real-time task progress to the PetHub desktop pet (HUD + pet voice announcements). Use this skill to report status whenever you are running a multi-step task, will take a long time, or need the user's approval/decision — so the user doesn't have to stare at the terminal. Keywords: pethub, desktop pet report, task progress push, notify pet, tell the pet I'm busy.
---

# PetHub desktop-pet reporting

PetHub shows each platform's task status on the desktop. **Report proactively** while you work, so the user can see progress at a glance.

## When to report

| Moment | Command |
|---|---|
| Starting a multi-step task | `say` status=running |
| Milestone progress | `say` status=running --progress N |
| **Need user approval / choice / decision** | `say --ask` (a real event: the pet calls the user) |
| Task finished | `say --done` |
| Error | `say --error` |

## Commands

```bash
PY="python"
CLI="%LOCALAPPDATA%\\PetHub\\connectors\\pethub.py"

# Running
"$PY" "$CLI" say "Refactoring the login module" --platform WorkBuddy --status running --progress 40

# Need a user decision (real event: pet pops a toast and calls out)
"$PY" "$CLI" say "Allow deleting the build/ directory?" --platform WorkBuddy --ask

# Done
"$PY" "$CLI" say "Refactor complete" --platform WorkBuddy --done
```

Optional flags: `--platform name`, `--progress 0-100`, `--message extra note`, `--id task id`.

## MCP tools (if the client supports MCP)

The tool service is registered in `~/.workbuddy/mcp.json` and `~/.qoder/mcp.json` under the name `pethub`:
`pethub_report` / `pethub_ask` / `pethub_done` / `pethub_remove` / `pethub_state`.

## Hard rules

1. **Never fake "needs approval / stuck"**. Only use `--ask` when you are genuinely waiting on a user decision.
2. If reporting fails (bridge not running), skip silently — **never interrupt the main task**.
3. One sentence is enough for the title; don't stuff in large content (title is truncated to 40 chars).
