"""Per-site lessons: a selector that worked is reused, one that went stale dies."""

from __future__ import annotations

import unittest

from tests.helpers import MockCase

from shadow.runtime.orchestrator import ShadowAI


class LessonCase(MockCase):
    def brain(self, **overrides) -> ShadowAI:
        brain = ShadowAI(self.config(**overrides), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain

    def open(self, brain: ShadowAI, url: str = "http://shop.local/"):
        brain.ctx.ensure_cdp("win-rtx4060").navigate(url)
        return brain.ctx.page()


class LessonTest(LessonCase):
    def test_a_remembered_selector_wins_over_scoring(self):
        brain = self.brain()
        probe = self.open(brain)
        discovery, memory = brain.engine("discovery"), brain.engine("memory")
        memory.learn("http://shop.local/", "click:Contact", "a")
        picked = discovery.pick(probe, "Contact")
        self.assertEqual(picked.score, 100.0)
        self.assertIn("remembered", picked.why[0])

    def test_a_stale_lesson_is_dropped_and_scoring_takes_over(self):
        brain = self.brain()
        probe = self.open(brain)
        discovery, memory = brain.engine("discovery"), brain.engine("memory")
        memory.learn("http://shop.local/", "click:Contact", "#removed-in-a-redesign")
        picked = discovery.pick(probe, "Contact")
        self.assertNotIn("remembered", picked.why[0])     # scoring, not the lesson
        self.assertEqual(discovery.forgot_lesson, "#removed-in-a-redesign")
        self.assertTrue(discovery.health()["lesson_stale"])

    def test_lessons_are_learned_by_a_real_click_and_reused_next_time(self):
        brain = self.brain()
        brain.run("Open http://shop.local/ and click Contact")
        lessons = brain.engine("memory").store.data.get("lessons", {}).get("shop.local", {})
        self.assertIn("click:Contact", lessons)
        self.assertTrue(lessons["click:Contact"]["detail"])
        # a second goal on the same site now starts from what the first learned
        probe = self.open(brain)
        picked = brain.engine("discovery").pick(probe, "Contact")
        self.assertIn("remembered", picked.why[0])

    def test_lessons_are_scoped_to_the_site_but_findable_without_a_url(self):
        brain = self.brain()
        probe = self.open(brain)
        brain.engine("memory").learn("http://elsewhere.local/", "click:Contact", "#other-site")
        picked = brain.engine("discovery").pick(probe, "Contact")
        self.assertNotIn("remembered", picked.why[0])     # a different site's lesson


if __name__ == "__main__":
    unittest.main()
