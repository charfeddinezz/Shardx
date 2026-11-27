"""Transport: the WebSocket client against the mock engine, CDP, the DOM reader,
and the launcher client (including its error handling)."""

from __future__ import annotations

import unittest

import time

from tests.helpers import MockCase

from shadow.errors import ApiError, AuthError, CdpError, ElementNotFound, TransportError
from shadow.transport.cdp import CdpClient
from shadow.transport.domprobe import PageProbe
from shadow.transport.http import HttpClient
from shadow.transport.shardx import ShardxApi
from shadow.transport.ws import WsClient


class WebSocketTest(MockCase):
    def test_handshake_and_a_command_round_trip(self):
        cdp = self.attach(self.context())
        self.assertTrue(cdp.call("Browser.getVersion", session_id="", timeout=10)["product"]
                        .startswith("ShardBrowser/"))

    def test_unknown_domain_raises_a_cdp_error(self):
        cdp = self.attach(self.context())
        with self.assertRaises(CdpError) as caught:
            cdp.call("Nope.nothing", {}, session_id="", timeout=10)
        self.assertIn("wasn't found", str(caught.exception))

    def test_motion_is_detected_and_used_without_a_session(self):
        cdp = self.attach(self.context())
        self.assertTrue(cdp.motion_available())
        cdp.pointer_create(10, 10)
        self.assertGreaterEqual(cdp.pointer_glide(200, 200, 120), 0)

    def test_ws_client_refuses_a_bad_scheme(self):
        with self.assertRaises(TransportError):
            WsClient("http://example.com").connect()


class CdpTest(MockCase):
    def test_navigate_title_and_url(self):
        cdp = self.attach(self.context(), "http://shop.local/")
        self.assertEqual(cdp.title(), "Sundial Shop")
        self.assertEqual(cdp.current_url(), "http://shop.local/")

    def test_attach_routes_page_commands_to_the_session_and_motion_to_root(self):
        cdp = self.attach(self.context())
        self.assertIsNotNone(cdp.session_id)
        # Page commands carry the session id; Motion does not.
        cdp.navigate("http://shop.local/about")
        self.assertEqual(cdp.title(), "About — Sundial Shop")

    def test_screenshot_is_a_png(self):
        cdp = self.attach(self.context(), "http://shop.local/")
        data = cdp.screenshot()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def test_settle_returns_without_hanging(self):
        """The mock has no MutationObserver, so settle answers False quickly —
        what matters is that it returns inside its budget instead of hanging."""
        cdp = self.attach(self.context(), "http://shop.local/")
        started = time.time()
        result = cdp.settle(quiet_ms=80, max_ms=600)
        self.assertIn(result, (True, False))
        self.assertLess(time.time() - started, 5.0)

    def test_close_marks_the_session_closed(self):
        cdp = self.attach(self.context())
        cdp.close()
        self.cdp = None
        with self.assertRaises(TransportError):
            cdp.call("Browser.getVersion", session_id="")


class ProbeTest(MockCase):
    def test_snapshot_interactive_and_text(self):
        ctx = self.context()
        probe = PageProbe(self.attach(ctx, "http://shop.local/"))
        names = [n["name"] for n in probe.interactive()]
        self.assertIn("Search", names)
        self.assertIn("Linen Lamp", names)
        self.assertEqual(probe.text("#title"), "Sundial Shop")
        self.assertEqual(probe.count("li.product"), 3)

    def test_probe_survives_navigation(self):
        ctx = self.context()
        probe = PageProbe(self.attach(ctx, "http://shop.local/"))
        self.assertEqual(probe.count("li.product"), 3)
        ctx.cdp.navigate("http://shop.local/about")
        self.assertEqual(probe.text("h1"), "About")

    def test_value_reflects_what_was_typed(self):
        ctx = self.context()
        probe = PageProbe(self.attach(ctx, "http://shop.local/"))
        probe.set_value("q", "lamp")
        self.assertEqual(probe.value("q"), "lamp")

    def test_links_and_forms(self):
        ctx = self.context()
        probe = PageProbe(self.attach(ctx, "http://login.local/"))
        fields = [f["name"] for f in probe.forms()[0]["fields"]]
        self.assertIn("user", fields)
        self.assertIn("pass", fields)


class LauncherApiTest(MockCase):
    def test_health_needs_no_token_but_profiles_do(self):
        api = ShardxApi(f"http://127.0.0.1:{self.mock.port}", token="")
        self.assertTrue(api.health()["ok"])
        with self.assertRaises(AuthError):
            api.profiles()

    def test_profiles_and_session_lifecycle(self):
        api = ShardxApi(f"http://127.0.0.1:{self.mock.port}", token="test-token")
        self.assertIn("win-rtx4060", [p["id"] for p in api.profiles()])
        started = api.start("win-rtx4060", headless=True)
        self.assertTrue(started["cdp"]["http_url"].startswith("http://127.0.0.1:"))
        running = api.running()
        self.assertEqual(running[0]["profile_id"], "win-rtx4060")
        stopped = api.stop("win-rtx4060")
        self.assertTrue(stopped["stopped"])

    def test_cdp_for_starts_the_profile_and_returns_its_endpoint(self):
        api = ShardxApi(f"http://127.0.0.1:{self.mock.port}", token="test-token")
        url = api.cdp_http_url("linux-box", headless=True)
        cdp = CdpClient.from_http(url).connect()
        try:
            self.assertTrue(cdp.motion_available())
        finally:
            cdp.close()

    def test_unknown_profile_is_an_api_error(self):
        api = ShardxApi(f"http://127.0.0.1:{self.mock.port}", token="test-token")
        with self.assertRaises(ApiError) as caught:
            api.start("nobody")
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(caught.exception.retryable)

    def test_unreachable_launcher_is_a_transport_error(self):
        api = ShardxApi("http://127.0.0.1:1", token="x")
        with self.assertRaises(TransportError):
            api.health()
        self.assertFalse(api.is_up())

    def test_missing_token_is_reported_before_the_call(self):
        api = ShardxApi("http://127.0.0.1:1", token="")
        with self.assertRaises(Exception):
            api.require_token()


class NoMotionBuildTest(MockCase):
    motion = False

    def test_missing_domain_is_detected_not_fatal(self):
        ctx = self.context()
        cdp = self.attach(ctx)
        self.assertFalse(cdp.motion_available())
        self.assertTrue(cdp.degraded)


if __name__ == "__main__":
    unittest.main()
