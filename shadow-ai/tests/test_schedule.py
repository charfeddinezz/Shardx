"""Saved goals on a clock: written down, restored once, and trimmed."""

from __future__ import annotations

import json
import time
import unittest

from tests.helpers import MockCase

from shadow.engines.workflow import Step
from shadow.runtime.orchestrator import ShadowAI


class ScheduleCase(MockCase):
    def brain(self, state: str = "") -> ShadowAI:
        overrides = {"state_dir": state or str(self.tmp)}
        brain = ShadowAI(self.config(**overrides), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain

    def jobs_file(self, brain: ShadowAI):
        return brain.ctx.cfg.state_path() / "jobs.json"


class PersistenceTest(ScheduleCase):
    def test_a_saved_goal_survives_a_restart_exactly_once(self):
        brain = self.brain()
        brain.engine("scheduler").every_goal("title check",
                                             "Open http://shop.local/ and read the title", 60)
        saved = json.loads(self.jobs_file(brain).read_text())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["name"], "title check")
        brain.shutdown()

        for _ in range(3):                     # booting repeatedly must not multiply it
            again = self.brain()
            goals = [j for j in again.engine("scheduler").jobs.values() if j.goal]
            self.assertEqual(len(goals), 1)
            self.assertEqual(len(json.loads(self.jobs_file(again).read_text())), 1)
            again.shutdown()

    def test_the_housekeeping_job_is_not_saved(self):
        brain = self.brain()
        kinds = {j.name for j in brain.engine("scheduler").jobs.values()}
        self.assertIn("journal compaction", kinds)
        self.assertEqual(json.loads(self.jobs_file(brain).read_text())
                         if self.jobs_file(brain).exists() else [], [])

    def test_pausing_and_resuming_is_remembered(self):
        brain = self.brain()
        job = brain.engine("scheduler").every_goal("x", "goal X", 60)
        brain.engine("scheduler").pause(job.id)
        brain.shutdown()
        again = self.brain()
        restored = [j for j in again.engine("scheduler").pending() if j.goal]
        self.assertEqual(restored, [])
        paused = [j for j in again.engine("scheduler").jobs.values() if j.goal]
        self.assertTrue(paused[0].paused)

    def test_cancelling_removes_it_from_disk(self):
        brain = self.brain()
        job = brain.engine("scheduler").every_goal("x", "goal X", 60)
        self.assertTrue(brain.engine("scheduler").cancel(job.id))
        self.assertEqual(json.loads(self.jobs_file(brain).read_text()), [])
        self.assertFalse(brain.engine("scheduler").cancel(job.id))

    def test_a_corrupt_jobs_file_is_ignored_not_fatal(self):
        brain = self.brain()
        self.jobs_file(brain).write_text("{not json")
        m = brain.engine("scheduler")
        self.assertEqual(m.load(), 0)


class FiringTest(ScheduleCase):
    def test_a_saved_job_runs_its_goal(self):
        brain = self.brain()
        seen = []

        def fake(goal):
            seen.append(goal)

            class R:
                status = "done"
            return R()

        brain.ctx._run_goal = fake
        brain.engine("scheduler").every_goal("x", "Open shop.local", 0.05)
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(seen, ["Open shop.local"])

    def test_a_failing_job_does_not_stop_the_scheduler(self):
        brain = self.brain()
        scheduler = brain.engine("scheduler")

        def boom():
            raise RuntimeError("a job that fails")

        job = scheduler.every("boom", boom, 0.05)
        deadline = time.time() + 5
        while job.failures < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(job.failures, 2)
        self.assertTrue(scheduler._thread.is_alive())


class CompactionTest(ScheduleCase):
    def test_the_journal_is_trimmed_keeping_the_tail(self):
        brain = self.brain()
        for i in range(30):
            brain.journal.append("noise", "t", i, i=i)
        before = self.jobs_file(brain).parent.joinpath("journal.jsonl").read_text().splitlines()
        dropped = brain.engine("scheduler").compact_journal(keep_last=10)
        self.assertEqual(dropped, len(before) - 10)
        lines = self.jobs_file(brain).parent.joinpath("journal.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 10)
        # and the live handle still writes after the rewrite
        brain.journal.append("after", "t", 99, ok=True)
        self.assertIn("after", self.jobs_file(brain).parent.joinpath("journal.jsonl").read_text())


class ScheduleDashboardTest(ScheduleCase):
    def test_add_pause_cancel_over_http(self):
        import urllib.error
        import urllib.request
        from shadow.core.config import ShadowConfig
        from shadow.dashboard.server import Dashboard

        cfg = ShadowConfig.load(overrides={"mock": True, "state_dir": str(self.tmp),
                                           "default_profile": "win-rtx4060"})
        brain = ShadowAI(cfg, printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        dash = Dashboard(cfg, host="127.0.0.1", port=0, brain=brain).start()
        self.addCleanup(dash.stop)
        base = f"http://127.0.0.1:{dash.port}"

        def post(payload):
            request = urllib.request.Request(base + "/api/schedule",
                                             data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"},
                                             method="POST")
            return json.loads(urllib.request.urlopen(request, timeout=10).read())

        def get():
            return json.loads(urllib.request.urlopen(base + "/api/schedule", timeout=10).read())

        job = post({"goal": "Open http://shop.local/ and read the title",
                    "minutes": 15, "name": "dash job"})
        self.assertEqual(job["every_s"], 900.0)
        self.assertEqual([j["name"] for j in get()["jobs"]], ["dash job"])
        self.assertTrue(post({"action": "pause", "job_id": job["id"]})["ok"])
        self.assertTrue(post({"action": "resume", "job_id": job["id"]})["ok"])
        self.assertTrue(post({"action": "cancel", "job_id": job["id"]})["ok"])
        self.assertEqual(get()["jobs"], [])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            post({"goal": ""})
        self.assertEqual(caught.exception.code, 400)


class ScheduleCliTest(ScheduleCase):
    def test_schedule_list_and_cancel_through_the_cli(self):
        from shadow.cli import main
        state = str(self.tmp)
        argv = ["--mock", "--state", state, "--quiet"]
        self.assertEqual(main(argv + ["schedule", "Open shop.local and read the title",
                                      "--every", "15", "--name", "quarter hourly"]), 0)
        saved = json.loads((self.tmp / "jobs.json").read_text())
        self.assertEqual(saved[0]["name"], "quarter hourly")
        self.assertEqual(saved[0]["every_s"], 900)
        self.assertEqual(main(argv + ["schedule", "--cancel", saved[0]["id"]]), 0)
        self.assertEqual(json.loads((self.tmp / "jobs.json").read_text()), [])
        self.assertEqual(main(argv + ["schedule", "--cancel", "no-such-job"]), 1)


if __name__ == "__main__":
    unittest.main()
