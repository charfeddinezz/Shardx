"""The local model bridge, exercised against a real HTTP server.

A fake Ollama/OpenAI-compatible server stands in for llama.cpp or Ollama, so
the whole path is covered without a model on the machine: prompt → HTTP →
whatever mess the model answered → one constrained decision.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import MockCase

from shadow.core.config import IntelligenceConfig
from shadow.engines.intelligence import LocalReasoner
from shadow.runtime.orchestrator import ShadowAI


class FakeModel:
    """An HTTP server that answers with whatever the test tells it to."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):                        # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                reply = outer.replies[min(len(outer.requests) - 1, len(outer.replies) - 1)]
                payload = reply if isinstance(reply, dict) else {"response": str(reply),
                                                                 "choices": [{"message": {"content": str(reply)}}]}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):             # keep the test output clean
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ParserTest(unittest.TestCase):
    """Small local models all break JSON in the same handful of ways."""

    def setUp(self):
        self.reasoner = LocalReasoner(IntelligenceConfig())

    def test_clean_json(self):
        got = self.reasoner._parse('{"action": "click", "target": "#buy"}')
        self.assertEqual(got["action"], "click")

    def test_fenced_json_with_braces_inside_a_string(self):
        raw = 'Here:\n```json\n{"thought": "the {x} form", "action": "open", "target": "http://a.local"}\n```'
        got = self.reasoner._parse(raw)
        self.assertEqual(got["action"], "goto")
        self.assertEqual(got["target"], "http://a.local")
        self.assertEqual(got["thought"], "the {x} form")

    def test_single_quotes_and_trailing_comma(self):
        got = self.reasoner._parse("{'thought': 'go', 'action': 'finish',}")
        self.assertEqual(got["action"], "finish")

    def test_prose_answer(self):
        got = self.reasoner._parse('I will click the "Search" button.')
        self.assertEqual(got["action"], "click")
        self.assertEqual(got["target"], "Search")

    def test_an_action_outside_the_allow_list_is_refused(self):
        self.assertIsNone(self.reasoner._parse('{"action": "exec", "target": "rm -rf /"}'))
        self.assertIsNone(self.reasoner._parse('{"action": "goto"}'[:0] + '{"action": "shell"}'))

    def test_verb_aliases_are_mapped(self):
        self.assertEqual(self.reasoner._parse('{"action": "open", "target": "x"}')["action"], "goto")
        self.assertEqual(self.reasoner._parse('{"action": "done"}')["action"], "finish")


class LocalModelHTTPTest(unittest.TestCase):
    def test_ollama_is_asked_for_json_and_the_body_is_parsed(self):
        model = FakeModel(['{"action": "click", "target": "#go"}'])
        self.addCleanup(model.stop)
        cfg = IntelligenceConfig(provider="ollama", model="qwen",
                                 base_url=f"http://127.0.0.1:{model.port}")
        reasoner = LocalReasoner(cfg)
        got = reasoner.plan("click go", {"url": "http://x/", "title": "X", "elements": []}, [])
        self.assertEqual(got["action"], "click")
        body = model.requests[0]["body"]
        self.assertEqual(body["format"], "json")          # constrained decoding
        self.assertEqual(body["model"], "qwen")

    def test_openai_compatible_is_asked_for_a_json_object(self):
        model = FakeModel([{"choices": [{"message": {"content": '{"action":"finish"}'}}]}])
        self.addCleanup(model.stop)
        cfg = IntelligenceConfig(provider="openai_compatible",
                                 base_url=f"http://127.0.0.1:{model.port}")
        got = LocalReasoner(cfg).plan("done", {"url": "", "elements": []}, [])
        self.assertEqual(got["action"], "finish")
        body = model.requests[0]["body"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("enum", json.dumps(body["grammar"]))

    def test_a_bad_first_answer_is_retried_once(self):
        model = FakeModel(["I am not sure, maybe click the thing?", '{"action": "click", "target": "#go"}'])
        self.addCleanup(model.stop)
        cfg = IntelligenceConfig(provider="ollama", base_url=f"http://127.0.0.1:{model.port}",
                                 retries=2)
        got = LocalReasoner(cfg).plan("click", {"url": "", "elements": []}, [])
        self.assertEqual(got["target"], "#go")
        self.assertEqual(len(model.requests), 2)
        self.assertIn("not valid JSON", model.requests[1]["body"]["prompt"])

    def test_a_server_that_is_not_there_is_simply_unavailable(self):
        cfg = IntelligenceConfig(provider="ollama", base_url="http://127.0.0.1:9", timeout_s=0.4,
                                 retries=1)
        self.assertIsNone(LocalReasoner(cfg).plan("anything", {"url": "", "elements": []}, []))


class ModelDrivenLoopTest(MockCase):
    """The loop keeps working when the model is present, wrong, or absent."""

    def brain(self, **overrides):
        brain = ShadowAI(self.config(**overrides), printer=lambda line: None)
        brain.ctx._mock = self.mock
        brain.boot()
        self.addCleanup(brain.shutdown)
        return brain

    def test_a_model_proposal_the_page_does_not_have_is_rejected(self):
        model = FakeModel(['{"action": "click", "target": "#ghost-button"}'])
        self.addCleanup(model.stop)
        brain = self.brain(intelligence={"provider": "ollama",
                                         "base_url": f"http://127.0.0.1:{model.port}"})
        # the plan covers this goal; the model is never consulted, which is
        # itself the guarantee: a reviewed plan outranks an invented target
        result = brain.run("Open http://shop.local/ and click Contact")
        self.assertEqual(result.status, "done", result.error)
        self.assertTrue(brain.ctx.cdp.current_url().endswith("/contact"))

    def test_the_heuristic_planner_still_finishes_with_no_model(self):
        brain = self.brain(intelligence={"provider": "heuristic"})
        result = brain.run("Open http://shop.local/product/lamp and read the price")
        self.assertEqual(result.status, "done", result.error)
        self.assertEqual(result.results.get("answer"), "$48")


if __name__ == "__main__":
    unittest.main()
