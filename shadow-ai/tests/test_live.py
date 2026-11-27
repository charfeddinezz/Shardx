"""The live path: launcher API → token → profile start → CDP → a real goal.

`tests/fake_launcher.py` mirrors this repository's `src-tauri/src/api.rs`, so
these tests exercise the code that talks to a genuine ShardX install. Only
Chrome itself is faked, by the mock CDP server.
"""

from __future__ import annotations

import json
import unittest

from tests.fake_launcher import FakeLauncher
from tests.helpers import MockCase

from shadow.core.config import ShadowConfig
from shadow.runtime.orchestrator import ShadowAI
from shadow.transport.shardx import ShardxApi


class LiveCase(MockCase):
    """A mock CDP browser published through a launcher that looks real."""

    def setUp(self):
        super().setUp()
        # the mock's first profile gets its own CDP port; we hand that address
        # to the fake launcher the way a real start would
        self.cdp_port = self.mock._open_engine(self.mock.profiles["main-profile"]
                                               if "main-profile" in self.mock.profiles
                                               else list(self.mock.profiles.values())[0], True)
        self.launcher = FakeLauncher(token="live-token",
                                     cdp_url=f"http://127.0.0.1:{self.cdp_port}",
                                     profiles=["main-profile", "second-profile"])
        self.addCleanup(self.launcher.stop)

    def live_config(self, **overrides) -> ShadowConfig:
        base = {"mock": False, "state_dir": str(self.tmp),
                "api_base": self.launcher.base_url, "api_token": "live-token",
                "default_profile": "main-profile"}
        base.update(overrides)
        return ShadowConfig.load(overrides=base)


class LauncherApiTest(LiveCase):
    def test_health_is_open_and_profiles_needs_the_token(self):
        api = ShardxApi(self.launcher.base_url, token="")
        self.assertTrue(api.health()["ok"])
        with self.assertRaises(Exception):
            api.profiles()
        api = ShardxApi(self.launcher.base_url, token="live-token")
        self.assertEqual([p["id"] for p in api.profiles()],
                         ["main-profile", "second-profile"])

    def test_profiles_lists_ids_and_cdp_is_null_until_started(self):
        api = ShardxApi(self.launcher.base_url, token="live-token")
        self.assertIsNone(api.profiles()[0]["cdp"])

    def test_start_returns_the_cdp_block(self):
        api = ShardxApi(self.launcher.base_url, token="live-token")
        started = api.start("main-profile", headless=True)
        self.assertEqual(started["cdp"]["http_url"], f"http://127.0.0.1:{self.cdp_port}")

    def test_cdp_for_reuses_a_running_profile_and_starts_a_stopped_one(self):
        api = ShardxApi(self.launcher.base_url, token="live-token")
        first = api.cdp_for("main-profile")
        second = api.cdp_for("main-profile")
        self.assertEqual(first, second)
        starts = [r for r in self.launcher.requests if r["path"].endswith("/start")]
        self.assertEqual(len(starts), 1)          # the second call reused it


class LiveAttachTest(LiveCase):
    def test_the_runtime_attaches_through_the_launcher(self):
        brain = ShadowAI(self.live_config(), printer=lambda line: None)
        self.addCleanup(brain.shutdown)
        brain.boot()
        cdp = brain.ctx.ensure_cdp("main-profile")
        self.assertTrue(cdp.motion_available())
        self.assertEqual(brain.ctx.profile_id, "main-profile")
        starts = [r for r in self.launcher.requests if r["path"].endswith("/start")]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["body"].get("headless"), False)

    def test_a_goal_runs_end_to_end_over_the_api(self):
        brain = ShadowAI(self.live_config(), printer=lambda line: None)
        self.addCleanup(brain.shutdown)
        brain.boot()
        result = brain.run("Open http://shop.local/ and read the title")
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(result.results.get("answer"), "Sundial Shop")

    def test_profiles_come_from_the_api_not_the_mock(self):
        brain = ShadowAI(self.live_config(), printer=lambda line: None)
        self.addCleanup(brain.shutdown)
        self.assertEqual(brain.profiles(), ["main-profile", "second-profile"])

    def test_a_profile_that_will_not_start_fails_with_the_launchers_reason(self):
        self.launcher.fail_start = "main-profile"
        brain = ShadowAI(self.live_config(), printer=lambda line: None)
        self.addCleanup(brain.shutdown)
        with self.assertRaises(Exception) as caught:
            brain.ctx.ensure_cdp("main-profile")
        self.assertIn("engine did not start", str(caught.exception))


class DoctorLiveTest(LiveCase):
    def test_doctor_live_passes_against_a_real_api(self):
        from shadow.cli import build_parser, _doctor
        args = build_parser().parse_args(
            ["--api-base", self.launcher.base_url, "--token", "live-token",
             "--state", str(self.tmp), "--profile", "main-profile",
             "doctor", "--live", "--live-url", "http://shop.local/"])
        code = _doctor(self.live_config(), args)
        self.assertEqual(code, 0)

    def test_doctor_live_fails_when_the_launcher_is_not_there(self):
        from shadow.cli import build_parser, _doctor
        args = build_parser().parse_args(
            ["--api-base", "http://127.0.0.1:9", "--token", "live-token",
             "--state", str(self.tmp), "doctor", "--live"])
        cfg = ShadowConfig.load(overrides={"mock": False, "state_dir": str(self.tmp),
                                           "api_base": "http://127.0.0.1:9",
                                           "api_token": "live-token"})
        self.assertEqual(_doctor(cfg, args), 1)


if __name__ == "__main__":
    unittest.main()
