"""Command line: `shadow <verb> ...`.

    shadow run "Open shop.local, search for lamp" --mock
    shadow plan "..."            print the steps, run nothing
    shadow ask "what is the price?" --url http://shop.local/product/lamp
    shadow status                engines, supervisor, journal
    shadow doctor                is the launcher reachable? is Motion there?
    shadow demo                  a scripted end-to-end run on the mock
    shadow serve                 a local dashboard on 127.0.0.1
    shadow approve t-1234        release a task waiting on a human
    shadow memory                what SHADOW remembers
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from typing import Any, Dict, List, Optional

from .core.config import ShadowConfig
from .runtime.orchestrator import ShadowAI
from .util import jdump, truncate


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shadow", description="SHADOW AI — SHARDX AUTOPILOT ULTIMATE")
    p.add_argument("--config", default="", help="path to shadow.json")
    p.add_argument("--mock", action="store_true", help="run against the built-in mock launcher")
    p.add_argument("--profile", default="", help="ShardX profile id")
    p.add_argument("--state", default="", help="state directory (journal, memory, data)")
    p.add_argument("--token", default="", help="bearer token from Settings > Automation API")
    p.add_argument("--launcher-settings", default="", help="path to the launcher's settings.json")
    p.add_argument("--api-base", default="", help="the launcher's automation API (default 127.0.0.1:40325)")
    p.add_argument("--quiet", action="store_true", help="print only results")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb")

    run = sub.add_parser("run", help="carry out a goal")
    run.add_argument("goal", nargs="+")
    run.add_argument("--yes", action="store_true", help="approve irreversible actions up front")
    run.add_argument("--no-resume", action="store_true", help="start from step 1")

    plan = sub.add_parser("plan", help="print the steps for a goal")
    plan.add_argument("goal", nargs="+")

    ask = sub.add_parser("ask", help="answer a question from a page")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--url", default="")

    sub.add_parser("status", help="engines, supervisor and journal")
    doc = sub.add_parser("doctor", help="check the environment")
    doc.add_argument("--live-url", default="http://example.com/",
                     help="the page the live check loads (use a site you expect to reach)")
    doc.add_argument("--live", action="store_true",
                     help="also prove it: profiles list, a browser starts, a page loads, a goal runs")
    sub.add_parser("engines", help="engine health table")
    sub.add_parser("memory", help="what SHADOW remembers")

    demo = sub.add_parser("demo", help="scripted end-to-end run on the mock")
    demo.add_argument("--goal", default="")

    serve = sub.add_parser("serve", help="local dashboard")
    serve.add_argument("--host", default="127.0.0.1",
                       help="0.0.0.0 reaches a container or a phone on the LAN; then --token is required")
    serve.add_argument("--token", dest="dash_token", default="",
                       help="required when --host is not loopback; sent as X-Shadow-Token")
    serve.add_argument("--port", type=int, default=40330)
    serve.add_argument("--open", action="store_true")

    fleet = sub.add_parser("fleet", help="one goal, several profiles, at the same time")
    fleet.add_argument("goal", nargs="+")
    fleet.add_argument("--profiles", default="", help="comma-separated profile ids (default: up to fleet.max_parallel)")
    fleet.add_argument("--all-profiles", action="store_true", help="use every profile the launcher knows")
    fleet.add_argument("--yes", action="store_true", help="approve irreversible actions up front")
    fleet.add_argument("--background", action="store_true", help="start and return immediately")

    sched = sub.add_parser("schedule", help="a goal on a clock, remembered across restarts")
    sched.add_argument("goal", nargs="*")
    sched.add_argument("--every", type=float, default=0.0, help="minutes between runs")
    sched.add_argument("--name", default="", help="a name for the job")
    sched.add_argument("--list", action="store_true", help="show saved jobs and exit")
    sched.add_argument("--cancel", default="", help="cancel a job by id")
    sched.add_argument("--pause", default="", help="pause a job by id")
    sched.add_argument("--resume", default="", help="resume a job by id")

    approve = sub.add_parser("approve", help="release a task that is waiting for a human")
    approve.add_argument("task_id")

    return p


GLOBAL_FLAGS = {"--mock", "--json", "--quiet", "--profile", "--state", "--token",
                "--launcher-settings", "--config", "--api-base"}


def _split_globals(argv: List[str]) -> List[str]:
    """Let `shadow run --json "goal"` mean the same as `shadow --json run "goal"`."""
    head, tail = [], []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in GLOBAL_FLAGS:
            head.append(token)
            if token != "--mock" and token != "--json" and token != "--quiet" and i + 1 < len(argv):
                head.append(argv[i + 1])
                i += 1
        else:
            tail.append(token)
        i += 1
    return head + tail


def main(argv: Optional[List[str]] = None) -> int:
    argv = _split_globals(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    overrides: Dict[str, Any] = {}
    if args.profile:
        overrides["default_profile"] = args.profile
    if args.mock:
        overrides["mock"] = True
    if args.state:
        overrides["state_dir"] = args.state
    if args.token:
        overrides["api_token"] = args.token
    if args.launcher_settings:
        overrides["launcher_settings"] = args.launcher_settings
    if getattr(args, "api_base", ""):
        overrides["api_base"] = args.api_base

    if args.verb == "serve":
        return _serve(args, overrides)

    cfg = ShadowConfig.load(path=args.config or None, overrides=overrides)
    if args.verb == "plan":
        return _plan(cfg, " ".join(args.goal), args)
    if args.verb == "doctor":
        return _doctor(cfg, args)

    brain = ShadowAI(cfg, printer=(lambda line: None) if args.json else None)
    if args.quiet and not args.json:
        brain.ui.quiet = True
    try:
        if args.verb == "run":
            return _run(brain, " ".join(args.goal), args)
        if args.verb == "ask":
            brain.boot()
            answer = brain.ask(" ".join(args.question), url=args.url)
            return _print(answer if args.json else _answer_text(answer), args)
        if args.verb == "status":
            brain.boot()
            return _status(brain, args)
        if args.verb == "engines":
            brain.boot()
            return _engines(brain, args)
        if args.verb == "memory":
            brain.boot()
            store = brain.engine("memory").store
            return _print(store.data if args.json else _memory_text(store))
        if args.verb == "demo":
            return _demo(brain, args)
        if args.verb == "schedule":
            return _schedule(brain, args)
        if args.verb == "fleet":
            return _fleet(brain, " ".join(args.goal), args)
        if args.verb == "approve":
            brain.boot()
            result = brain.approve(args.task_id)
            return _print(result.to_dict() if args.json else _result_text(result), args)
        return _print("nothing to do. try: run, plan, ask, status, doctor, demo, serve", args)
    finally:
        brain.shutdown()


# --------------------------------------------------------------------------
# verbs
# --------------------------------------------------------------------------

def _run(brain: ShadowAI, goal: str, args) -> int:
    brain.boot()
    result = brain.run(goal, approved=getattr(args, "yes", False), resume=not getattr(args, "no_resume", False))
    if args.json:
        print(jdump(result.to_dict(), indent=2))
    else:
        print(_result_text(result))
    if result.status == "needs_approval":
        brain.ui.notify("warn", f"to continue: shadow approve {result.task_id}")
        return 3
    return 0 if result.ok else 1


def _schedule(brain: ShadowAI, args) -> int:
    brain.boot()
    scheduler = brain.engine("scheduler")
    for verb, past, action in (("cancel", "cancelled", scheduler.cancel),
                               ("pause", "paused", scheduler.pause),
                               ("resume", "resumed", scheduler.resume)):
        job_id = getattr(args, verb, "")
        if not job_id:
            continue
        ok = action(job_id)
        _print({"job": job_id, "ok": ok} if args.json else
               (f"{past} {job_id}" if ok else f"no job {job_id}"), args)
        return 0 if ok else 1
    goal = " ".join(args.goal)
    if not goal or args.list:
        jobs = [job.to_dict() for job in scheduler.jobs.values()]
        if args.json:
            print(jdump(jobs, indent=2))
        else:
            for job in jobs:
                print(f"  {job['id']:<20} {job['name'][:30]:<30} every {job['every_s']:>8.0f}s "
                      f"runs={job['runs']} paused={job['paused']}")
            if not jobs:
                print("  no jobs saved")
        return 0
    minutes = args.every or 60.0
    job = scheduler.every_goal(args.name or truncate(goal, 40), goal, minutes * 60.0)
    if args.json:
        print(jdump(job.to_dict(), indent=2))
    else:
        print(f"  scheduled {job.id} every {minutes:g} min: {goal}")
        print("  the dashboard keeps it; jobs saved here come back after a restart.")
    return 0


def _fleet(brain: ShadowAI, goal: str, args) -> int:
    brain.boot()
    names = [p.strip() for p in (args.profiles or "").split(",") if p.strip()]
    if args.all_profiles or not names:
        names = []          # let the runtime pick fleet.max_parallel profiles
    jobs = brain.fan_out(goal, names or None, approved=getattr(args, "yes", False),
                         background=getattr(args, "background", False))
    if args.json:
        print(jdump(jobs, indent=2))
    else:
        for job in jobs:
            print(f"  {job['profile']:<16} {job['status']:<12} "
                  f"{(job.get('answer') or job.get('error') or '')!s:.90}")
    codes = {"done": 0, "needs_approval": 3}
    if any(j["status"] == "needs_approval" for j in jobs):
        return 3
    return 0 if all(j["status"] in ("done", "running", "pending") for j in jobs) else 1


def _plan(cfg: ShadowConfig, goal: str, args) -> int:
    brain = ShadowAI(cfg, printer=lambda line: None)
    try:
        brain.boot(engine_names=["intel", "workflow", "memory", "safety", "ui"])
        task = brain.plan(goal)
        if args.json:
            print(jdump(task.to_dict(), indent=2))
        else:
            intel = brain.engine("intel").understand(goal)
            print(f"goal      {goal}")
            print(f"language  {intel.language}")
            print(f"url       {intel.url or '-'}")
            print("steps")
            for i, step in enumerate(task.steps, 1):
                print(f"  {i:2d}. {step.label()}")
        return 0
    finally:
        brain.shutdown()


def _live_checks(cfg: ShadowConfig, checks: List[Dict[str, Any]], args) -> None:
    """The checks that are only true if something actually happens.

    `doctor` without --live proves the token parses and the launcher answers
    /health. That is not the same as "SHADOW can drive this machine", so with
    --live it lists real profiles, starts one, loads a real page and runs one
    goal that needs no judgement.
    """
    from .engines.workflow import Step
    from .transport.shardx import ShardxApi

    if not cfg.mock and not any(c["check"] == "launcher" and c["ok"] for c in checks):
        # one clear line beats five symptoms: nothing can be proven about a
        # machine whose launcher is not answering
        checks.append({"check": "live run", "ok": False,
                       "detail": f"the launcher is not answering at {cfg.api_base} — "
                                 "start ShardX, then run this again"})
        return

    brain = ShadowAI(cfg, printer=lambda line: None)
    profiles: List[str] = []
    try:
        if cfg.mock:
            profiles = brain.profiles()
            checks.append({"check": "profiles", "ok": bool(profiles),
                           "detail": f"mock: {', '.join(profiles)}"})
        else:
            api = ShardxApi(cfg.api_base, token=cfg.resolve_token())
            raw = api.profiles()
            profiles = [p.get("id", "") for p in raw if p.get("id")]
            checks.append({"check": "profiles", "ok": bool(profiles),
                           "detail": f"{len(profiles)} profile(s): {', '.join(profiles[:5]) or 'none'}"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "profiles", "ok": False, "detail": str(exc)[:200]})

    target = (args.profile or "") or (profiles[0] if profiles else cfg.default_profile)
    try:
        brain.boot()
        cdp = brain.ctx.ensure_cdp(target)
        checks.append({"check": "attach", "ok": cdp is not None,
                       "detail": f"{brain.ctx.profile_id} · motion={cdp.motion_available()}"})
        loaded = brain.engine("actions").perform(
            Step("goto", target=args.live_url), index=-1)
        title = str((loaded or {}).get("title", ""))
        checks.append({"check": "page load", "ok": bool(title),
                       "detail": f"{args.live_url} → {title[:60]!r}"})
        result = brain.run(f"Open {args.live_url} and read the title")
        checks.append({"check": "goal", "ok": result.status == "done",
                       "detail": f"{result.status} · answer={result.results.get('answer', '-')!r}"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "live run", "ok": False,
                       "detail": f"{type(exc).__name__}: {exc}"[:200]})
    finally:
        brain.shutdown()


def _doctor(cfg: ShadowConfig, args) -> int:
    checks: List[Dict[str, Any]] = []
    checks.append({"check": "python", "ok": sys.version_info >= (3, 8),
                   "detail": sys.version.split()[0]})
    try:
        token = "" if cfg.mock else cfg.resolve_token()
        checks.append({"check": "token", "ok": bool(token) or cfg.mock,
                       "detail": "minted from settings.json" if cfg.launcher_settings else
                                 ("supplied" if cfg.api_token else ("mock" if cfg.mock else "missing"))})
    except Exception as exc:  # noqa: BLE001
        token = ""
        checks.append({"check": "token", "ok": False, "detail": str(exc)[:160]})

    if cfg.mock:
        checks.append({"check": "launcher", "ok": True, "detail": "mock (no launcher needed)"})
    else:
        from .transport.shardx import ShardxApi
        api = ShardxApi(cfg.api_base, token=token)
        try:
            health = api.health()
            checks.append({"check": "launcher", "ok": True,
                           "detail": f"{cfg.api_base} · {health.get('name', 'launcher')} "
                                     f"{health.get('version', '')}".strip()})
            if not cfg.default_profile:
                # nothing was chosen: use the first real profile rather than
                # failing the browser check for the lack of a name
                listed = api.profiles()
                if listed and listed[0].get("id"):
                    cfg.default_profile = listed[0]["id"]
        except Exception as exc:  # noqa: BLE001
            checks.append({"check": "launcher", "ok": False, "detail": f"{cfg.api_base}: {exc}"})

    try:
        import shutil
        checks.append({"check": "tesseract", "ok": shutil.which("tesseract") is not None,
                       "detail": shutil.which("tesseract") or "not installed (DOM reading still works)"})
    except Exception:
        pass

    brain = ShadowAI(cfg, printer=lambda line: None)
    try:
        probe = brain.engine("intel").probe_model()
        checks.append({"check": "local model", "ok": bool(probe.get("reachable")),
                       "detail": f"{cfg.intelligence.provider}/{cfg.intelligence.model}" +
                                 ("" if probe.get("reachable") else f" · {probe.get('reason', 'not reachable')}")})
        brain.boot()
        if cfg.mock or checks[2]["ok"]:
            try:
                cdp = brain.ctx.ensure_cdp()
                motion = cdp.motion_available()
                checks.append({"check": "Motion domain", "ok": motion,
                               "detail": "human input available" if motion else
                                         "missing — input would fall back to Input.* (visible to pages)"})
            except Exception as exc:  # noqa: BLE001
                checks.append({"check": "browser", "ok": False, "detail": str(exc)[:200]})
    finally:
        brain.shutdown()

    if getattr(args, "live", False):
        _live_checks(cfg, checks, args)

    if args.json:
        print(jdump(checks, indent=2))
    else:
        for item in checks:
            print(f"{'ok  ' if item['ok'] else 'FAIL'}  {item['check']:<14} {item['detail']}")
    # tesseract and a local model are genuinely optional; anything else that
    # fails (including every --live check) means SHADOW cannot drive this box
    optional = ("tesseract", "local model")
    return 0 if all(c["ok"] for c in checks if c["check"] not in optional) else 1


def _engines(brain: ShadowAI, args) -> int:
    health = brain.ctx.registry.health()
    if args.json:
        print(jdump(health, indent=2))
        return 0
    width = max(len(n) for n in health) if health else 10
    for name, entry in health.items():
        mark = "ok  " if entry.get("healthy") else "DOWN"
        state = entry.get("state", "?")
        extra = ""
        probe = entry.get("probe") or {}
        if "ocr" in probe:
            extra = f"ocr={probe['ocr']}"
        elif "provider" in probe:
            extra = f"{probe.get('provider')}"
        elif "attached" in probe:
            extra = f"alive={probe.get('alive')} degraded={probe.get('degraded')}"
        print(f"{mark}  {name:<{width}}  {state:<9} {extra}")
    return 0


def _demo(brain: ShadowAI, args) -> int:
    brain.ui.banner("SHADOW AI · demo", "an end-to-end run against the built-in mock")
    brain.boot()
    goals = [
        "Go to shop.local, fill the search field with lamp, press Search",
        "Open http://login.local/ user: admin pass: hunter2 then read the title",
        "Open http://slow.local/ and read the page",
        "Open http://shop.local/product/lamp and buy the Linen Lamp",
    ]
    if args.goal:
        goals = [args.goal]
    for goal in goals:
        result = brain.run(goal)
        brain.ui.notify("ok" if result.ok else "warn",
                        f"{result.status}: {truncate(goal, 60)}")
        if result.status == "needs_approval":
            brain.ui.notify("info", "approving it now (demo only: a real run waits for you)")
            result = brain.approve(result.task_id)
            brain.ui.notify("ok" if result.ok else "warn", f"after approval: {result.status}")
    report = brain.engine("supervisor").report()
    brain.ui.notify("ok", f"supervisor: {sum(1 for e in report['engines'].values() if e['healthy'])}"
                          f"/{len(report['engines'])} engines healthy, "
                          f"{len(report['incidents'])} incidents")
    return 0


def _serve(args, overrides: Dict[str, Any]) -> int:
    from .dashboard.server import Dashboard
    cfg = ShadowConfig.load(path=args.config or None, overrides=overrides)
    token = getattr(args, "dash_token", "") or args.token
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        token = secrets.token_urlsafe(18)
        print(f"this dashboard is reachable beyond this computer; token: {token}")
        print("open it with:  --token " + token + "   (or send X-Shadow-Token)")
    dash = Dashboard(cfg, host=args.host, port=args.port, token=token)
    dash.start()
    url = f"http://{args.host}:{dash.port}/"
    print(f"SHADOW dashboard: {url}")
    if not cfg.mock and not cfg.api_token and not cfg.launcher_settings:
        print("no token configured — the dashboard is up but cannot reach ShardX")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        dash.stop()
        print("\nstopped")
    return 0


# --------------------------------------------------------------------------
# printing
# --------------------------------------------------------------------------

def _print(value: Any, args) -> int:
    if args.json:
        print(jdump(value, indent=2) if not isinstance(value, str) else value)
    else:
        print(value if isinstance(value, str) else jdump(value, indent=2))
    return 0


def _status(brain: ShadowAI, args) -> int:
    status = brain.status()
    if args.json:
        print(jdump(status, indent=2))
        return 0
    ctx = status["context"]
    print(f"SHADOW AI {status['version']}   profile {ctx['profile'] or '-'}"
          f"{'   (mock)' if ctx['mock'] else ''}")
    print(f"browser attached: {ctx['attached']}   degraded input: {ctx['degraded']}")
    health = status["engines"]
    down = [n for n, e in health.items() if not e.get("healthy")]
    print(f"engines: {len(health) - len(down)}/{len(health)} healthy" + (f"   down: {', '.join(down)}" if down else ""))
    sup = status["supervisor"]
    print(f"supervisor: {sup['heartbeats']} heartbeats, {len(sup['quarantined'])} quarantined, "
          f"{len(sup['incidents'])} incidents")
    for task_id in status["journal"]["tasks"][-5:]:  # newest last
        print(f"  task {task_id}")
    print(f"journal: {status['journal']['path']}")
    return 0


def _result_text(result) -> str:
    lines = [f"{result.status.upper()}  {truncate(result.goal, 70)}"]
    for i, step in enumerate(result.steps, 1):
        mark = {"done": "+", "failed": "x", "skipped": "-", "needs_approval": "!",
                "pending": "·", "running": ">"}.get(step.status, "?")
        lines.append(f"  {mark} {i:2d}. {step.label()[:70]}")
        if step.status in ("done", "failed") and step.detail:
            lines.append(f"        {truncate(step.detail, 110)}")
    if result.results.get("answer"):
        lines.append(f"answer: {truncate(str(result.results['answer']), 300)}")
    if result.error:
        lines.append(f"error:  {truncate(result.error, 300)}")
    lines.append(f"task {result.task_id} · {result.duration_s if hasattr(result, 'duration_s') else round((result.ended_at or time.time()) - result.started_at, 2)}s")
    return "\n".join(lines)


def _answer_text(answer: Dict[str, Any]) -> str:
    if not answer.get("answer"):
        return f"no answer on this page ({answer.get('url', '')})"
    return f"{answer['answer']}\n\n— {answer.get('url', '')}"


def _memory_text(store) -> str:
    data = store.data
    lines = []
    for namespace, bucket in data.get("facts", {}).items():
        for key, entry in bucket.items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            lines.append(f"{namespace}/{key} = {truncate(str(value), 80)}")
    if data.get("checkpoints"):
        lines.append(f"checkpoints: {', '.join(data['checkpoints'])}")
    if data.get("lessons"):
        for site, lessons in data["lessons"].items():
            lines.append(f"lessons {site}: {', '.join(lessons)}")
    return "\n".join(lines) or "memory is empty"
