"""Kernel: events, registry, config, token, journal, utilities."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import MockCase  # noqa: F401  (sets sys.path)

from shadow.core.config import ShadowConfig
from shadow.core.events import EventBus
from shadow.core.journal import Journal
from shadow.core.registry import EngineState, Registry
from shadow.core.token import mint_from_secret, read_secret
from shadow.errors import ConfigError
from shadow.util import Backoff, hs256_jwt, redact, redact_obj, verify_hs256


class TinyEngine:
    def __init__(self, healthy=True):
        self.up = False
        self.healthy = healthy
        self.starts = 0
        self.stops = 0

    def start(self):
        self.up = True
        self.starts += 1

    def stop(self):
        self.up = False
        self.stops += 1

    def health(self):
        return {"healthy": self.healthy and self.up}


class EventBusTest(unittest.TestCase):
    def test_wildcard_and_prefix(self):
        bus = EventBus()
        seen = []
        bus.on("*", lambda e: seen.append(("all", e.topic)))
        bus.on("task.*", lambda e: seen.append(("task", e.topic)))
        bus.publish("task.started", source="t", goal="x")
        bus.publish("engine.ready", source="t")
        self.assertEqual([s[1] for s in seen].count("task.started"), 2)
        self.assertIn(("all", "engine.ready"), seen)

    def test_a_failing_subscriber_does_not_break_the_publisher(self):
        bus = EventBus()
        bus.on("task.*", lambda e: 1 / 0)
        got = []
        bus.on("task.*", lambda e: got.append(e.topic))
        bus.publish("task.done")
        self.assertEqual(got, ["task.done"])

    def test_payload_keys_named_topic_and_source_do_not_collide(self):
        bus = EventBus()
        bus.publish("ui.notice", source="ui", topic="inner", text="hello")
        bus.publish("ui.notice", source="ui", emitter="y")
        self.assertEqual(bus.recent(2)[0]["data"]["topic"], "inner")
        self.assertEqual(bus.recent(1)[0]["source"], "ui")

    def test_recent_is_bounded_and_redacted(self):
        bus = EventBus(keep=10)
        for i in range(50):
            bus.publish("x", source="t", password="hunter2", i=i)
        recent = bus.recent(100)
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[-1]["data"]["password"], "<redacted>")


class RegistryTest(unittest.TestCase):
    def test_topological_start_and_reverse_stop(self):
        r = Registry()
        order = []
        a, b, c = TinyEngine(), TinyEngine(), TinyEngine()
        r.register("a", a)
        r.register("b", b, depends_on=["a"])
        r.register("c", c, depends_on=["b"])
        r.start_all()
        self.assertEqual([x.state for x in r.records()], [EngineState.READY] * 3)
        r.stop_all()
        self.assertEqual(a.stops + b.stops + c.stops, 3)

    def test_cycle_is_refused(self):
        r = Registry()
        r.register("a", TinyEngine(), depends_on=["b"])
        r.register("b", TinyEngine(), depends_on=["a"])
        with self.assertRaises(ConfigError):
            r.order()

    def test_unknown_dependency_is_refused(self):
        r = Registry()
        r.register("a", TinyEngine(), depends_on=["ghost"])
        with self.assertRaises(ConfigError):
            r.order()

    def test_restart_counts_and_recovers(self):
        r = Registry()
        eng = TinyEngine()
        r.register("a", eng)
        r.start_all()
        r.restart("a")
        self.assertEqual(eng.starts, 2)
        self.assertEqual(r.record("a").restarts, 1)

    def test_health_probe_marks_down_engine_unhealthy(self):
        r = Registry()
        r.register("a", TinyEngine(healthy=False))
        r.start_all()
        self.assertFalse(r.health()["a"]["healthy"])


class ConfigTest(unittest.TestCase):
    def test_env_overrides_file_defaults(self):
        os.environ["SHADOW_MODEL"] = "qwen2.5"
        os.environ["SHADOW_ALLOW_OS_INPUT"] = "true"
        try:
            cfg = ShadowConfig.load()
            self.assertEqual(cfg.intelligence.model, "qwen2.5")
            self.assertTrue(cfg.safety.allow_os_input)
        finally:
            del os.environ["SHADOW_MODEL"]
            del os.environ["SHADOW_ALLOW_OS_INPUT"]

    def test_unknown_setting_is_an_error(self):
        with self.assertRaises(ConfigError):
            ShadowConfig.load(overrides={"nope": 1})

    def test_nested_partial_update_keeps_siblings(self):
        cfg = ShadowConfig.load(overrides={"supervisor": {"interval_s": 0.5}})
        self.assertEqual(cfg.supervisor.interval_s, 0.5)
        self.assertEqual(cfg.supervisor.max_restarts, 3)

    def test_token_is_not_printed(self):
        cfg = ShadowConfig.load(overrides={"api_token": "secret-token"})
        self.assertNotIn("secret-token", cfg.describe())
        self.assertIn("<set>", cfg.describe())

    def test_safety_defaults_are_closed(self):
        cfg = ShadowConfig()
        self.assertFalse(cfg.safety.allow_os_input)
        self.assertFalse(cfg.safety.allow_system_commands)
        self.assertIn("127.0.0.1", cfg.safety.blocked_hosts)


class TokenTest(unittest.TestCase):
    def test_minted_token_verifies(self):
        token = mint_from_secret("s3cr3t")
        self.assertTrue(verify_hs256("s3cr3t", token))
        self.assertFalse(verify_hs256("other", token))

    def test_settings_file_in_utf16_le_with_bom(self):
        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_bytes(json.dumps({"api_secret": "from-file"}).encode("utf-16"))
        self.assertEqual(read_secret(str(path)), "from-file")

    def test_settings_file_in_utf8_with_bom(self):
        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"api_secret": "utf8-bom"}).encode())
        self.assertEqual(read_secret(str(path)), "utf8-bom")

    def test_missing_secret_is_a_clear_error(self):
        path = Path(tempfile.mkdtemp()) / "settings.json"
        path.write_text("{}")
        with self.assertRaises(ConfigError) as caught:
            read_secret(str(path))
        self.assertIn("api_secret", str(caught.exception))


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.journal = Journal(self.dir / "journal.jsonl", sync=False)

    def test_last_step_and_resume(self):
        self.journal.append("task.started", "t1", -1, goal="go")
        for i in range(4):
            self.journal.append("step.done", "t1", i, action="click")
        self.assertEqual(self.journal.last_step("t1"), 3)
        self.journal.append("task.ended", "t1", 3, status="done")
        kinds = {e.kind for e in self.journal.entries(task_id="t1")}
        self.assertIn("task.ended", kinds)

    def test_torn_last_line_is_ignored(self):
        self.journal.append("task.started", "t1", -1)
        with open(self.dir / "journal.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"kind": "step.do')
        self.assertEqual(len(self.journal.entries(task_id="t1")), 1)

    def test_secrets_are_redacted_on_write(self):
        self.journal.append("step.done", "t1", 0, password="hunter2", note="token=abc")
        raw = (self.dir / "journal.jsonl").read_text()
        self.assertNotIn("hunter2", raw)
        self.assertNotIn("token=abc", raw)

    def test_compact_keeps_the_tail(self):
        for i in range(30):
            self.journal.append("step.done", "t1", i)
        dropped = self.journal.compact(keep_last=10)
        self.assertEqual(dropped, 20)
        self.assertEqual(len(self.journal.entries(task_id="t1")), 10)

    def test_ignores_the_run_marker_task(self):
        self.journal.append("runtime.boot", "-", -1)
        self.journal.append("task.started", "t9", -1)
        self.assertEqual(self.journal.task_ids(), ["t9"])


class UtilTest(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        bo = Backoff(base=0.5, cap=4.0, jitter=False)
        self.assertEqual([bo.delay(n) for n in (1, 2, 4, 99)], [0.5, 1.0, 4.0, 4.0])

    def test_redaction(self):
        self.assertIn("<redacted>", redact("password = hunter2"))
        self.assertIn("<redacted-number>", redact("card 4111111111111111"))
        clean = redact_obj({"api_token": "x", "nested": {"password": "y", "keep": "z"}})
        self.assertEqual(clean["api_token"], "<redacted>")
        self.assertEqual(clean["nested"]["password"], "<redacted>")
        self.assertEqual(clean["nested"]["keep"], "z")

    def test_jwt_header_is_hs256(self):
        token = hs256_jwt("s", {"sub": "x"})
        import base64
        header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
        self.assertEqual(header["alg"], "HS256")


if __name__ == "__main__":
    unittest.main()
