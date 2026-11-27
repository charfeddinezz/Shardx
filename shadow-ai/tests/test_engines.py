"""Engines, one behaviour each that matters: understanding, discovery,
perception, human input, memory, data, safety, recovery, vision."""

from __future__ import annotations

import unittest

from tests.helpers import MockCase

from shadow.engines.data import DataEngine
from shadow.engines.discovery import DiscoveryEngine
from shadow.engines.intelligence import GoalParser
from shadow.engines.input import KeyboardEngine, MouseEngine
from shadow.engines.memory import MemoryEngine
from shadow.engines.page_understanding import PageUnderstandingEngine
from shadow.engines.recovery import RecoveryEngine
from shadow.engines.safety import SafetyEngine
from shadow.engines.vision import VisionEngine
from shadow.errors import (AmbiguousTarget, ApprovalRequired, ElementNotFound, SafetyViolation)
from shadow.transport.domprobe import PageProbe


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = GoalParser()

    def kinds(self, goal):
        return [(i.kind, i.target) for i in self.parser.parse(goal).intents]

    def test_english_goal_in_sentence_order(self):
        goal = "Go to shop.local, fill the search field with lamp, press Search"
        self.assertEqual([k for k, _ in self.kinds(goal)], ["open", "fill", "click"])

    def test_arabic_goal(self):
        interp = self.parser.parse("افتح https://shop.local/ وابحث عن lamp ثم اضغط على Linen Lamp")
        self.assertEqual(interp.language, "ar")
        self.assertEqual([i.kind for i in interp.intents], ["open", "find", "click"])
        self.assertEqual(interp.intents[1].target, "lamp")
        self.assertEqual(interp.intents[2].target, "Linen Lamp")

    def test_french_credentials(self):
        interp = self.parser.parse("Ouvre http://login.local/ user: admin pass: hunter2 puis clique Se connecter")
        login = [i for i in interp.intents if i.kind == "login"][0]
        self.assertEqual((login.target, login.value), ("admin", "hunter2"))

    def test_fill_value_stops_at_the_next_instruction(self):
        interp = self.parser.parse("fill email = a@b.co then click Sign in")
        fill = [i for i in interp.intents if i.kind == "fill"][0]
        self.assertEqual(fill.value, "a@b.co")

    def test_purchase_is_recognised(self):
        self.assertIn("purchase", [k for k, _ in self.kinds("buy the linen lamp and checkout")])

    def test_read_selector_and_question(self):
        interp = self.parser.parse("read the text of #title")
        read = [i for i in interp.intents if i.kind == "read"][0]
        self.assertEqual(read.target, "#title")

    def test_question_is_extracted(self):
        interp = self.parser.parse("what is the price of the oak desk?")
        self.assertEqual(interp.question, "the price of the oak desk")


class DiscoveryTest(MockCase):
    def setUp(self):
        super().setUp()
        self.ctx = self.context()
        self.probe = PageProbe(self.attach(self.ctx, "http://shop.local/"))
        self.engine = DiscoveryEngine(self.ctx)
        self.engine.start()

    def test_picks_the_exact_label(self):
        self.assertEqual(self.engine.pick(self.probe, "Linen Lamp").label, "Linen Lamp")

    def test_role_word_narrows_the_match(self):
        self.assertEqual(self.engine.pick(self.probe, "Search button").label, "Search")

    def test_find_field_by_placeholder(self):
        node = self.engine.find_field(self.probe, "search").node
        self.assertEqual(node["selector"], "#q")

    def test_missing_target_raises_with_suggestions(self):
        with self.assertRaises(ElementNotFound) as caught:
            self.engine.pick(self.probe, "orbital launch button")
        self.assertIn("suggestions", caught.exception.details)

    def test_equally_good_matches_are_refused(self):
        # Both links say the same thing, so guessing would be a coin flip.
        self.probe.ask("setValue", ["dup1", "x"])   # any call refills the mock's snapshot
        self.ctx.cdp.evaluate("""
            document.body.insertAdjacentHTML('beforeend',
              '<a href="/x" id="dup1">Cancel order</a><a href="/y" id="dup2">Cancel order</a>')
        """)
        with self.assertRaises(AmbiguousTarget):
            self.engine.pick(self.probe, "Cancel order link")

    def test_disabled_elements_score_low(self):
        self.probe.ask("setValue", ["off", "x"])
        self.ctx.cdp.evaluate("""
            document.body.insertAdjacentHTML('beforeend',
              '<button id="off" disabled>Checkout</button>')
        """)
        with self.assertRaises(ElementNotFound):
            self.engine.pick(self.probe, "Checkout button")


class PageUnderstandingTest(MockCase):
    def setUp(self):
        super().setUp()
        self.ctx = self.context()
        self.engine = PageUnderstandingEngine(self.ctx)
        self.engine.start()

    def model(self, url):
        probe = PageProbe(self.attach(self.ctx, url))
        return self.engine.understand(probe, self.ctx.cdp)

    def test_kinds(self):
        self.assertEqual(self.model("http://login.local/").kind, "login")
        self.assertEqual(self.model("http://shop.local/").kind, "search")

    def test_forms_and_fields(self):
        model = self.model("http://login.local/")
        self.assertIn("pass", [f["name"] for f in model.forms[0]["fields"]])
        self.assertTrue(any(f.get("type") == "password" for f in model.fields))

    def test_question_is_answered_from_the_page(self):
        model = self.model("http://shop.local/")
        answer = self.engine.answer(model, "where is the oak desk?")
        self.assertIn("Oak Desk", answer["answer"])
        self.assertTrue(answer["sources"])

    def test_unknown_question_falls_back_to_the_page_opening(self):
        model = self.model("http://shop.local/")
        answer = self.engine.answer(model, "zebra quantum")
        self.assertTrue(answer["answer"])


class InputTest(MockCase):
    def setUp(self):
        super().setUp()
        self.ctx = self.context()
        self.ctx.registry.register("discovery", DiscoveryEngine(self.ctx))
        self.ctx.registry.get("discovery").start()
        self.keyboard = KeyboardEngine(self.ctx)
        self.mouse = MouseEngine(self.ctx)
        self.ctx.registry.register("keyboard", self.keyboard)
        self.ctx.registry.register("mouse", self.mouse)
        self.keyboard.start()
        self.mouse.start()
        self.probe = PageProbe(self.attach(self.ctx, "http://shop.local/"))

    def test_typing_lands_in_the_field_and_verifies(self):
        field = self.ctx.registry.get("discovery").find_field(self.probe, "search")
        result = self.keyboard.fill_field(self.probe, field.node, "lamp")
        self.assertTrue(result["verified"])
        self.assertEqual(self.probe.value("q"), "lamp")

    def test_click_walks_the_pointer_before_pressing(self):
        result = self.mouse.click_label(self.probe, "Search button")
        self.assertEqual(result["clicks"], 1)
        self.assertGreaterEqual(result["move_ms"], 0)

    def test_submitting_the_search_reaches_the_results_page(self):
        self.keyboard.fill_field(self.probe, self.ctx.registry.get("discovery")
                                 .find_field(self.probe, "search").node, "desk")
        self.mouse.click_label(self.probe, "Search button")
        self.assertIn("/search?q=desk", self.ctx.cdp.current_url())

    def test_tab_moves_focus(self):
        self.keyboard.focus(self.probe, element_id="q")
        self.assertEqual(self.probe.active()["id"], "q")
        self.keyboard.press("Tab")
        self.assertNotEqual(self.probe.active()["id"], "q")

    def test_scroll_changes_the_page_offset(self):
        self.mouse.scroll("down", 400)
        self.assertAlmostEqual(self.probe.ask("scrollY"), 400, places=1)


class MemoryTest(MockCase):
    def test_facts_checkpoints_and_lessons_persist_to_disk(self):
        ctx = self.context()
        memory = MemoryEngine(ctx)
        memory.start()
        memory.remember("last_search", "lamp", "site:shop.local")
        memory.checkpoint("t-1", 4, {"url": "http://shop.local/"})
        memory.learn("http://shop.local/", "search", "#q")

        again = MemoryEngine(self.context())  # a new engine, same state directory
        self.assertEqual(again.recall("last_search", "site:shop.local"), "lamp")
        self.assertEqual(again.resume_point("t-1")["step"], 4)
        self.assertIn("search", again.lessons_for("http://shop.local/"))


class DataTest(MockCase):
    def test_person_is_stable_per_seed_and_distinct_across_seeds(self):
        data = DataEngine(self.context())
        data.start()
        self.assertEqual(data.person("a").email, data.person("a").email)
        self.assertNotEqual(data.person("a").email, data.person("b").email)

    def test_rows_survive_a_round_trip(self):
        data = DataEngine(self.context())
        data.start()
        data.store("job", [{"a": 1}, {"a": 2}])
        self.assertEqual(len(data.read("job")), 2)

    def test_table_extraction(self):
        ctx = self.context()
        probe = PageProbe(self.attach(ctx, "http://login.local/app"))
        data = DataEngine(ctx)
        data.start()
        table = data.table(probe)
        self.assertIn("Total", table["header"][1])
        self.assertGreaterEqual(table["count"], 1)

    def test_fill_person_fills_what_the_page_has(self):
        ctx = self.context()
        ctx.registry.register("discovery", DiscoveryEngine(ctx))
        keyboard = KeyboardEngine(ctx)
        ctx.registry.register("keyboard", keyboard)
        ctx.registry.register("mouse", MouseEngine(ctx))
        for name in ("discovery", "keyboard", "mouse"):
            ctx.registry.get(name).start()
        probe = PageProbe(self.attach(ctx, "http://login.local/"))
        data = DataEngine(ctx)
        data.start()
        filled = data.fill_person(probe, data.person("t"))
        self.assertIn("user", " ".join(filled))


class SafetyTest(MockCase):
    def setUp(self):
        super().setUp()
        self.engine = SafetyEngine(self.context())
        self.engine.start()

    def test_local_and_metadata_hosts_are_blocked(self):
        for url in ("http://127.0.0.1/", "http://localhost:8080/", "http://169.254.169.254/latest/meta-data"):
            self.assertFalse(self.engine.check_url(url).allowed, url)

    def test_other_schemes_are_blocked(self):
        self.assertFalse(self.engine.check_url("file:///etc/passwd").allowed)
        self.assertFalse(self.engine.check_url("javascript:alert(1)").allowed)

    def test_payment_clicks_need_approval(self):
        with self.assertRaises(ApprovalRequired):
            self.engine.check_action("click", "Pay now")
        self.assertTrue(self.engine.check_action("click", "Pay now", approved=True).allowed)

    def test_ordinary_clicks_pass(self):
        self.assertTrue(self.engine.check_action("click", "Sign in").allowed)

    def test_os_input_is_off_by_default(self):
        self.assertFalse(self.engine.check_os_input().allowed)

    def test_budget_stops_a_runaway_task(self):
        class FakeTask:
            steps_done = 999
            started_at = 0
        self.assertIn("step budget", self.engine.budget(FakeTask()))


class RecoveryTest(MockCase):
    def test_classifies_by_error_kind(self):
        from shadow.errors import TransportError
        engine = RecoveryEngine(self.context())
        engine.start()
        self.assertIn("rescan", engine.classify(ElementNotFound("x")))
        self.assertEqual(engine.classify(AmbiguousTarget("x")), ["rescan"])
        self.assertIn("reattach", engine.classify(TransportError("socket closed")))

    def test_reload_page_recovers_a_transport_failure(self):
        ctx = self.context()
        self.attach(ctx, "http://shop.local/")
        engine = RecoveryEngine(ctx)
        engine.start()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ElementNotFound("not yet")
            return "ok"

        result = engine.run(flaky, retries=2)
        self.assertEqual(result, "ok")
        self.assertGreaterEqual(engine.recoveries, 1)


class VisionTest(MockCase):
    def test_screenshot_is_saved_and_sized(self):
        ctx = self.context()
        self.attach(ctx, "http://shop.local/")
        vision = VisionEngine(ctx)
        vision.start()
        frame = vision.shot()
        self.assertEqual((frame.width, frame.height), (320, 200))
        self.assertTrue(frame.path.endswith(".png"))
        self.assertTrue(open(frame.path, "rb").read(8).startswith(b"\x89PNG"))

    def test_same_page_does_not_read_as_changed(self):
        ctx = self.context()
        self.attach(ctx, "http://shop.local/")
        vision = VisionEngine(ctx)
        vision.start()
        a = vision.shot(save=False)
        b = vision.shot(save=False)
        self.assertFalse(vision.diff(a, b)["changed"])

    def test_different_page_reads_as_changed(self):
        ctx = self.context()
        self.attach(ctx, "http://shop.local/")
        vision = VisionEngine(ctx)
        vision.start()
        a = vision.shot(save=False)
        ctx.cdp.navigate("http://login.local/")
        b = vision.shot(save=False)
        self.assertTrue(vision.diff(a, b)["changed"])

    def test_ocr_says_so_when_there_is_no_engine(self):
        vision = VisionEngine(self.context(vision={"ocr": "none"}))
        vision.start()
        from shadow.errors import VisionUnavailable
        with self.assertRaises(VisionUnavailable):
            vision.ocr()


class NoMotionTest(MockCase):
    motion = False

    def test_input_engine_reports_degraded_instead_of_pretending(self):
        ctx = self.context()
        ctx.registry.register("discovery", DiscoveryEngine(ctx))
        ctx.registry.get("discovery").start()
        keyboard = KeyboardEngine(ctx)
        ctx.registry.register("keyboard", keyboard)
        keyboard.start()
        probe = PageProbe(self.attach(ctx, "http://shop.local/"))
        field = ctx.registry.get("discovery").find_field(probe, "search")
        keyboard.fill_field(probe, field.node, "lamp")
        self.assertTrue(keyboard.degraded)
        self.assertEqual(probe.value("q"), "lamp")


if __name__ == "__main__":
    unittest.main()
