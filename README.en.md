# PetHub · Desktop Pet (English)

<div align="right">

[![lang](https://img.shields.io/badge/lang-%E4%B8%AD%E6%96%87-blue)](README.md)

</div>

A little pet that lives on your desktop and keeps an eye on what Qoder, WorkBuddy, Doubao… are doing.
**Everything it says is reported by the apps themselves — the pet never guesses and never makes things up.**

> 中文版见 [README.md](README.md)

---

## 0. Download

Prefer not to build it yourself? Grab the installer:

**[↓ PetHub-Setup-v1.2.1.exe](https://github.com/xiaoyang-student/pet-hub/releases/latest) (130 MB — double-click to install)**

There is also a portable `PetHub-Setup-v1.2.1.zip` (75 MB — unzip and run). Both ship their own Python
runtime and need no administrator rights.

> **New in v1.1**: the **AI status tracker** (`agent-trace\`) — follows all 8 AI office apps step by
> step; the uninstaller was rewritten from scratch (stop tasks → delete everything → clean the
> registry, without touching any of your other software).
>
> **New in v1.2**: one app gets exactly one status row; finished rows retire in 60s;
> Qwen Work now reads its real event stream (the old probe read an empty database and
> reported 0 steps); "150s idle" is no longer reported as finished.
>
> **New in v1.2.1**: nothing is assumed to live in a fixed place — the install
> directory is selectable (`-Dir`), uninstall locates the real directory via
> process / registry / shortcuts and requires a PetHub fingerprint match, and the
> tracked AI apps are found by enumerating this machine's actual drives instead of
> hardcoded `D:\` paths. A running app is located straight from its process image.
>
> Full history: [Releases](https://github.com/xiaoyang-student/pet-hub/releases).

> Repo owner: after publishing a Release, replace `xiaoyang-student/pet-hub` above with your real repository path.

---

## 1. Install

**Option A: Windows installer (recommended)** — download `PetHub-Setup.exe` and **double-click it**; a graphical wizard walks you through it:

- pick the install folder, toggle desktop shortcut / launch at Windows startup, with a progress bar;
- leaves a Desktop + Start Menu entry, plus an "Uninstall PetHub.cmd" in the install folder;
- run with no arguments for the wizard, or install silently from the command line (bulk deploy, re-install):

```powershell
PetHub-Setup.exe                                        # graphical wizard
PetHub-Setup.exe --silent --dir="D:\Apps\PetHub"       # silent, chosen folder
PetHub-Setup.exe --silent --dir="D:\Apps\PetHub" --autostart
```

Without `--dir` it installs to `%LOCALAPPDATA%\PetHub`. No administrator rights required.

**Option B: install script** — when you want to decide exactly what gets installed (MCP server, reporting skill):

```powershell
cd installer
powershell -ExecutionPolicy Bypass -File install.ps1
```

The script: installs to `%LOCALAPPDATA%\PetHub`, registers the MCP tool server, installs the
`pethub-notify` skill, adds auto-start and a desktop shortcut.
Flags: `-NoAutoStart`, `-NoShortcut`.

Uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1          # keeps the pet's save data
powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Purge   # removes everything
```

**Requirements**: nothing to install. The installer ships its own Python 3.13 runtime (`runtime\python\`), used by the bridge, the app watcher and the MCP server — it never touches your system PATH and needs no admin rights. The only thing the pet itself needs is the **WebView2 runtime** (bundled with Win10 1803+ / Win11); if it is missing, the installer downloads and installs it silently (~1 min, needs internet).

---

## 2. Layout

```
pet-hub/
├─ app/index.html          The pet: rendering, HUD, all interaction logic
├─ bridge/server.py        Bridge service (127.0.0.1:8765) — collects events, feeds the pet
├─ agent-trace/            AI status tracker (new in v1.1)
│   ├─ trace.py            Entry point: scan / watch / selftest
│   ├─ start-tracer.bat    Double-click start (uses the bundled Python, feeds the pet)
│   ├─ core/               Discovery + engine + noise filtering
│   ├─ adapters/           Probes for the 8 apps (WorkBuddy/Qoder/ZCode/Qwen/Doubao family/Dazi/ChatGPT)
│   └─ tools/              realtest.py (takeover test on real schemas), simulate.py
├─ connectors/             Integration toolkit
│   ├─ pethub.py           CLI: install/remove integrations, status, one-line reporting
│   ├─ pethub_mcp.py       MCP tool server (recommended — any MCP client can call it)
│   ├─ universal_hook.py   Generic hook script (Claude Code / WorkBuddy / Qoder …)
│   └─ observer.py         Zero-integration observer (process & file activity fallback)
├─ light/                  Windows shell (.NET 10 + WebView2): transparency & click-through
├─ installer/              install.ps1 / uninstall.ps1 / skill
└─ README.md / README.en.md
```

Start the bridge (`python bridge/server.py`) first, then `PetHubLight.exe`.
The shell starts the bridge itself if it isn't running.

### AI status tracker (new in v1.1, agent-trace\)

Want the pet bubble to show what **8 AI office apps** (WorkBuddy / Qoder / ZCode / Qwen Work /
Doubao / Doubao Work / Baidu Dazi / ChatGPT) are doing, step by step? Just double-click:

```
agent-trace\start-tracer.bat      (uses PetHub's bundled Python — nothing to install)
```

It **never guesses**: it reads the records those apps write themselves (trace span streams, SQLite
session databases, engine logs), auto-detects which ones are installed and follows those.
See `agent-trace\README.md`.

---

## 3. Five ways to connect (pick any)

Design rule: **let the app speak for itself** instead of us guessing what it's doing.

### ① MCP tools (recommended)

The installer registers it as `pethub` in each client's config (`~/.workbuddy/mcp.json`, `~/.qoder/mcp.json`, …).

| Tool | Purpose |
|---|---|
| `pethub_report` | Report `running / thinking / waiting / done / error` |
| `pethub_ask` | **Real event**: I'm asking the user / need approval / need a decision |
| `pethub_done` | Mark finished (pet mood +3) |
| `pethub_remove` | Remove a task |
| `pethub_state` | Query what the pet is currently showing |

### ② CLI (scripts, CI, anything that can run a command)

```bash
python connectors/pethub.py say "Refactoring login" --platform Qoder --status running --progress 40
python connectors/pethub.py say "Allow deleting build/?" --platform Qoder --ask    # needs approval
python connectors/pethub.py say "Refactor done" --platform Qoder --done
```

### ③ Hooks (Claude Code / WorkBuddy / Qoder …)

```bash
python connectors/pethub.py list          # which platforms are on this machine
python connectors/pethub.py install auto  # hook up everything detected
python connectors/pethub.py remove <name>
```

### ④ HTTP endpoint (anything that can POST — this is the one AI office apps use)

```bash
curl.exe --noproxy "*" -s -X POST http://127.0.0.1:8765/api/events \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"Doubao\",\"task_id\":\"job-1\",\"title\":\"Weekly report\",\"status\":\"running\",\"progress\":30}"
```

Six fields, nothing else: `platform` (who's working), `task_id` (stable ID — posting again updates the same
task), `title`, `status` (`running / thinking / waiting / done / error`), `progress` (0-100, optional),
`message`. Batch sync: `{"type":"sync","tasks":[...]}`. Remove: `{"type":"remove","task_id":"..."}`.

> **Three traps that make it look broken**
> - Use `curl.exe`, not `curl`: in PowerShell `curl` is an alias for `Invoke-WebRequest` and rejects every flag.
> - Keep `--noproxy "*"`: behind Clash / V2Ray, requests to 127.0.0.1 get routed through the proxy and return 502.
> - PowerShell 5.1 eats the double quotes inside JSON (`{"a":"b"}` arrives as `{a:b}`) and you get
>   `Expecting property name enclosed in double quotes`. The server now repairs that automatically, so the
>   command above still works; for a strict payload, wrap the JSON in single quotes: `-d '{"platform":"..."}'`.

**Don't want to hand-assemble it?** Tray → "给 AI 软件接入…" (Connect an AI app) gives you a **prompt you can
paste straight into Doubao / Baidu Dazi / ChatGPT**, a ready-made command, the MCP snippet, and a "send a test"
button — one click and the pet speaks, so you know instantly whether it works.

Machine-readable spec: `GET /api/help` (JSON, for AI). Human page: `http://127.0.0.1:8765/help`.

### ⑤ Import an app (Doubao, Baidu Dazi — anything with no local API)

Let's be straight about this: Doubao, Baidu Dazi and the ChatGPT desktop app **expose no local plugin or MCP entry point**, so there is no "install a connector inside it". What they're doing, and how far along they are, simply isn't reachable from outside.

So we flip it: **PetHub watches them from outside.**

Tray icon → `接入应用…` (Import app) → pick its exe (multi-select). **One click opens the file picker** — no submenu. Nothing else to configure:

| When | What you see |
|---|---|
| App just started / CPU moving / window title changed | Task appears instantly, titled "App · current window title" |
| 22 seconds of silence | Marked done |
| Another minute of silence | Removed from the list |
| App closed | Task disappears — no fake "still running" |

The only signals are **process alive, CPU moving, title changing**, which is why it works for *any* app. The trade-off is no precise percentage — for "40% · waiting for you" granularity the app still has to speak for itself (①②④).

The list lives in `%LOCALAPPDATA%\PetHub\apps.json`; scripts and installers can register silently:

```bash
PetHubLight.exe --watch-app "C:\Program Files\Doubao\Doubao.exe"
```

The tray entry "已接入 N 个" lets you enable / disable / clear the list at any time; it never affects the pet itself.

### Truthfulness rules (important)

- **"Needs approval / stuck" only comes from the app's own report** (`pethub_ask`, `--ask`, or a hook carrying `ask`).
- With no signal, the pet states facts only, e.g. "Qoder has been quiet for 7 minutes" — it will **not** turn that into "waiting for you".
- A task with no updates for **30 minutes** is treated as lost contact and removed, so nothing keeps claiming "still running"; **done / error** rows do not wait that long — they leave after 60 seconds (the bridge sweeps every 15s, worst case ~75s).
- **One row per conversation**: several turns in the same conversation, and WorkBuddy tool spans, merge into that single row instead of one row per turn.

---

## 4. Everyday use

| Action | Result |
|---|---|
| Left-click the pet | Interact / talk |
| Right-click the pet | Menu: Feed / Pet / Play / Clean / Sleep / Vet / Language (that's it — no more piling up). **With several pets, the menu acts on the one you right-clicked**; "🐾 Next pet" only shows when you have a single pet |
| Double-click the pet | Switch layer: **always-on-top** ↔ **desktop layer** |
| Click a mess on the ground | Sweeps it up — no menu entry needed |
| Drag the pet past the screen edge and let go | It leaves home and **doesn't come back** |
| **Drop it at the very top** of the screen | It grabs the ceiling, shuffles twice, then climbs down the wall |
| Click the desktop shortcut again | **One more pet** (max 6; still a single taskbar icon) |
| Click a task in the HUD | The pet repeats that task |
| Drag the HUD header | Move it |
| "Hide ▸" | Collapse to a small paw (the icon matches the current pet) |
| Left-click the tray dot | Expand / collapse the task panel |
| Right-click the tray dot | Task list + `Import app…` (pick an exe) + `Connect an AI app…` (prompt & self-test) + show pet / switch layer / quit |

**Language**: right-click → `🌐 中文 / English`. UI, statuses, pet lines and bubble bylines **all switch together** — no mixed-language output. The choice is remembered.

**A day in the life**: bright in the morning, drowsy after lunch, asleep at deep night; hungry faster by day, slower at night; says good morning and wakes up between 6–9, says good night at 23. Five stats only: Food / Energy / Mood / Clean / Health.

**Offline memory**: come back the next day and the time you were away is settled for real — it got hungry, mood dropped, a few messes piled up, and a "while you were away for N hours" summary pops up. Capped at 12 hours, so it never "starves".

**Time awareness & care**: on startup and at every hour it tells you the date, weekday and hour with a fitting remark (Monday morning, Friday night, lunchtime, late-night nudge…), and remembers **which day since you installed it** (day 7/30/100/365 get anniversary lines). The shell samples the foreground app every 3 seconds (negligible cost); past 45 minutes it suggests a break, water, or looking away — **at most once every 25 minutes**, so it never nags.

### Tray: still visible with the pet put away

A small round badge lives in the notification area — the relay between "the pet" and "your tasks":

| Badge | Meaning |
|---|---|
| Grey ring + dot | Idle |
| Blue ring + number | N tasks running |
| Amber ring + number | Tasks **waiting on you** |
| Red ring + number | Tasks failed |

- **Hover**: `PetHub · 2 running`, second line shows the current task and progress
- **Left-click**: expand / collapse the task panel (same as "Hide ▸" in the HUD)
- **Right-click**: task list (click any row and the pet reads it out), `Import app…` (Doubao and friends), `Connect an AI app…` (copy prompt / send test), show/hide pet, switch layer, quit
- **Pops a notification when something starts waiting on you**, so you don't have to stare at the screen (at most once every 20 s)

> **Windows 11 tucks it under `^` the first time.** Click the `^` on the taskbar and drag the PetHub icon onto the taskbar — you only do this once. On first run the program also shows a hint and the pet mentions it.

The data is **live**: the page pushes task changes to the shell the moment they arrive over the bridge SSE, and the badge updates instantly. A 3-second fallback poll takes over if the page is stuck or still loading, so the tray never lies about a task that's no longer running.

---

## 5. Multiple pets: lifecycle & social life

**One more pet**: just click the desktop shortcut again. Single-instance protection still applies — the second launch doesn't create a second fullscreen window, it pokes the running one so the page spawns another pet. Cap of 6, one taskbar icon forever.

**They grow up and grow old**: egg → baby → young → adult → elder, with the body scaling per stage; adults with a good relationship lay eggs; when their lifespan ends they pass away (with a farewell line). Everything is saved, so a reboot continues where you left off.

**What happens when two of them get close** (only when near each other, one scene at a time):

| Interaction | What you see |
|---|---|
| Sniff 👃 | A first greeting; relationship +3 |
| Play 🎵 | Chase each other around; mood up, energy down |
| Nuzzle ❤️ | Affection reserved for good friends; may lead to an egg |
| Gossip 💬 | A back-and-forth conversation: complaining about you, bad jokes, counting desktop icons… |
| Quarrel 💢 | Snapping at each other; both lose mood, relationship drops |
| Fight 💥 | Only when resentment runs deep or tempers flare; **the oldest pet always wins**, the loser loses mood, health and cleanliness |

Relationships are tracked **per pair** and saved: friends stay friendly, rivals keep bickering.

**Full-screen roaming**: no longer nailed to the taskbar — the middle and upper half of the screen are fair game (flyers and hoppers go up more often). Height changes are smooth, never teleporting.

**Layers**: double-click the pet to switch.
- **Always-on-top** (default): visible above every window;
- **Desktop layer**: sits below normal windows but above the wallpaper — open another app and it's gone; back to the desktop and it's still there.
  To call it back: `Ctrl + Alt + B`, or click empty desktop then double-click it.

**Going out**: now and then a pet walks off the screen edge for a while (at least one always stays home). It comes back with news — and about **12%** of the time brings a new friend, or a small gift roughly a third of the time.

**Every species has its own mess**: cats and dogs get the classic three-tier cone, rabbits a little pile of dry pellets, chickens and ducks a white splash, snakes a thin strip, spiders a few dark dots. One click sweeps it up.

**Thrown at the ceiling (the Shimeji move)**: lift a pet to the top of the screen and let go — it grabs the edge, shuffles sideways a couple of times looking for a foothold, then **creeps down the invisible wall** and only lets go when it lands. Body squashes and stretches with each pull, flakes of paint drop from under its feet. Dragging used to cap at 280px so the ceiling was unreachable; now you can carry it all the way up.

**It steals the mouse pointer**: every so often, when you haven't touched the mouse for a while, it sneaks over to the pointer, bites it, carries it around at its mouth for a few seconds, then drops it.

The rules are deliberately strict so it never actually hijacks your mouse:
- only after **20 s** with no mouse input, at most once every 4–10 minutes;
- if the pointer is in the upper half of the screen (out of reach), it doesn't even try;
- **move the mouse or click once and it lets go immediately** (the shell tells the page the moment real input arrives);
- it only moves the pointer with `SetCursorPos` — it never clicks anything.

---

## 6. Transparency & clicking (FAQ)

- The pet is a **full-screen transparent overlay**, but it does **not block clicks**: desktop icons, files and other windows work normally.
- Only clicks that actually land on the pet / HUD / bubble are taken by the pet (forwarded via a global hook).
- A past bug blocked the whole screen. It had two layers:
  1. WebView2 creates a full-screen **child window** that silently ate clicks — forced to `WS_EX_TRANSPARENT` at startup.
  2. WebView2 also opens a full-screen host window (`Chrome_WidgetWin_1`) inside its **browser process**. It belongs to another process, so the parent's click-through flag does not apply. The shell now finds it via the process tree, applies the same flag, and re-checks every 1.5s so a rebuilt window is fixed automatically.
- **Escape hatch hotkeys** (work at any time, independent of the UI):
  - `Ctrl + Alt + Q` — quit the pet;
  - `Ctrl + Alt + H` — hide / show the pet (hiding also removes the mouse hook, leaving the desktop completely clean).
- If you see a thin white line across the screen, that was the old decorative ground line; it has been removed. Recompile the shell if it persists.

---

## 7. Performance & privacy

- **No internet**: everything is on the `127.0.0.1:8765` loopback. The pet contacts no external service.
- **No content collection**: it only receives platform name / task name / status / progress / a one-line note. It never reads your files or chat content.
- **Hook dump off by default**: `universal_hook.py` only writes raw events when you explicitly set `PETHUB_DUMP=1`, because raw payloads may contain private data.
- **Cost**: shell ~60–90MB resident; redraws only while animating; foreground sampling every 3s; hit-testing every 30ms and only while the mouse moves; no full-screen polling, no OCR.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| HUD says "demo mode" | Bridge isn't up: run `python bridge/server.py` |
| Pet frozen / blank | Check the WebView2 runtime; see `%TEMP%\pethub-light.log` |
| External reports don't show | Check the bridge: `python connectors/pethub.py status` |
| MCP tools missing | Restart the client so it reloads `mcp.json`; ensure Python is on PATH |
| Inspect what a platform actually sends | `set PETHUB_DUMP=1`, run one task, read `~/.pethub-hookdump.log` (**contains raw content — turn it off and delete afterwards**) |
| Tray shows 2-3 icons, then back to 1 | The old build created a fresh icon handle on every state change; Explorer treats that as a new icon and leaves ghosts for a moment. Icons are now cached per state (handles never change) and coalesced over 400 ms, so it no longer happens |
| An imported app never shows up | Make sure it's running and its entry is ticked; the watcher only knows process + CPU + window title, so a fullscreen-exclusive or silently idle app can look like it's doing nothing |
