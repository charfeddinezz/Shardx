# Architecture

Where every piece lives, why the boundaries are where they are, and what a
change is allowed to touch.

## The shape

```
        ┌─────────────── ShadowContext ───────────────┐
        │ config · bus · journal · registry · browser │
        └──────────────────┬──────────────────────────┘
                           │ (passed to every engine)
 ┌─────────────────────────┴─────────────────────────────┐
 │                      Engines                          │
 │  intel · discovery · page · browser · keyboard · mouse│
 │  vision · data · computer · recovery · safety · ui    │
 │  pages · actions · react · decision · workflow        │
 │  fleet · scheduler · supervisor                       │
 └───────────────────────────────────────────────────────┘
                           │
                  transport layer
        http (launcher API) · ws (RFC 6455) · cdp · domprobe
                           │
                 ShardX launcher  →  ShardBrowser engine
```

Three rules hold the system together:

1. **An engine never imports another engine.** It asks the registry:
   `self.ctx.registry.get("mouse")`. That is what lets the supervisor restart
   one engine without a cascade, and lets a test build a small world with only
   the engines a behaviour needs.
2. **The context is the only way out.** Config, the event bus, the journal and
   the browser come from `ShadowContext`. Nothing reads the environment or
   opens a socket on its own.
3. **`runtime/orchestrator.py` is the only file that knows the whole cast.**
   Adding an engine is one tuple there and nothing else.

## Layers

### `core/`

| File | Responsibility |
|---|---|
| `config.py` | Defaults → `shadow.json` → environment → explicit overrides. Unknown keys are an error, not a silent typo. |
| `events.py` | One bus. Subscribers cannot break publishers; the ring buffer is bounded; payloads are redacted before the dashboard sees them. |
| `registry.py` | Engine records, dependency order (with cycle detection), start/stop, health, restart counters. |
| `journal.py` | Append-only JSONL, fsynced per line. Torn last lines are expected after a crash and skipped. |
| `context.py` | Owns the single CDP attachment. `ensure_cdp()` is the only place a profile is started. |
| `token.py` | Mints the launcher's HS256 bearer token from its `settings.json`, handling the UTF‑16 and BOM files Windows writes. |

### `transport/`

`ws.py` is a small RFC 6455 client (stdlib only) used from both sides: the
product connects with it, and the mock engine accepts with it, so the framing
code is exercised by every test.

`cdp.py` keeps two channels on purpose:

* the **browser** channel — `Target.*`, and the patched `Motion` domain;
* a **page** session (flat attach) — the DOM, screenshots, navigation.

`call()` routes by domain (`BROWSER_ONLY`), and `session_id=""` forces the root
channel. That is the single place that decision is made.

`domprobe.py` defines one read-only protocol (`window.__shadowProbe`) so no
engine ever hand-writes a `Runtime.evaluate` string. The mock implements the
same protocol against its parsed document, which is why Discovery and Page
Understanding are testable without a browser and unchanged against a real one.

### `engines/`

Each engine is ~100–450 lines and answers one question:

| Engine | Question |
|---|---|
| intel | What did the person ask for? |
| discovery | Which element do they mean? |
| page | What kind of page is this, and what is on it? |
| workflow | What are the steps, and how did each one go? |
| decision | What is the next action? |
| safety | Is this action allowed, and does a person need to say yes? |
| recovery | It failed — what is the smallest thing that fixes it? |
| memory | What do we already know? |
| data | Extract, store, generate. |
| vision | What do the pixels say when the DOM does not? |
| supervisor | Is every engine alive, and did any task stop unfinished? |

### `mock/`

A launcher and an engine, in-process: an HTTP API with the launcher's auth and
endpoints, and a CDP WebSocket server that implements `Target`, `Runtime`,
`Page`, `Input`, `Motion`, and the probe protocol over a small HTML engine with
deterministic layout. `motion=False` builds one without the patched domain, so
the degraded path is tested rather than hoped about.

## Lifecycle of one goal

1. `ShadowAI.run(goal)` journals `goal` and hands it to the workflow engine.
2. `WorkflowEngine.plan()` asks `intel` for intents and `PlanBuilder` turns them
   into steps. Ordering follows the sentence, not a fixed table.
3. For each step: `SafetyEngine` checks a URL or an action, `WorkflowEngine._do`
   resolves the target against the live page through `discovery`, and
   `keyboard`/`mouse` act through `Motion`.
4. On success: `journal.append("step.done")` and a memory checkpoint. On failure:
   `RecoveryEngine.recover()` classifies and acts, then the step is retried once.
5. A step that needs a person raises `ApprovalRequired`; the task is marked
   `needs_approval` and the journal records `task.paused`.
6. The supervisor's next pass sees an interrupted task (no `task.ended`, quiet
   for longer than `heartbeat_timeout_s`) and resumes it from the next step.

## Configuration and secrets

Config order: dataclass defaults → `shadow.json` (`$SHADOW_CONFIG`) → `SHADOW_*`
environment → explicit overrides. Secrets never live in `shadow.json`; use
`SHADOW_API_TOKEN` or `SHADOW_LAUNCHER_SETTINGS`. `describe()` masks both, and
the journal and dashboard redact credential-shaped keys on the way out.

## Testing strategy

* **Unit**: parser, scoring, config, journal, redaction — no I/O.
* **Protocol**: the WebSocket and CDP clients against the mock engine,
  including a build without `Motion`.
* **Behaviour**: each engine's promise (a refusal, an ambiguity, a degraded
  flag, a persisted checkpoint) with the engines it needs and no more.
* **System**: goals end to end, supervisor restart/quarantine/resume, the
  dashboard over HTTP, every CLI verb.

Nothing in the suite needs a browser, a network, or a token.

## Deliberate limits

* Input goes through `Motion` or it is marked degraded. There is no third path
  that pretends.
* Ambiguity is refused, not guessed. `AmbiguousTarget` carries the options so a
  caller can narrow the label.
* OS input and shell commands are off by default and allowlisted when on.
* The model bridge is optional, local-only, and its output is validated.


## The reasoning loop (`engines/react.py`)

`WorkflowEngine.run_goal` decides *how* a goal will be carried out before any
action happens:

```
plan ──► should_reason()? ──no──► run the plan once (cheap, read-only goals)
                    │
                   yes
                    ▼
        ReasoningEngine.run(goal, seed_plan)
          look ─► decide ─► act ─► look …
```

`decide` has a fixed precedence and it is load-bearing:

1. an unused step of the reviewed plan;
2. the local model — only when the plan is spent or a step failed;
3. a heuristic from the page — which may not repeat an `(action, target)` it
   already performed on this page.

The `done_here` set and the page signature are what keep the loop from
re-clicking the same button forever; a page that does not change for
`stale_after` turns ends the run as `stuck`, and three failures in a row end
it as `failed` with the last real error. Every turn is journaled, and the
whole trail comes back in `TaskResult.results["thoughts"]`.

`ActionsEngine` is the seam: `WorkflowEngine._do` and the loop both call
`actions.perform(step)`, so a plan step and a model-proposed step behave
identically, and the safety engine sees both.

## Page events (`engines/pages.py`)

Two conditions on a real page will not answer a `Runtime.evaluate`:

* a JavaScript dialog is open — the protocol waits until it is answered;
* the result is in a tab that this click just opened.

`PagesEngine` subscribes to `Target.*` and `Page.javascriptDialog*` on the
browser channel. `before_action()` snapshots the tab list, `after_action()`
answers a pending `confirm`, finds the new tab and applies `tabs_policy`.
The snapshot is taken *before* the click on purpose: a `targetCreated` event
often arrives before the next look, so comparing afterwards would find
nothing new.

## The fleet (`engines/fleet.py`)

A fleet worker is a whole `ShadowAI`, not a shared context with a different
profile id: own registry, own journal, own state directory, own CDP session.
That is what makes parallelism safe — there is nothing to step on. The fleet
engine only owns the pool, the job list and the two rules it inherits:
protected hosts are checked before any worker starts, and one worker's
exception is one failed job, never a failed fleet.

## Where secrets stop

Three independent gates, because one is not enough:

1. `util.redact` — key names and `pass: …` phrases, for anything logged;
2. `SafetyEngine.note_typed` — the keyboard engine registers every value it
   typed into a labelled secret field;
3. `Journal.scrub` — wired at boot to `SafetyEngine.scrub_output`, so those
   values are removed *by value* from every journal line before it is written.

Tests assert the strongest form: after a login, the password does not appear
anywhere in the state directory.
