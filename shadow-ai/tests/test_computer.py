"""The shell engine: off unless a person turns it on, and narrow when it is on.

The defaults are the feature. These tests exist mostly to keep them.
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import MockCase

from shadow.errors import SafetyViolation
from shadow.runtime.orchestrator import ShadowAI


class ComputerCase(MockCase):
    def brain(self, **overrides) -> ShadowAI:
        brain = ShadowAI(self.config(**overrides), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain


class DefaultOffTest(ComputerCase):
    def test_shell_is_refused_by_default(self):
        engine = self.brain().engine("computer")
        with self.assertRaises(SafetyViolation) as caught:
            engine.run("echo", ["hello"])
        self.assertIn("off", str(caught.exception))
        self.assertIn("SHADOW_ALLOW_SYSTEM_COMMANDS", str(caught.exception))

    def test_the_refusal_names_what_would_enable_it(self):
        engine = self.brain().engine("computer")
        with self.assertRaises(SafetyViolation):
            engine.run_line("echo hello")

    def test_info_reports_the_state_without_running_anything(self):
        info = self.brain().engine("computer").info()
        self.assertFalse(info["enabled"])
        self.assertIn("echo", info["allowed"])


class EnabledTest(ComputerCase):
    def enabled(self, **overrides):
        safety = {"allow_system_commands": True,
                  "allowed_commands": ["echo", "python3"]}
        safety.update(overrides.pop("safety", {}))
        return self.brain(safety=safety, **overrides)

    def test_an_allowed_program_runs_without_a_shell(self):
        out = self.enabled().engine("computer").run("echo", ["hello", "world"])
        self.assertTrue(out["ok"], out)
        self.assertIn("hello world", out["stdout"])

    def test_a_program_outside_the_list_is_refused(self):
        engine = self.enabled().engine("computer")
        with self.assertRaises(SafetyViolation) as caught:
            engine.run("curl", ["http://example.com"])
        self.assertIn("allowed_commands", str(caught.exception))

    def test_shell_metacharacters_are_arguments_not_a_shell(self):
        out = self.enabled().engine("computer").run("echo", ["$(whoami)", ";", "rm", "-rf", "/"])
        self.assertIn("$(whoami)", out["stdout"])       # passed through literally
        self.assertIn("rm -rf /", out["stdout"])

    def test_nul_bytes_are_refused(self):
        with self.assertRaises(SafetyViolation):
            self.enabled().engine("computer").run("echo", ["a\x00b"])

    def test_a_missing_program_says_so(self):
        brain = self.brain(safety={"allow_system_commands": True,
                                   "allowed_commands": ["definitely-not-here"]})
        with self.assertRaises(SafetyViolation) as caught:
            brain.engine("computer").run("definitely-not-here", [])
        self.assertIn("not installed", str(caught.exception))

    def test_a_timeout_is_a_result_not_an_exception(self):
        brain = self.brain(safety={"allow_system_commands": True,
                                   "allowed_commands": ["sleep"]})
        out = brain.engine("computer").run("sleep", ["5"], timeout=0.3)
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])

    def test_a_nonzero_exit_is_reported_with_its_code(self):
        script = "import sys; sys.exit(3)"
        out = self.enabled().engine("computer").run("python3", ["-c", script])
        self.assertEqual(out["code"], 3)
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
