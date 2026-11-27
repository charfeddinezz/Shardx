"""Page events, reasoning, and the two safety promises: protected hosts and
typed secrets that never reach the disk."""

from __future__ import annotations

import unittest

from tests.helpers import MockCase

from shadow.errors import ApprovalRequired
from shadow.runtime.orchestrator import ShadowAI


class BrainCase(MockCase):
    def brain(self, **overrides) -> ShadowAI:
        brain = ShadowAI(self.config(**overrides), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain


class DialogTest(BrainCase):
    def test_a_confirm_dialog_is_seen_and_answered(self):
        brain = self.brain()
        result = brain.run("Open http://dialog.local/ and click the Rename account button")
        self.assertEqual(result.status, "done", result.error)
        dialogs = brain.engine("pages").report()["dialogs"]
        self.assertTrue(dialogs)
        self.assertEqual(dialogs[0]["kind"], "confirm")
        self.assertIn("Rename", dialogs[0]["message"])
        self.assertEqual(dialogs[0]["answered"], "accept")

    def test_a_dialog_does_not_wedge_the_session(self):
        brain = self.brain()
        brain.engine("pages")   # answers the confirm on its own
        brain.run("Open http://dialog.local/ and click the Rename account button")
        # proves Runtime.evaluate is unblocked again: this would hang otherwise
        self.assertEqual(brain.ctx.cdp.evaluate("document.readyState"), "complete")


class PopupTest(BrainCase):
    def test_a_popup_is_followed_and_recorded(self):
        brain = self.brain()
        result = brain.run("Open http://popup.local/ and click Continue with Example")
        self.assertEqual(result.status, "done", result.error)
        reports = brain.engine("pages").report()
        self.assertGreaterEqual(len(reports["tabs"]), 2)
        self.assertTrue(any(t.get("opener") for t in reports["tabs"]))
        self.assertGreaterEqual(reports["switches"], 1)
        self.assertIn("/consent", brain.ctx.cdp.current_url())

    def test_tabs_policy_ignore_leaves_the_session_on_the_original_tab(self):
        brain = self.brain(tabs_policy="ignore")
        result = brain.run("Open http://popup.local/ and click Continue with Example")
        self.assertEqual(result.status, "done", result.error)
        self.assertIn("popup.local/", brain.ctx.cdp.current_url())
        self.assertNotIn("consent", brain.ctx.cdp.current_url())

    def test_switch_tab_moves_the_session_and_back_again(self):
        brain = self.brain()
        pages = brain.engine("pages")
        brain.run("Open http://popup.local/ and click Continue with Example")
        back = pages.switch_tab("main")
        self.assertTrue(back["switched"], back)
        self.assertNotIn("consent", brain.ctx.cdp.current_url())


class ProtectedHostTest(BrainCase):
    def test_a_bank_host_needs_a_person(self):
        brain = self.brain()
        with self.assertRaises(ApprovalRequired):
            brain.engine("safety").check_url("https://www.paypal.com/signin")
        self.assertEqual(brain.engine("safety").check_url(
            "https://www.paypal.com/signin", approved=True).allowed, True)

    def test_an_ordinary_shop_is_not_protected(self):
        brain = self.brain()
        self.assertTrue(brain.engine("safety").check_url("http://shop.local/").allowed)

    def test_a_goal_to_a_protected_host_pauses_before_the_browser_opens(self):
        brain = self.brain()
        result = brain.run("Open https://www.bank-of-nowhere.example/ and read the title")
        self.assertEqual(result.status, "needs_approval", result.error)
        self.assertTrue(result.approvals)
        self.assertIsNone(brain.ctx.cdp)


class RedactionTest(BrainCase):
    def test_a_typed_password_is_never_written_to_the_journal(self):
        brain = self.brain()
        result = brain.run("Open http://login.local/ and log in with user: ahmed pass: s3cret-value",
                           approved=True)
        self.assertEqual(result.status, "done", result.error)
        raw = (self.tmp / "journal.jsonl").read_text()
        self.assertNotIn("s3cret-value", raw)
        self.assertIn("<redacted>", raw)

    def test_the_runs_own_goal_text_is_redacted(self):
        brain = self.brain()
        brain.run("Open http://login.local/ and log in with user: ahmed pass: s3cret-value",
                  approved=True)
        raw = (self.tmp / "journal.jsonl").read_text()
        self.assertIn("ahmed", raw)          # the user name is not a secret
        self.assertNotIn("s3cret-value", raw)

    def test_scrub_removes_values_from_any_nested_output(self):
        brain = self.brain()
        safety = brain.engine("safety")
        safety.note_typed("hunter2x", force=True)
        scrubbed = safety.scrub_output({"a": ["x hunter2x y"], "b": {"c": "hunter2x"}})
        self.assertEqual(scrubbed, {"a": ["x <redacted> y"], "b": {"c": "<redacted>"}})


class SafetyDefaultsTest(BrainCase):
    def test_os_input_and_shell_stay_off(self):
        brain = self.brain()
        safety = brain.engine("safety")
        self.assertFalse(safety.check_os_input().allowed)
        self.assertFalse(brain.cfg.safety.allow_system_commands)


class ReasonLoopTest(BrainCase):
    def test_a_navigation_goal_is_reasoned_and_explained(self):
        brain = self.brain()
        result = brain.run("Open http://shop.local/ and click Contact")
        self.assertEqual(result.status, "done", result.error)
        thoughts = result.results.get("thoughts") or []
        self.assertGreaterEqual(len(thoughts), 2)
        self.assertEqual(thoughts[0]["action"], "goto")
        self.assertTrue(all(t.get("reason") for t in thoughts))
        self.assertEqual(result.results.get("turns"), len(thoughts))

    def test_a_read_goal_is_answered_without_the_local_model(self):
        brain = self.brain()
        result = brain.run("Open http://shop.local/product/lamp and read the price")
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(result.results.get("answer"), "$48")
        thoughts = result.results.get("thoughts") or []
        self.assertTrue(thoughts)
        self.assertEqual({t["source"] for t in thoughts}, {"plan"})

    def test_the_loop_stops_rather_than_clicking_the_same_wall(self):
        brain = self.brain()
        result = brain.run("Open http://shop.local/about and click the orbital launch button")
        self.assertEqual(result.status, "failed")
        self.assertIn("orbital launch", result.error)


if __name__ == "__main__":
    unittest.main()
