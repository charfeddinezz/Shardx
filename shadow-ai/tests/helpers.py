"""Shared fixtures: a mock launcher with an ephemeral port and a temp state dir."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shadow.core.config import ShadowConfig          # noqa: E402
from shadow.core.context import ShadowContext        # noqa: E402
from shadow.mock.launcher import MockLauncher        # noqa: E402
from shadow.transport.cdp import CdpClient           # noqa: E402


class MockCase(unittest.TestCase):
    """A test with a fresh mock launcher and a fresh state directory."""

    motion = True

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shadow-test-"))
        self.mock = MockLauncher(token="test-token", human_speed=0.0, motion=self.motion).start()
        self.addCleanup(self.mock.stop)
        self.cdp = None

    def tearDown(self) -> None:
        if self.cdp is not None:
            try:
                self.cdp.close()
            except Exception:
                pass

    # -- helpers ------------------------------------------------------
    def config(self, **overrides) -> ShadowConfig:
        base = {"mock": True, "state_dir": str(self.tmp), "default_profile": "win-rtx4060"}
        base.update(overrides)
        return ShadowConfig.load(overrides=base)

    def context(self, **overrides) -> ShadowContext:
        ctx = ShadowContext(self.config(**overrides), mock_launcher=self.mock)
        self.addCleanup(ctx.close)
        return ctx

    def attach(self, ctx: ShadowContext, url: str = "") -> CdpClient:
        cdp = ctx.ensure_cdp("win-rtx4060")
        if url:
            cdp.navigate(url)
        self.cdp = cdp
        return cdp
