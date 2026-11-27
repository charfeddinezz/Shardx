"""The fleet: several profiles, one goal (or several), bounded concurrency,
and the same safety rules as a single run."""

from __future__ import annotations

import threading
import time
import unittest

from tests.helpers import MockCase

from shadow.engines.fleet import Fleet, FleetJob
from shadow.runtime.orchestrator import ShadowAI


class FleetCase(MockCase):
    def brain(self, **overrides) -> ShadowAI:
        brain = ShadowAI(self.config(**overrides), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain


class FanOutTest(FleetCase):
    def test_one_goal_on_three_profiles_finishes_on_all_of_them(self):
        brain = self.brain()
        self.assertEqual(len(brain.profiles()), 3)
        jobs = brain.fan_out("Open http://shop.local/ and read the title")
        self.assertEqual(len(jobs), 3)
        self.assertEqual([j["status"] for j in jobs], ["done"] * 3)
        self.assertEqual({j["answer"] for j in jobs}, {"Sundial Shop"})
        self.assertEqual({j["profile"] for j in jobs}, set(brain.profiles()))

    def test_each_worker_has_its_own_browser_and_state(self):
        brain = self.brain()
        brain.fan_out("Open http://shop.local/ and read the title",
                      ["win-rtx4060", "mac-studio"])
        clients = [w.ctx.cdp for w in brain._workers]
        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0], clients[1])
        self.assertNotEqual(clients[0].target_id, clients[1].target_id)
        for worker in brain._workers:
            self.assertTrue((self.tmp / "fleet" / worker.cfg.default_profile).is_dir())

    def test_a_named_subset_is_respected(self):
        brain = self.brain()
        jobs = brain.fan_out("Open http://shop.local/ and read the title", ["linux-box"])
        self.assertEqual([j["profile"] for j in jobs], ["linux-box"])

    def test_workers_seed_their_motion_differently(self):
        brain = self.brain()
        brain.fan_out("Open http://shop.local/ and read the title",
                      ["win-rtx4060", "mac-studio"])
        seeds = {w.cfg.human.pointer_seed for w in brain._workers}
        self.assertEqual(len(seeds), len(brain._workers))

    def test_parallel_jobs_really_overlap(self):
        """Three workers on three different clocks: total time is well under
        the sum of the parts."""
        started = {}
        lock = threading.Lock()

        class Slow:
            def __init__(self, name):
                self.name = name

            def run(self, goal, approved=False):
                with lock:
                    started[self.name] = time.time()
                time.sleep(0.4)

                class R:
                    task_id = self.name
                    status = "done"
                    error = ""
                    steps = []
                    results = {"answer": self.name}
                return R()

            def shutdown(self):
                pass

        fleet = Fleet(lambda profile: Slow(profile), max_parallel=3)
        t0 = time.time()
        jobs = fleet.run([FleetJob("g", "a"), FleetJob("g", "b"), FleetJob("g", "c")])
        elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0)                  # not 1.2s
        self.assertEqual([j.status for j in jobs], ["done"] * 3)
        self.assertEqual(fleet.report()["parallel"], 3)

    def test_max_parallel_bounds_the_pool(self):
        active, peak = [0], [0]
        lock = threading.Lock()

        class Probe:
            def run(self, goal, approved=False):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.15)
                with lock:
                    active[0] -= 1

                class R:
                    task_id = "t"
                    status = "done"
                    error = ""
                    steps = []
                    results = {}
                return R()

        fleet = Fleet(lambda profile: Probe(), max_parallel=2)
        fleet.run([FleetJob("g", str(i)) for i in range(5)])
        self.assertLessEqual(peak[0], 2)
        self.assertEqual(len(fleet.jobs), 5)


class FleetSafetyTest(FleetCase):
    def test_a_protected_host_in_a_fan_out_never_starts_a_worker(self):
        brain = self.brain()
        jobs = brain.fan_out("Open https://www.paypal.com/ and read the title",
                             ["win-rtx4060", "mac-studio"])
        self.assertEqual([j["status"] for j in jobs], ["needs_approval"] * 2)
        self.assertTrue(all("protected" in j["error"] or "paypal" in j["error"] for j in jobs))
        self.assertEqual(brain._workers, [])

    def test_a_worker_that_raises_does_not_take_the_fleet_down(self):
        def factory(profile):
            if profile == "boom":
                class Bad:
                    def run(self, goal, approved=False):
                        raise RuntimeError("this worker fell over")
                    def shutdown(self):
                        pass
                return Bad()
            class Good:
                def run(self, goal, approved=False):
                    class R:
                        task_id = "t"
                        status = "done"
                        error = ""
                        steps = []
                        results = {}
                    return R()
                def shutdown(self):
                    pass
            return Good()
        fleet = Fleet(factory, max_parallel=2)
        jobs = fleet.run([FleetJob("g", "boom"), FleetJob("g", "ok")])
        statuses = sorted(j.status for j in jobs)
        self.assertEqual(statuses, ["done", "failed"])
        self.assertIn("fell over", [j for j in jobs if j.status == "failed"][0].error)


class FleetReportTest(FleetCase):
    def test_the_dashboard_and_cli_see_the_same_report(self):
        brain = self.brain()
        brain.fan_out("Open http://shop.local/ and read the title", ["mac-studio"])
        report = brain.engine("fleet").report()
        self.assertEqual(len(report["jobs"]), 1)
        self.assertEqual(report["profiles"], ["mac-studio"])
        self.assertEqual(report["running"], 0)
        health = brain.engine("fleet").health()
        self.assertEqual(health["workers"], 1)

    def test_shutdown_closes_the_workers(self):
        brain = ShadowAI(self.config(), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        brain.fan_out("Open http://shop.local/ and read the title", ["mac-studio"])
        worker = brain._workers[0]
        brain.shutdown()
        self.assertIsNone(worker.ctx.cdp)


if __name__ == "__main__":
    unittest.main()
