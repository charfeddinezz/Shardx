"""Computer Command Engine — the small, allowlisted bridge to the OS.

Off by default (`safety.allow_system_commands`). When switched on it will only
run programs named in `safety.allowed_commands`, with an argument vector and
`shell=False`, so a goal cannot smuggle `; rm -rf` through a string. It exists
for the legitimate cases: opening a local file, checking a service is up,
reading a version.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from ..errors import SafetyViolation
from .base import Engine

DEFAULT_TIMEOUT = 20.0


class ComputerEngine(Engine):
    name = "computer"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.runs = 0

    def _guard(self, program: str) -> None:
        cfg = self.ctx.cfg.safety
        if not cfg.allow_system_commands:
            raise SafetyViolation(
                "system commands are off. This is deliberate; set SHADOW_ALLOW_SYSTEM_COMMANDS=1 "
                "and list the programs you trust in safety.allowed_commands.")
        allowed = {os.path.basename(a) for a in cfg.allowed_commands}
        if os.path.basename(program) not in allowed:
            raise SafetyViolation(f"{program!r} is not in safety.allowed_commands ({sorted(allowed)})")

    def run(self, program: str, args: Optional[List[str]] = None, timeout: float = DEFAULT_TIMEOUT,
            cwd: Optional[str] = None) -> Dict[str, Any]:
        args = [str(a) for a in (args or [])]
        self._guard(program)
        exe = shutil.which(program)
        if not exe:
            raise SafetyViolation(f"{program!r} is not installed or not on PATH")
        if any("\x00" in a for a in args):
            raise SafetyViolation("argument contains a NUL byte")
        try:
            proc = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout,
                                  cwd=cwd, shell=False, check=False)
        except subprocess.TimeoutExpired as exc:
            self.fail(exc)
            return {"ok": False, "error": f"timed out after {timeout}s", "program": program}
        self.runs += 1
        self.note(program=program, args=args, code=proc.returncode)
        self.emit("ran", program=program, code=proc.returncode)
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": (proc.stdout or "")[:8000], "stderr": (proc.stderr or "")[:4000],
                "program": program, "args": args}

    def run_line(self, line: str, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """Split a command line without a shell, then run it as a vector."""
        parts = shlex.split(line, posix=(os.name != "nt"))
        if not parts:
            raise SafetyViolation("empty command")
        return self.run(parts[0], parts[1:], timeout=timeout)

    def info(self) -> Dict[str, Any]:
        return {"platform": sys.platform, "python": sys.version.split()[0],
                "cwd": os.getcwd(), "enabled": self.ctx.cfg.safety.allow_system_commands,
                "allowed": sorted(self.ctx.cfg.safety.allowed_commands)}

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info["runs"] = self.runs
        return info
