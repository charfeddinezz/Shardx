"""Vision Engine — pixels when the DOM is not enough.

Three capabilities, in the order they should be tried:

1. **screenshot** — a PNG of the viewport or the whole page, saved to disk.
2. **change detection** — compare two frames and report how much moved, so a
   step can wait for the page to settle instead of sleeping a fixed time.
3. **OCR** — text out of an image, through Tesseract when it is installed.

No cloud calls. When Tesseract is absent, `ocr()` says so plainly rather than
pretending; the DOM reader covers most "read the page" goals anyway.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..errors import VisionUnavailable
from ..util import iso, now
from .base import Engine


@dataclass
class Frame:
    data: bytes
    ts: float
    width: int = 0
    height: int = 0
    path: str = ""

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.data).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": iso(self.ts), "width": self.width, "height": self.height,
                "bytes": len(self.data), "sha": self.sha, "path": self.path}


class VisionEngine(Engine):
    name = "vision"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.frames: List[Frame] = []
        self.shots = 0

    # -- capture ------------------------------------------------------
    def shot(self, full_page: bool = False, save: bool = True, fmt: str = "png") -> Frame:
        cdp = self.ctx.ensure_cdp()
        data = cdp.screenshot(fmt=fmt, full_page=full_page)
        if len(data) > self.ctx.cfg.vision.max_image_bytes:
            raise VisionUnavailable(f"screenshot is {len(data)} bytes, over the configured limit")
        width, height = png_size(data) if fmt == "png" else (0, 0)
        frame = Frame(data=data, ts=now(), width=width, height=height)
        if save:
            path = self.ctx.cfg.data_path() / "shots" / f"{int(frame.ts)}-{frame.sha}.{fmt}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            frame.path = str(path)
        self.frames.append(frame)
        if len(self.frames) > 20:
            self.frames = self.frames[-20:]
        self.shots += 1
        self.note(bytes=len(data), full=full_page)
        self.emit("captured", sha=frame.sha, path=frame.path)
        return frame

    # -- change detection ---------------------------------------------
    def diff(self, a: Frame, b: Frame) -> Dict[str, Any]:
        """What share of the image changed between two frames (same size only)."""
        if a.width != b.width or a.height != b.height or not a.width:
            return {"changed": True, "share": 1.0, "reason": "different size"}
        pa, pb = decode_png(a.data), decode_png(b.data)
        if pa is None or pb is None:
            return {"changed": a.sha != b.sha, "share": 1.0 if a.sha != b.sha else 0.0,
                    "reason": "unsupported format, compared by hash"}
        changed = 0
        total = 0
        for row_a, row_b in zip(pa, pb):
            for x in range(0, min(len(row_a), len(row_b)), 3 * 4):
                total += 1
                if row_a[x:x + 3] != row_b[x:x + 3]:
                    changed += 1
        share = (changed / total) if total else 0.0
        return {"changed": share >= self.ctx.cfg.vision.diff_threshold, "share": round(share, 4),
                "pixels": total}

    def wait_for_change(self, timeout: float = 10.0, full_page: bool = False) -> Dict[str, Any]:
        before = self.shot(full_page=full_page, save=False)
        deadline = time.time() + timeout
        last = before
        while time.time() < deadline:
            time.sleep(0.35)
            current = self.shot(full_page=full_page, save=False)
            result = self.diff(last, current)
            if result["changed"]:
                return {"changed": True, "after": round(timeout - (deadline - time.time()), 2),
                        "share": result.get("share")}
            last = current
        return {"changed": False, "after": round(timeout, 2)}

    # -- OCR ----------------------------------------------------------
    def ocr_available(self) -> bool:
        mode = self.ctx.cfg.vision.ocr
        if mode == "none":
            return False
        return shutil.which("tesseract") is not None

    def ocr(self, frame: Optional[Frame] = None, lang: str = "") -> Dict[str, Any]:
        if not self.ocr_available():
            raise VisionUnavailable(
                "no OCR engine: install Tesseract (it reads the screenshot on this computer), "
                "or keep using the DOM reader")
        frame = frame or self.shot(save=True)
        if not frame.path:
            path = self.ctx.cfg.data_path() / "shots" / f"ocr-{frame.sha}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(frame.data)
            frame.path = str(path)
        language = lang or self.ctx.cfg.vision.tesseract_lang
        try:
            proc = subprocess.run(["tesseract", frame.path, "stdout", "-l", language],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.fail(exc)
            raise VisionUnavailable(f"tesseract failed: {exc}") from None
        text = (proc.stdout or "").strip()
        self.note(kind="ocr", chars=len(text))
        return {"text": text, "lang": language, "source": frame.to_dict()}

    def locate_text(self, needle: str, frame: Optional[Frame] = None) -> Dict[str, Any]:
        """Where a phrase appears, from Tesseract's word boxes (TSV output)."""
        if not self.ocr_available():
            raise VisionUnavailable("no OCR engine installed")
        frame = frame or self.shot(save=True)
        if not frame.path:
            raise VisionUnavailable("the frame was not saved to disk")
        try:
            proc = subprocess.run(["tesseract", frame.path, "stdout", "-l",
                                   self.ctx.cfg.vision.tesseract_lang, "tsv"],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VisionUnavailable(f"tesseract failed: {exc}") from None
        rows = [line.split("\t") for line in (proc.stdout or "").splitlines()[1:] if line.count("\t") >= 11]
        hits = []
        for row in rows:
            word = row[11].strip()
            if word and needle.lower() in word.lower():
                hits.append({"word": word, "x": int(row[6]), "y": int(row[7]),
                             "width": int(row[8]), "height": int(row[9])})
        return {"found": bool(hits), "hits": hits[:10], "needle": needle}

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"shots": self.shots, "frames_kept": len(self.frames),
                     "ocr": self.ocr_available()})
        return info


# --------------------------------------------------------------------------
# Minimal PNG reader: 8-bit RGB/RGBA, no interlace. Enough for CDP frames.
# --------------------------------------------------------------------------

def png_size(data: bytes) -> tuple:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    width, height = struct.unpack(">II", data[16:24])
    return (width, height)


def decode_png(data: bytes) -> Optional[List[bytes]]:
    """Return the decompressed scanline rows, or None if the format is exotic."""
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    idat = b""
    header = None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif tag == b"IDAT":
            idat += chunk
        elif tag == b"IEND":
            break
        pos += 12 + length
    if header is None:
        return None
    width, height, depth, color, _comp, _filter, interlace = header
    if depth != 8 or color not in (2, 6) or interlace:
        return None
    try:
        raw = zlib.decompress(idat)
    except zlib.error:
        return None
    stride = width * (3 if color == 2 else 4) + 1
    return [raw[y * stride:(y + 1) * stride] for y in range(height) if (y + 1) * stride <= len(raw)]
