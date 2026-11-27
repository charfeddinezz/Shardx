# SHADOW AI — SHARDX AUTOPILOT ULTIMATE

An operator for ShardX (ShardBrowser) that runs **entirely on your computer**.
Give it a sentence — in Arabic, English or French — and it drives a live
ShardX profile: it reads the page, decides, types and clicks through the
engine's own human-input path, remembers what worked, recovers when a page
moves under it, and keeps watching its own engines so an unattended run does
not need you sitting there.

No cloud model, no account, no API key. The only network calls it can make are
the ones you configure (an optional model server on `127.0.0.1`).

```bash
shadow run "Go to shop.local, fill the search field with lamp, press Search"
shadow run "افتح http://login.local/ user: admin pass: hunter2 ثم اقرأ العنوان"
shadow ask "where is the oak desk?" --url http://shop.local/
shadow plan "Open http://shop.local/product/lamp and read the price"   # print steps, run nothing
```

---

## 1. Start here

### Windows

1. Unzip the folder.
2. Open it.
3. Run **`install.bat`** — it finds Python 3.8+, makes a private `.venv`,
   installs the package, runs the self test and prints a `doctor` report.
4. Run **`run.bat`** — opens the local dashboard on `http://127.0.0.1:40330`.

`run.bat` also takes a verb: `run.bat run "Open shop.local and read the title"`.

### Linux / macOS

```bash
./install.sh
./run.sh                                   # dashboard
./run.sh run "Open shop.local and read the title"
```

### Try it without ShardX

Everything below works with no browser installed, against a built-in mock
engine that speaks the real CDP protocol over a real WebSocket:

```bash
shadow --mock demo
```

That runs four goals end to end — a search, a login, a slow page, and a
purchase that stops for approval — then prints the supervisor's report.

### Point it at your ShardX

In ShardX: **Settings → Automation API**. Either copy the token, or let SHADOW
read it from the launcher's own `settings.json`:

```bash
export SHADOW_API_TOKEN="<token from Settings>"
# or, on the same computer as ShardX:
export SHADOW_LAUNCHER_SETTINGS="$HOME/.config/shardx-launcher/settings.json"
#    Windows: %APPDATA%\shardx-launcher\settings.json
#    macOS:   ~/Library/Application Support/shardx-launcher/settings.json

shadow doctor          # token, launcher, Motion domain, OCR, local model
shadow run "..." --profile win-rtx4060
```

`doctor` tells you the truth before you trust it with a goal: whether the
launcher answers, whether the engine exposes the patched `Motion` domain, and
whether the input path will therefore be human or visible.

---

## 2. The engines

Every engine is an independent object with `start()`, `stop()` and `health()`.
They talk only through the event bus and the registry, which is what lets the
supervisor restart one without touching the others.

| Engine | Module | What it actually does |
|---|---|---|
| 🧠 **Intelligence** | `engines/intelligence.py` | Reads a goal in AR/EN/FR into intents; optional local-model bridge |
| ⌨️ **Smart Keyboard** | `engines/input.py` | Focus, clear, type, press — through `Motion` |
| 🖱️ **Smart Mouse** | `engines/input.py` | Glide, tap, drag, scroll, hover — through `Motion` |
| 👁️ **Vision** | `engines/vision.py` | Screenshots, frame diffing, OCR when Tesseract is installed |
| 🔍 **Discovery** | `engines/discovery.py` | Scores every element against the goal; refuses ties |
| 🌐 **Page Understanding** | `engines/page_understanding.py` | Page kind, headings, forms, tables, alerts; answers questions from text |
| 🗄️ **Memory** | `engines/memory.py` | Facts, checkpoints, lessons — small JSON you can read |
| 📊 **Data** | `engines/data.py` | Tables, lists, prices; JSONL/CSV storage; coherent generated people |
| 💻 **Computer Command** | `engines/computer.py` | Allowlisted subprocesses, `shell=False`, off by default |
| 🎛️ **Smart UI** | `engines/ui.py` | Terminal ticker, notifications, state document for the dashboard |
| 🔄 **Recovery** | `engines/recovery.py` | Classifies a failure, then settles / re-waits / reattaches / reloads |
| ⚙️ **Workflow** | `engines/workflow.py` | Goal → steps → actions, journaled per step |
| ⏱️ **Scheduler** | `engines/scheduler.py` | `every()`/`after()` jobs, saved goals in `jobs.json`, hourly journal compaction |
| 🛡️ **Safety** | `engines/safety.py` | URL and action policy, approvals, budgets — the last gate |
| 🧭 **Decision** | `engines/decision.py` | Chooses the next action from plan, model, or page; validates model output |
| 🧩 **Browser** | `engines/browser.py` | Profile lifecycle and the CDP attachment the rest share |
| 🪟 **Page Events** | `engines/pages.py` | New tabs, popups and JavaScript dialogs — the three things that stop a run dead |
| 🎬 **Actions** | `engines/actions.py` | Every verb in one place, so a plan, a model and the CLI behave identically |
| 🔁 **Reasoning** | `engines/react.py` | The loop: look, decide, act, look again — plan first, model when the plan is spent |
| 🚀 **Fleet** | `engines/fleet.py` | Several profiles at once, one worker each, bounded parallelism |
| **Autonomous Supervisor** | `engines/supervisor.py` | Heartbeat, restart, quarantine, resume — the next development, built |

---

## 3. How one goal flows

```text
goal (AR / EN / FR)
        │
        ▼
 Intelligence ── understand ──► intents ──► PlanBuilder ──► steps
        │                                                   │
        │                        ┌──────────────────────────┘
        ▼                        ▼
   Decision ◄── page ───── Workflow ── per step ──► Safety ──► pass / refuse
        │                        │                             │
        ▼                        ▼                             ▼
  (optional) local model    Keyboard / Mouse  ───────►  Motion domain  ──► ShardX
        │                        │                             │
        └──── validated ─────────┤                             ▼
                                 │                        page changes
                                 ▼                             │
                        Recovery (retry, reattach) ◄────────────┘
                                 │
                                 ▼
                        Memory checkpoint ──► Journal (fsync per step)
                                 │
                                 ▼
                        Supervisor (watch, restart, resume)
```

A step is resolved **against the page as it is**, not against a selector
guessed before the page was seen. `click the Sign in button` finds the button
when the step runs; if the page changed, Recovery settles and retries, and if
the label is genuinely absent the run fails with a reason instead of clicking
something nearby.

---

### The reasoning loop, and when it runs

A goal whose plan contains a change to the page — open, find, click, fill,
login, submit, buy, scroll, select, press — goes through the loop, not through
the plan once and done. Read-only goals stay plan-driven and cheap.

Every turn: look at the page, pick one action, carry it out, look again. The
decision order is fixed and deliberate:

1. **The reviewed plan**, if it still has an unused step. `shadow plan` is a
   contract, and the loop does not quietly ignore it.
2. **The local model**, only when the plan is spent or a step just failed.
3. **The page itself**, as a last resort — and it may not repeat an action
   already performed against the same page.

The result carries the whole reasoning trail:

```bash
shadow --json run "Open http://shop.local/ and click Contact" | jq '.results.thoughts'
```

Three things stop a loop rather than letting it spin: `stale_after` turns
without the page changing, three failed actions in a row, and `max_turns`.
The status says which: `stuck` (the page never moved) or `failed` with the
last real error.

### Page events: popups and dialogs

Two page behaviours break naive automation, so they are handled in the page
session rather than left to luck:

* **A new tab or window** (OAuth, a receipt, a payment page). A click is
  bracketed: snapshot the tab list, click, then attribute. `tabs_policy`
  decides what happens — `switch` (default) follows the popup and keeps
  working there, `ignore` leaves the session where it was, `close` shuts the
  popup. `switch_tab` returns to `main` afterwards.
* **A JavaScript dialog.** While one is open, every `Runtime.evaluate` blocks
  forever. Dialogs are subscribed to, plain `alert`s are dismissed, and a
  `confirm`/`prompt` is recorded and either answered by the run or left for a
  person. The mock reproduces the block exactly, which is how the fix is
  tested rather than hoped for.

```bash
shadow run "Open http://dialog.local/ and click the Rename account button"
shadow run "Open http://popup.local/ and click Continue with Example"
```

---

## 4. The supervisor

Four jobs, one thread, every 5 seconds by default:

1. **Heartbeat.** Health is a probe, not a flag: the browser engine reads a URL
   over CDP, so a process that died is reported dead.
2. **Detect.** An unhealthy engine is marked down. A quarantined one is left
   alone.
3. **Restart with a cap.** Restarts back off (`0.25s · 2ⁿ`, capped) and are
   counted inside a rolling window. A component that dies `max_restarts` times
   in `restart_window_s` is **quarantined** — restart loops hide the fault.
   `supervisor.release("name")` puts it back under watch once a human has
   fixed it.
4. **Resume.** On startup it reads the journal for tasks that started and
   never ended, skips the ones a person paused for approval, and re-runs each
   from the step after its last journaled one — once.

```python
brain = ShadowAI(cfg)
brain.boot()
print(brain.engine("supervisor").report())
```

---

## 5. Command line

| Command | What it does |
|---|---|
| `shadow run "<goal>"` | Carry a goal out end to end |
| `shadow plan "<goal>"` | Print the steps; touch no browser |
| `shadow ask "<question>" [--url U]` | Answer from the current page (or `U`) |
| `shadow status` | Profile, engines, supervisor, recent tasks |
| `shadow engines` | Health table, one line per engine |
| `shadow fleet "<goal>" [--profiles a,b]` | The same goal on several profiles, in parallel |
| `shadow doctor` | Python, token, launcher, Motion, OCR, local model |
| `shadow doctor --live` | The above **and** a real profile, page and goal, end to end |
| `shadow demo` | The scripted mock run |
| `shadow memory` | What SHADOW remembers |
| `shadow approve <task>` | Continue a task that stopped for approval |
| `shadow schedule "<goal>" --every 30` | A goal on a clock, remembered across restarts |
| `shadow schedule --list` / `--pause` / `--resume` / `--cancel <id>` | Manage saved jobs |
| `shadow serve [--port 40330]` | Local dashboard |

Global flags: `--mock`, `--json`, `--quiet`, `--profile`, `--state`,
`--token`, `--launcher-settings`, `--api-base`, `--config`. They work before
or after the verb.

### The fleet

One goal, several browsers, one worker each — each with its own engines,
journal, state directory and CDP session, so nothing is shared but the pool
that starts them:

```bash
shadow fleet "Open http://shop.local/ and read the title" --profiles a,b,c
shadow fleet --json "Open shop.local and read the title" --all-profiles
```

`fleet.max_parallel` (default 3) bounds the pool. Each worker seeds its own
motion, so two identical goals do not produce two identical pointers. A
worker that throws fails alone; the rest finish. A protected host is refused
*before* any worker starts. The dashboard has the same thing at `/api/fleet`
and a **Fan out** box.

### Proving it on the machine it will run on

`doctor` alone proves the token parses and `/health` answers. That is not the
same as "SHADOW can drive this box", so:

```bash
shadow doctor --live --live-url https://example.com/
```

It lists your real profiles, starts one through `POST /profiles/{id}/start`,
attaches to the returned CDP endpoint, loads a page and runs one goal. Every
line after `Motion domain` is something that actually happened. Exit code 1
means it did not.

Exit codes: `0` done, `1` failed, `3` waiting for approval.

---

## 6. Safety defaults

These are not suggestions; they are the values a fresh install starts with.

| Setting | Default | Meaning |
|---|---|---|
| `allow_os_input` | **false** | Real OS-level mouse and keyboard stay off |
| `allow_system_commands` | **false** | No shell at all until you say so |
| `allowed_commands` | `echo date hostname` | The only programs allowed when enabled |
| `blocked_hosts` | localhost, `127.0.0.1`, `::1`, `169.254.169.254`, … | Never touched, whatever the goal says |
| `redact_sensitive_inputs` | **true** | Passwords and card numbers never reach the journal or memory |
| `redact_values` | **true** | Values *typed into* secret fields are scrubbed by value from every journal line, memory file and dashboard payload |
| `protected_hosts` | bank, paypal, stripe, wise, revolut, gov, tax, cashplus, westernunion … | A matching host stops the run as `needs_approval` **before a browser is even started** |
| `max_task_seconds` / `max_steps_per_task` | 900 / 500 | A run that never ends is stopped |
| Payment / destructive clicks | ask a person | `needs_approval`, then `shadow approve` |

Purposes that cannot be done quietly are simply not done quietly: a purchase
pauses the run and prints the task id. `--yes` approves up front when you mean
it.

```bash
shadow run --yes "Open http://shop.local/product/lamp and buy the Linen Lamp"
```

---

## 7. A local model, if you want one

The heuristic planner is complete for searching, filling forms, logging in,
reading and navigating. A model helps with ambiguous pages. It must run on
your machine:

```bash
ollama pull llama3.1
export SHADOW_PROVIDER=ollama
export SHADOW_MODEL=llama3.1
shadow doctor | grep "local model"
```

Any OpenAI-compatible server works too (`provider: openai_compatible`,
`base_url: http://127.0.0.1:8080`).

A model's proposal is **validated before it runs**: the action must be in
`intelligence.allow_actions`, and a `click` target must exist on the page the
model was shown. A rejected proposal is an event, not a crash, and the planner
carries on without it. The Safety Engine sees every action either way.

---

## 8. Library use

```python
from shadow import ShadowAI, ShadowConfig

cfg = ShadowConfig.load(overrides={"default_profile": "win-rtx4060"})
with ShadowAI(cfg) as brain:
    result = brain.run("Open http://login.local/ user: admin pass: hunter2 then read the title")
    print(result.status, result.results.get("answer"))

    # Answer without running a goal
    print(brain.ask("what is the price of the oak desk?", url="http://shop.local/"))

    # Drive pieces directly
    brain.engine("keyboard").type("hello")
    brain.engine("mouse").click_label(brain.ctx.probe, "Search button")
    brain.engine("data").person("win-rtx4060", card=True).to_dict()
```

---

## 9. Tests

```bash
python run_tests.py -v      # 210 tests, no browser, no network
```

The suite runs the whole system against the mock engine: the WebSocket framing,
CDP routing (page session vs. the browser channel `Motion` lives on), the DOM
reader, the parser in three languages, discovery scoring and its refusals,
human input landing in fields, memory persistence, journal durability with a
torn last line, safety refusals, recovery, the supervisor restarting and
quarantining engines, resuming an interrupted task, scheduling, the dashboard
over HTTP, and every CLI verb.

---

## 10. What this is not (yet)

Said plainly, because the difference matters:

* **It has not been run against every ShardBrowser build.** It is written
  against the documented local API and the `Motion` domain in this repository's
  README, and it detects their absence rather than assuming them.
* **The mock is not a browser.** It parses a small HTML subset and implements
  the CDP commands SHADOW uses. Tests prove the wiring, not that a particular
  site works.
* **OCR needs Tesseract** installed by your OS package manager. Without it,
  `vision.ocr()` raises a clear `VisionUnavailable` and the DOM reader is used.
* **It does not control the OS mouse or keyboard.** That is off by default
  (`allow_os_input`); everything goes through the browser's own input engine.
* **The local model is optional and off by default.** With no model, the
  planner is deterministic and narrower — very good at forms, search, reading
  and navigation, not at improvising on arbitrary layouts.
* **The live path is tested against a launcher-shaped fake, not Chrome.** The
  fake mirrors this repository's `src-tauri/src/api.rs` and `openapi.yaml`
  route for route, and behind it is a real CDP server — but the last step
  (a real ShardBrowser engine on a real desktop) still has to be `shadow
  doctor --live` on your machine.
* **No CAPTCHA and no 2FA path.** A login that asks for a one-time code stops
  and says so; it does not try to look human at a challenge.
* **Lessons are learned, not curated.** A selector that worked on a site is
  reused for that action the next time and dropped when it goes stale, but
  there is no hand-written selector pack per site — a heavily customised page
  may still need one editing session.
* **`shadow init` does not exist.** Configuration is `shadow.json` (or
  `shadow.json.example`, which lists every key) plus `--token`.
* **Only saved goals persist, not closures.** A job added with `goal=`
  (the CLI, the dashboard, `every_goal`) is written to `jobs.json` and comes
  back on the next boot; a job added with a Python closure cannot be, and says
  so rather than pretending. The journal is compacted hourly and keeps its tail.

---

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401/403` from the launcher | Token stale. Copy a fresh one, or set `SHADOW_LAUNCHER_SETTINGS` |
| `no ShardX settings.json found` | ShardX never ran on this computer — open it once, or use a token |
| `no profile chosen` | `--profile <id>`, or `default_profile` in `shadow.json` |
| `Motion domain missing` in `doctor` | This engine build has no `Motion`; input falls back to `Input.*` and the run is marked degraded |
| `element did not keep the value` | The field clears itself on blur; the keyboard engine retries once, then fails honestly |
| A run stops at `needs_approval` | That is the safety engine. `shadow approve <task-id>` |
| Job never fires | `scheduler.every()` takes seconds, minimum 0.05; a paused job has `next_at = 0` and stays paused across restarts |

---

## 12. Layout

```text
shadow-ai/
├── install.bat  run.bat        Windows install and start
├── install.sh   run.sh         Linux / macOS install and start
├── shadow.json.example         every setting, with its default
├── run_tests.py                the suite
├── shadow/
│   ├── cli.py                  shadow run / plan / ask / doctor / serve …
│   ├── core/                   events · registry · config · journal · context · token
│   ├── transport/              http · websocket (RFC 6455, stdlib) · cdp · domprobe · shardx API
│   ├── engines/                one file per engine, plus supervisor
│   ├── runtime/orchestrator.py the only place that knows the whole cast
│   ├── mock/                   launcher · CDP engine · DOM · pages
│   └── dashboard/              one HTML page and a small JSON API
├── tests/                      210 tests (incl. fake launcher + fake model)
└── docs/                       architecture · writing an engine
```

MIT licensed, like the rest of this repository.
