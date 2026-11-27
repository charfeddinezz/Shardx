# Writing an engine

An engine is a small object with three methods. If it satisfies the contract,
the supervisor will watch it, the registry will order it, and the dashboard
will show it — no other file needs to change.

## The contract

```python
from shadow.engines.base import Engine

class TabsEngine(Engine):
    name = "tabs"                       # used by the registry and every event
    depends_on = ("browser",)           # other engine names; order is derived

    def __init__(self, ctx):
        super().__init__(ctx)
        self.opened = 0

    def start(self) -> None:
        super().start()                 # sets started_at / _started; keep it
        self.cache = {}

    def stop(self) -> None:
        self.cache = {}
        super().stop()

    def health(self) -> dict:
        info = super().health()         # started? calls? errors? uptime?
        info["opened"] = self.opened
        return info

    # your own methods
    def open(self, url: str) -> dict:
        self.note(url=url)              # counts the call and publishes an event
        cdp = self.ctx.ensure_cdp()     # never build your own connection
        cdp.new_page(url)               # target lifecycle is the browser's job
        self.opened += 1
        return {"url": url}
```

Guidelines that matter more than the three methods:

* **Use `self.ctx`, not globals.** Config, bus, journal, registry, browser.
* **Reach other engines through the registry.** `self.engine("mouse")` — never
  `from .input import MouseEngine`.
* **`health()` must be cheap and must not raise.** The supervisor calls it every
  interval; an exception there is reported as "down".
* **Raise `ShadowError` subclasses**, not bare strings. Recovery classifies by
  type and `code`, and the workflow wraps them with the step number.
* **Emit before you act and after you finish.** Events are the audit trail the
  journal and dashboard read.

## Registering it

One line in `shadow/runtime/orchestrator.py`, with its dependencies:

```python
("tabs", TabsEngine(self.ctx), ("browser",)),
```

That is the whole wiring. The order is computed from `depends_on`, a cycle is
a startup error, and `start_all()` reports per-engine status.

## What the supervisor will do with it

* call `health()` every `supervisor.interval_s`;
* on `healthy: false` with `state: ready`, restart it through the registry;
* count the restart inside `restart_window_s`, with exponential backoff;
* quarantine it after `max_restarts`, and journal `supervisor.quarantine`;
* never touch it again until `supervisor.release("tabs")`.

So an engine that can be restarted should make `start()` idempotent.

## Adding a step action

A new action is three edits in `engines/workflow.py`:

1. add the name to `PlanBuilder` (when the planner should produce it);
2. add a branch in `WorkflowEngine._do` that returns a small dict;
3. if it touches the page, pass it through `SafetyEngine.check_action` first.

`Step.label()` and the journal entry are generated, so nothing else changes.

## Testing it

```python
from tests.helpers import MockCase

class TabsTest(MockCase):
    def test_open_creates_a_target(self):
        ctx = self.context()                       # mock launcher, temp state dir
        self.attach(ctx, "http://shop.local/")
        engine = TabsEngine(ctx)
        engine.start()
        self.assertEqual(engine.open("http://shop.local/about")["url"], "http://shop.local/about")
        self.assertTrue(engine.health()["healthy"])
```

`MockCase` gives you a launcher on an ephemeral port, a fresh state directory,
and `self.attach(ctx, url)` for a connected CDP client. No browser, no network,
no token.

## Style that the rest of the code follows

* Comments explain **why**, once, where the reason is not obvious from the code.
* No emoji in code or logs. A log line is read at 3am on a terminal.
* Prefer a plain function and a dataclass over a class hierarchy.
* Any behaviour a person would notice gets a test that would fail if it broke.
