"""SHADOW AI — SHARDX AUTOPILOT ULTIMATE.

A local, offline-first operator for ShardBrowser (ShardX). It speaks the
launcher's local automation API, drives a live profile over the Chrome
DevTools Protocol, and orchestrates a set of independent engines that can be
supervised, restarted and resumed.

Nothing here needs a cloud model or a network account. The "intelligence" is
a deterministic planner plus an optional bridge to a *local* LLM server
(Ollama / llama.cpp) that you run yourself.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
