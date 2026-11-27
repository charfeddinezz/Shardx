"""Every verb, on its own, through the engine that carries it out.

`ActionsEngine` is the seam a plan, the reasoning loop and the CLI all go
through, so each verb is worth one test of its own.
"""

from __future__ import annotations

import unittest

from tests.helpers import MockCase

from shadow.engines.workflow import Step
from shadow.errors import ElementNotFound
from shadow.runtime.orchestrator import ShadowAI


class ActionCase(MockCase):
    def brain(self, **overrides) -> ShadowAI:
        brain = ShadowAI(self.config(**overrides), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain

    def do(self, brain: ShadowAI, action: str, target: str = "", value: str = ""):
        return brain.engine("actions").perform(Step(action, target=target, value=value))


class NavigationTest(ActionCase):
    def test_goto_returns_url_and_title(self):
        brain = self.brain()
        out = self.do(brain, "goto", "http://shop.local/")
        self.assertEqual(out["url"], "http://shop.local/")
        self.assertEqual(out["title"], "Sundial Shop")

    def test_back_and_forward(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/")
        self.do(brain, "goto", "http://shop.local/about")
        self.assertEqual(self.do(brain, "back")["url"], "http://shop.local/")
        self.assertEqual(self.do(brain, "forward")["url"], "http://shop.local/about")

    def test_reload(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/")
        self.assertEqual(self.do(brain, "reload")["url"], "http://shop.local/")

    def test_verify_loaded(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/")
        self.assertTrue(self.do(brain, "verify_loaded")["loaded"])

    def test_a_blocked_host_is_refused_by_the_action_itself(self):
        brain = self.brain()
        with self.assertRaises(Exception):
            self.do(brain, "goto", "http://localhost:40325/health")


class FormsTest(ActionCase):
    def test_fill_never_returns_the_value(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/contact")
        out = self.do(brain, "fill", "email", "a@b.co")
        self.assertTrue(out["verified"])
        self.assertEqual(out["chars"], len("a@b.co"))
        self.assertNotIn("a@b.co", str(out))

    def test_fill_on_a_missing_field_names_it(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/contact")
        with self.assertRaises(ElementNotFound) as caught:
            self.do(brain, "fill", "passport number", "x")
        self.assertIn("passport number", caught.exception.message)

    def test_select_by_option_text(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/product/lamp")
        out = self.do(brain, "select", "Quantity", "3")
        self.assertEqual(out["selected"], "3")
        self.assertEqual(brain.ctx.probe.value("qty"), "3")

    def test_hover(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/")
        out = self.do(brain, "hover", "Contact")
        self.assertEqual(out["hovered"], "Contact")


class ReadingTest(ActionCase):
    def test_read_the_price(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/product/lamp")
        self.assertEqual(self.do(brain, "read", "price"), "$48")

    def test_read_the_title(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/")
        self.assertEqual(self.do(brain, "read", "title"), "Sundial Shop")

    def test_extract_links(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/")
        out = self.do(brain, "extract", "links")
        self.assertTrue(out)

    def test_assert_checks_text_on_the_page(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/product/lamp")
        self.assertTrue(self.do(brain, "assert", "#buy", "cart").get("ok"))
        self.assertFalse(self.do(brain, "assert", "#buy", "nothing like this").get("ok"))

    def test_scroll(self):
        brain = self.brain()
        self.do(brain, "goto", "http://shop.local/")
        out = self.do(brain, "scroll", "down", "300")
        self.assertEqual(out["dy"], 300)


class PageEventVerbsTest(ActionCase):
    def test_dialog_verb_accepts_the_open_dialog(self):
        brain = self.brain()
        pages = brain.engine("pages")
        self.do(brain, "goto", "http://dialog.local/")
        self.do(brain, "click", "Rename account")
        self.assertEqual(len(pages.dialogs), 1)          # it happened, and was answered
        out = self.do(brain, "dialog", "accept")
        self.assertFalse(out["answered"])                # nothing left to answer
        self.assertEqual(brain.ctx.cdp.evaluate("document.readyState"), "complete")

    def test_switch_tab_and_close_tab(self):
        brain = self.brain()
        self.do(brain, "goto", "http://popup.local/")
        self.do(brain, "click", "Continue with Example")
        self.assertIn("consent", brain.ctx.cdp.current_url())
        out = self.do(brain, "switch_tab", "main")
        self.assertTrue(out["switched"])
        self.do(brain, "close_tab")
        self.assertEqual(len(brain.engine("pages").open_tabs()), 1)

    def test_record_is_a_marker_not_a_page_action(self):
        brain = self.brain()
        self.assertTrue(self.do(brain, "record", "note")["recorded"])


class ApprovalVerbTest(ActionCase):
    def test_purchase_asks_for_a_person_without_approval(self):
        brain = self.brain()
        with self.assertRaises(Exception) as caught:
            self.do(brain, "purchase", "Linen Lamp")
        self.assertIn("irreversible", str(caught.exception))

    def test_unknown_verb_fails_with_its_name(self):
        brain = self.brain()
        with self.assertRaises(Exception) as caught:
            self.do(brain, "teleport", "somewhere")
        self.assertIn("teleport", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
