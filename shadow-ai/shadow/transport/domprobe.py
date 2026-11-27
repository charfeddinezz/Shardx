"""One DOM protocol for both the real engine and the mock.

Engines never hand-write `Runtime.evaluate` strings. They ask this module for
facts — a snapshot of the page's interactive elements, an element's text, its
rect — and the module answers from `window.__shadowProbe`, a small read-only
runtime it injects once per page (and re-injects on navigation).

The mock engine implements the same protocol against its parsed document, so
the Discovery and Page Understanding engines are exercised by the test suite
without a browser, and run unchanged against a real ShardBrowser.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..errors import CdpError, TransportError
from .cdp import CdpClient

CALL = '__shadowProbe'

RUNTIME_JS = r"""
(() => {
  if (window.__shadowProbe) { return "ready"; }

  const INTERACTIVE = new Set(["a","button","input","select","textarea","option","summary"]);
  const TEXTY = new Set(["h1","h2","h3","h4","p","li","span","a","button","td","th",
                         "label","option","div","strong","em","small","legend"]);

  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
    return !(r.bottom < -4000 || r.top > (innerHeight + 8000));
  };

  const textOf = (el) => {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return "";
    const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return t.length > 300 ? t.slice(0, 300) : t;
  };

  const nameOf = (el) => {
    for (const a of ["aria-label","placeholder","title","alt"]) {
      const v = el.getAttribute(a);
      if (v) return v;
    }
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return (lab.innerText || "").trim();
    }
    const wrap = el.closest("label");
    if (wrap) return (wrap.innerText || "").trim();
    if (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA") {
      return el.getAttribute("name") || "";
    }
    return textOf(el).slice(0, 120);
  };

  const roleOf = (el) => el.getAttribute("role") || ({
    a: "link", button: "button", select: "combobox", textarea: "textbox",
    h1: "heading", h2: "heading", h3: "heading", li: "listitem", td: "cell",
    input: ({"search":"searchbox","email":"textbox","password":"textbox",
             "checkbox":"checkbox","radio":"radio","submit":"button",
             "number":"spinbutton","tel":"textbox","url":"textbox"})[el.type] || "textbox"
  })[el.tagName.toLowerCase()] || "";

  const cssPath = (el) => {
    if (el.id) return "#" + CSS.escape(el.id);
    const bits = [];
    let node = el;
    while (node && node.nodeType === 1 && bits.length < 4) {
      let part = node.tagName.toLowerCase();
      const cls = (node.className && typeof node.className === "string")
        ? node.className.trim().split(/\s+/).filter(Boolean) : [];
      if (cls.length) part += "." + cls.map(c => CSS.escape(c)).join(".");
      const parent = node.parentElement;
      if (parent) {
        const same = [...parent.children].filter(c => c.tagName === node.tagName);
        if (same.length > 1) part += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
      }
      bits.unshift(part);
      if (node.id) { bits[0] = "#" + CSS.escape(node.id); break; }
      node = node.parentElement;
    }
    return bits.join(" > ");
  };

  const describe = (el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || "",
      classes: (typeof el.className === "string" && el.className)
        ? el.className.trim().split(/\s+/).filter(Boolean) : [],
      text: textOf(el),
      attrs: Object.fromEntries([...el.attributes].map(a => [a.name, a.value])
                 .filter(([k]) => k !== "class" && k !== "style")),
      rect: { x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height },
      visible: vis(el),
      interactive: INTERACTIVE.has(el.tagName.toLowerCase()) || el.hasAttribute("onclick")
                   || (el.getAttribute("role") || "").match(/button|link|tab|menuitem/) !== null
                   || !!el.getAttribute("tabindex"),
      role: roleOf(el),
      name: nameOf(el),
      value: ("value" in el) ? String(el.value) : "",
      checked: !!(el.checked),
      disabled: !!(el.disabled) || el.getAttribute("aria-disabled") === "true",
      href: el.getAttribute("href") || "",
      type: el.getAttribute("type") || "",
      active: el === document.activeElement,
      key: el.id || ("@" + el.tagName.toLowerCase() + ":" + cssPath(el)),
      selector: cssPath(el)
    };
  };

  window.__shadowProbe = (name, arg) => {
    try {
      switch (name) {
        case "snapshot": {
          const sel = (arg && arg.selector) || "a,button,input,select,textarea,label,option,summary," +
            "[role],[onclick],h1,h2,h3,li";
          const limit = (arg && arg.limit) || 400;
          const nodes = [...document.querySelectorAll(sel)];
          return nodes.slice(0, limit).map(describe);
        }
        case "text": {
          const el = document.querySelector(arg);
          return el ? textOf(el) : null;
        }
        case "texts": return [...document.querySelectorAll(arg)].map(textOf);
        case "attribute": {
          const el = document.querySelector(arg[0]);
          return el ? el.getAttribute(arg[1]) : null;
        }
        case "count": return document.querySelectorAll(arg).length;
        case "active": return document.activeElement ? describe(document.activeElement) : null;
        case "value": {
          const el = document.getElementById(arg);
          return el && ("value" in el) ? String(el.value) : null;
        }
        case "setValue": {
          const el = document.getElementById(arg[0]);
          if (!el) return false;
          const proto = Object.getPrototypeOf(el);
          const setter = Object.getOwnPropertyDescriptor(proto, "value");
          if (setter && setter.set) setter.set.call(el, arg[1]);
          else el.value = arg[1];
          el.dispatchEvent(new Event("input", {bubbles: true}));
          el.dispatchEvent(new Event("change", {bubbles: true}));
          return true;
        }
        case "html": return document.body ? document.body.innerText : "";
        case "links": return [...document.querySelectorAll("a[href]")]
          .map(a => ({text: textOf(a), href: a.href}));
        case "scrollY": return window.scrollY;
        case "forms": return [...document.forms].map(f => ({
          action: f.getAttribute("action") || location.href,
          method: (f.getAttribute("method") || "get").toLowerCase(),
          fields: [...f.elements].filter(e => e.name).map(e => ({
            name: e.name, type: (e.type || e.tagName).toLowerCase(),
            required: !!e.required, label: nameOf(e)
          }))
        }));
        case "ready": return document.readyState;
        default: return null;
      }
    } catch (err) {
      return {__error: String(err && err.message || err)};
    }
  };
  return "installed";
})()
"""


class PageProbe:
    """Read-only view of the page, shared by every engine."""

    def __init__(self, cdp: CdpClient):
        self.cdp = cdp
        self._installed = False
        self._url = ""
        self.calls = 0

    # -- runtime install ----------------------------------------------
    def ensure(self, force: bool = False) -> bool:
        url = ""
        try:
            url = self.cdp.current_url()
        except (CdpError, TransportError):
            pass
        if force or url != self._url or not self._installed:
            self._url = url
            try:
                self.cdp.call("Page.addScriptToEvaluateOnNewDocument",
                              {"source": RUNTIME_JS}, timeout=10)
            except (CdpError, TransportError):
                pass  # not fatal; the one-shot evaluate below still installs it
            try:
                self.cdp.evaluate(RUNTIME_JS, timeout=15)
                self._installed = True
            except (CdpError, TransportError) as exc:
                self._installed = False
                raise CdpError(f"cannot install the DOM reader: {exc}") from None
        return self._installed

    # -- protocol -----------------------------------------------------
    def ask(self, name: str, arg: Any = None, ensure: bool = True) -> Any:
        if ensure:
            self.ensure()
        self.calls += 1
        expression = f"window.{CALL}({json.dumps(name)}, {json.dumps(arg)})"
        value = self.cdp.evaluate(expression, timeout=30)
        if isinstance(value, dict) and "__error" in value:
            raise CdpError(f"DOM reader failed in {name}: {value['__error']}")
        return value

    # -- convenience ---------------------------------------------------
    def snapshot(self, selector: str = "", limit: int = 400) -> List[Dict[str, Any]]:
        return self.ask("snapshot", {"selector": selector, "limit": limit}) or []

    def interactive(self, limit: int = 400) -> List[Dict[str, Any]]:
        return [n for n in self.snapshot(limit=limit)
                if n.get("interactive") and n.get("visible")]

    def by_id(self, element_id: str) -> Optional[Dict[str, Any]]:
        for node in self.snapshot(selector=f"#{_escape(element_id)}", limit=5):
            return node
        return None

    def text(self, selector: str) -> Optional[str]:
        return self.ask("text", selector)

    def texts(self, selector: str) -> List[str]:
        return self.ask("texts", selector) or []

    def count(self, selector: str) -> int:
        return int(self.ask("count", selector) or 0)

    def active(self) -> Optional[Dict[str, Any]]:
        return self.ask("active", None)

    def value(self, element_id: str) -> Optional[str]:
        return self.ask("value", element_id)

    def set_value(self, element_id: str, value: str) -> bool:
        return bool(self.ask("setValue", [element_id, value]))

    def links(self) -> List[Dict[str, str]]:
        return self.ask("links", None) or []

    def forms(self) -> List[Dict[str, Any]]:
        return self.ask("forms", None) or []

    def ready_state(self) -> str:
        return self.ask("ready", None, ensure=False) or ""

    def forget(self) -> None:
        self._installed = False
        self._url = ""


def probe(cdp: CdpClient, name: str, arg: Any = None) -> Any:
    """One-shot convenience for scripts and tests."""
    return PageProbe(cdp).ask(name, arg)


def _escape(ident: str) -> str:
    return re.sub(r'(["\\])', r"\\\1", ident)
