"""The assembled system: goals end to end, the supervisor's three jobs
(watch, restart, resume), decisions, scheduling, the dashboard and the CLI."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import MockCase

from shadow.core.config import ShadowConfig
from shadow.core.events import EventBus
from shadow.core.journal import Journal
from shadow.engines.base import Engine
from shadow.engines.decision import DecisionEngine
from shadow.engines.scheduler import SchedulerEngine
from shadow.engines.supervisor import AutonomousSupervisor
from shadow.errors import ApprovalRequired
from shadow.runtime.orchestrator import ShadowAI


class BrainCase(MockCase):
    """A booted SHADOW AI on the mock launcher, torn down afterwards."""

    def brain(self, **overrides) -> ShadowAI:
        cfg = self.config(**overrides)
        brain = ShadowAI(cfg, printer=lambda line: None)
        brain.ctx._mock = self.mock          # share this test's launcher
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain


class EndToEndTest(BrainCase):
    def test_search_goal_reaches_the_results_page(self):
        brain = self.brain()
        result = brain.run("Go to shop.local, fill the search field with lamp, press Search")
        self.assertEqual(result.status, "done", result.error)
        self.assertIn("/search?q=lamp", brain.ctx.cdp.current_url())
        self.assertEqual([s.status for s in result.steps], ["done"] * len(result.steps))

    def test_login_goal_signs_in_and_reads_the_title(self):
        brain = self.brain()
        result = brain.run("Open http://login.local/ user: admin pass: hunter2 then read the title")
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(result.results.get("answer"), "Dashboard")
        self.assertIn("/app", brain.ctx.cdp.current_url())

    def test_read_goal_answers_from_the_page_without_a_model(self):
        brain = self.brain()
        result = brain.run("Open http://shop.local/product/lamp and read the price")
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(result.results.get("answer"), "$48")

    def test_purchase_pauses_then_completes_after_a_human_approves(self):
        brain = self.brain()
        paused = brain.run("Open http://shop.local/product/lamp and buy the Linen Lamp")
        self.assertEqual(paused.status, "needs_approval")
        self.assertTrue(paused.approvals)
        resumed = brain.approve(paused.task_id)
        self.assertEqual(resumed.status, "done", resumed.error)

    def test_plan_is_printable_before_anything_runs(self):
        brain = self.brain()
        task = brain.plan("Open http://login.local/ user: a pass: b then click Sign in")
        self.assertEqual([s.action for s in task.steps][0], "goto")
        self.assertIn("login", [s.action for s in task.steps])
        self.assertIsNone(brain.ctx.cdp)   # planning touched no browser

    def test_ask_reads_a_url_directly(self):
        brain = self.brain()
        answer = brain.ask("where is the oak desk?", url="http://shop.local/")
        self.assertIn("Oak Desk", answer["answer"])

    def test_a_goal_that_cannot_be_done_fails_with_a_reason(self):
        brain = self.brain()
        result = brain.run("Open http://shop.local/about and click the orbital launch button")
        self.assertEqual(result.status, "failed")
        self.assertIn("orbital launch", result.error)

    def test_arabic_goal_logs_in_and_reads_the_title(self):
        brain = self.brain()
        result = brain.run("افتح http://login.local/ user: admin pass: hunter2 ثم اقرأ العنوان")
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(result.results.get("answer"), "Dashboard")

    def test_french_goal_fills_the_search_field_and_submits(self):
        brain = self.brain()
        result = brain.run("Ouvre http://shop.local/ puis remplis le champ de recherche "
                           "avec lamp et clique Search")
        self.assertEqual(result.status, "done", result.error)
        self.assertIn("/search?q=lamp", brain.ctx.cdp.current_url())

    def test_arabic_goal_fills_the_email_field_on_the_contact_page(self):
        brain = self.brain()
        result = brain.run("املأ حقل البريد بـ a@b.co في http://shop.local/contact")
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(brain.ctx.probe.value("email"), "a@b.co")

    def test_journal_records_the_whole_run(self):
        brain = self.brain()
        result = brain.run("Open http://shop.local/ and read the title")
        kinds = {e.kind for e in brain.journal.entries(task_id=result.task_id)}
        self.assertIn("task.started", kinds)
        self.assertIn("step.done", kinds)
        self.assertIn("task.ended", kinds)

    def test_run_is_idempotent_about_attaching(self):
        brain = self.brain()
        brain.run("Open http://shop.local/ and read the title")
        first = brain.ctx.cdp
        brain.run("Open http://shop.local/about and read the title")
        self.assertIs(brain.ctx.cdp, first)


class SupervisorTest(BrainCase):
    def test_every_engine_is_healthy_after_boot(self):
        brain = self.brain()
        health = brain.ctx.registry.health()
        self.assertEqual(len(health), 17)
        self.assertEqual([n for n, e in health.items() if not e["healthy"]], [])

    def test_a_dead_engine_is_restarted(self):
        brain = self.brain()
        engine = brain.engine("discovery")
        engine.start = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

        class Sick(Engine):
            name = "sick"
            def __init__(self, ctx):
                super().__init__(ctx)
                self.alive = False
                self.attempts = 0
            def start(self):
                self.attempts += 1
                if self.attempts > 1:
                    self.alive = True
                    return super().start()
                raise RuntimeError("cannot come up")
            def health(self):
                return {"healthy": self.alive}

        sick = Sick(brain.ctx)
        brain.ctx.registry.register("sick", sick)
        brain.ctx.registry.record("sick").state = "ready"
        supervisor: AutonomousSupervisor = brain.engine("supervisor")
        first = supervisor.check_once()      # the engine refuses to come up
        self.assertIn("sick", first["failing"])
        second = supervisor.check_once()     # a second try succeeds
        self.assertIn("sick", second["restarted"])
        self.assertTrue(sick.alive)

    def test_an_engine_that_will_not_come_up_is_quarantined_and_can_be_released(self):
        brain = self.brain()
        supervisor: AutonomousSupervisor = brain.engine("supervisor")
        supervisor.cfg.max_restarts = 1

        class Broken(Engine):
            name = "boom"
            def __init__(self, ctx):
                super().__init__(ctx)
                self.up = False        # it is down, and cannot start
            def start(self):
                raise RuntimeError("still broken")
            def health(self):
                return {"healthy": self.up}

        boom = Broken(brain.ctx)
        brain.ctx.registry.register("boom", boom)
        brain.ctx.registry.record("boom").state = "ready"
        report = supervisor.check_once()   # the one restart allowed fails: quarantined
        self.assertIn("boom", report["quarantined"])
        self.assertIn("boom", supervisor.quarantined)
        # a second pass leaves it alone
        supervisor.check_once()
        self.assertIn("boom", supervisor.quarantined)
        # once fixed by a human, it goes back under watch
        boom.start = lambda: setattr(boom, "_started", True)
        self.assertTrue(supervisor.release("boom"))
        self.assertNotIn("boom", supervisor.quarantined)

    def test_an_interrupted_task_is_resumed_from_its_last_step(self):
        brain = self.brain()
        goal = "Open http://login.local/ user: admin pass: hunter2 then read the title"
        # simulate a run that died after step 0: the journal has the task, no end
        journal = Journal(brain.cfg.journal_path(), sync=False)
        journal.append("task.started", "t-crashed", -1, goal=goal, profile="win-rtx4060",
                       steps=[{"action": "goto"}])
        journal.append("step.done", "t-crashed", 0, action="goto")
        journal.append("step.started", "t-crashed", 1, action="login")
        journal.close()

        # the supervisor only resumes a task that has been quiet for a while
        brain.cfg.supervisor.heartbeat_timeout_s = 0
        resumed = brain.engine("supervisor").resume_interrupted()
        self.assertIn("t-crashed", resumed)
        entries = brain.journal.entries(task_id="t-crashed")
        self.assertIn("task.ended", {e.kind for e in entries})
        last = [e for e in entries if e.kind == "task.ended"][-1]
        self.assertEqual(last.data["status"], "done")

    def test_a_running_task_is_not_resumed_twice(self):
        brain = self.brain()
        workflow = brain.engine("workflow")
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(10)
        workflow._do = lambda task, step, index: slow()   # every step blocks
        thread = threading.Thread(target=lambda: brain.run("Open http://shop.local/ and read the title"))
        thread.start()
        started.wait(5)
        brain.cfg.supervisor.heartbeat_timeout_s = 0
        self.assertEqual(brain.engine("supervisor").resume_interrupted(), [])
        release.set()
        thread.join(15)

    def test_report_lists_engines_and_incidents(self):
        brain = self.brain()
        report = brain.engine("supervisor").report()
        self.assertEqual(len(report["engines"]), 17)
        self.assertIn("watching", report)


class DecisionTest(BrainCase):
    def test_off_page_target_from_a_model_is_rejected(self):
        brain = self.brain()
        decision: DecisionEngine = brain.engine("decision")
        page = {"url": "http://shop.local/", "title": "Shop",
                "elements": [{"selector": "#go", "name": "Search", "tag": "button"}]}
        self.assertIsNone(decision._validate({"action": "click", "target": "#nope"}, page))
        self.assertIsNotNone(decision._validate({"action": "click", "target": "#go"}, page))

    def test_forbidden_action_from_a_model_is_rejected(self):
        brain = self.brain()
        decision: DecisionEngine = brain.engine("decision")
        page = {"url": "", "title": "", "elements": []}
        self.assertIsNone(decision._validate({"action": "shell", "target": "rm -rf /"}, page))
        self.assertIsNone(decision._validate({"action": "pay", "target": "x"}, page))

    def test_page_driven_decision_finds_a_matching_element(self):
        brain = self.brain()
        brain.ctx.ensure_cdp().navigate("http://shop.local/")
        model = brain.engine("page").understand(brain.ctx.probe, brain.ctx.cdp)
        decision = brain.engine("decision").choose("search for lamp", model.to_dict(), [], allow_model=False)
        self.assertIn(decision.action, ("type", "click"))

    def test_model_bridge_is_off_by_default_and_never_network_calls(self):
        brain = self.brain()
        self.assertIsNone(brain.engine("intel").reason("x", {"elements": []}, []))
        self.assertFalse(brain.engine("intel").probe_model()["reachable"])


class SchedulerTest(BrainCase):
    def test_a_job_runs_on_its_interval(self):
        brain = self.brain()
        scheduler: SchedulerEngine = brain.engine("scheduler")
        ran = []
        scheduler.every("probe", lambda: ran.append(1), 0.1, limit=3)
        deadline = time.time() + 5
        while len(ran) < 3 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(ran), 3)

    def test_a_failing_job_does_not_stop_the_scheduler(self):
        brain = self.brain()
        scheduler: SchedulerEngine = brain.engine("scheduler")

        def explode():
            raise RuntimeError("nope")
        job = scheduler.every("bad", explode, 0.1, limit=2)
        deadline = time.time() + 5
        while job.failures < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(job.failures, 2)
        self.assertTrue(scheduler.health()["running"])


class DashboardTest(BrainCase):
    def test_state_run_and_approve_over_http(self):
        from shadow.dashboard.server import Dashboard
        cfg = self.config()
        brain = ShadowAI(cfg, printer=lambda line: None)
        brain.ctx._mock = self.mock
        dash = Dashboard(cfg, host="127.0.0.1", port=0, brain=brain).start()
        self.addCleanup(dash.stop)
        base = f"http://127.0.0.1:{dash.port}"

        page = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        self.assertIn("<title>SHADOW AI</title>", page)
        state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=10).read())
        self.assertTrue(state["status"]["context"]["mock"])
        self.assertFalse(state["running"])

        def post(path, payload):
            req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            return urllib.request.urlopen(req, timeout=20)

        self.assertEqual(post("/api/run", {"goal": "Open http://shop.local/ and read the title"}).status, 200)
        deadline = time.time() + 20
        while time.time() < deadline:
            state = json.loads(urllib.request.urlopen(base + "/api/state", timeout=10).read())
            if state["last"]:
                break
            time.sleep(0.2)
        self.assertEqual(state["last"]["status"], "done")

        with self.assertRaises(urllib.error.HTTPError) as caught:
            post("/api/run", {"goal": ""})
        self.assertEqual(caught.exception.code, 400)


class CliTest(BrainCase):
    def run_cli(self, *argv):
        from shadow.cli import main
        import io
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--mock", "--state", str(self.tmp)] + list(argv))
        return code, out.getvalue()

    def test_plan_prints_steps_without_running_them(self):
        code, out = self.run_cli("plan", "Open shop.local and read the title")
        self.assertEqual(code, 0)
        self.assertIn("steps", out)
        self.assertIn("goto", out)

    def test_run_json_reports_a_machine_readable_result(self):
        code, out = self.run_cli("run", "--json", "Open http://shop.local/ and read the title")
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "done")
        self.assertIn("Sundial Shop", payload["results"]["answer"])

    def test_doctor_reports_the_mock_and_the_engines(self):
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("launcher", out)
        self.assertIn("mock", out)

    def test_status_and_engines(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("engines", out)
        code, out = self.run_cli("engines")
        self.assertEqual(code, 0)
        self.assertIn("supervisor", out)

    def test_approve_unknown_task_is_reported(self):
        from shadow.cli import main
        with self.assertRaises(ValueError):
            main(["--mock", "--state", str(self.tmp), "approve", "t-nope"])


if __name__ == "__main__":
    unittest.main()
