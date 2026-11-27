"""A local stand-in for ShardX, so SHADOW can be run and tested on any machine.

`MockLauncher` serves the subset of the launcher's HTTP API that SHADOW uses
and exposes a CDP websocket that speaks the real protocol — including the
patched `Motion` domain and enough `Runtime.evaluate` to run the DOM scripts
the engines write. It is a test harness, not a browser: the HTML engine
understands the small HTML subset in `pages.py`.
"""

from .launcher import MockLauncher, MockProfile  # noqa: F401  re-exported
