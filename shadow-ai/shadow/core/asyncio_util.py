"""Run a coroutine from synchronous code, safely, inside or outside a loop."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine


def run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Execute `coro` to completion and return its result.

    If the caller already has a running loop in this thread (a notebook, an
    async test), running `asyncio.run` would fail, so the coroutine is pushed
    to a private loop on a helper thread instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=runner, name="shadow-run-sync", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
