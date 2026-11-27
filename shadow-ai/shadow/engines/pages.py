"""Page Events Engine — the things a page does that are not part of the DOM.

Three of them stop a run dead on a real site:

* **a new tab or window** opened by a click (OAuth, a receipt, a payment page);
* **a JavaScript dialog** (`alert`, `confirm`, `prompt`, `beforeunload`) —
  while one is open, every `Runtime.evaluate` waits forever;
* **the tab that clicked is no longer the one that holds the result**.

This engine subscribes to CDP target and dialog events on the browser channel,
keeps a list of pages, answers a dialog when asked to, and can switch the
session to whichever page actually carries the work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import CdpError, TransportError
from ..util import iso, now, truncate
from .base import Engine


@dataclass
class PageTab:
    target_id: str
    url: str = ""
    title: str = ""
    opener_id: str = ""
    created_at: float = field(default_factory=now)
    closed: bool = False
    attached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"target": self.target_id[:8], "url": truncate(self.url, 120),
                "title": truncate(self.title, 80), "opener": self.opener_id[:8] or None,
                "attached": self.attached, "closed": self.closed,
                "age_s": round(now() - self.created_at, 1)}


@dataclass
class Dialog:
    kind: str
    message: str
    target_id: str = ""
    ts: float = field(default_factory=now)
    answered: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "message": truncate(self.message, 200),
                "target": self.target_id[:8], "answered": self.answered, "ts": iso(self.ts)}


class PagesEngine(Engine):
    name = "pages"
    depends_on = ("browser",)

    def __init__(self, ctx):
        super().__init__(ctx)
        self.tabs: Dict[str, PageTab] = {}
        self.dialogs: List[Dialog] = []
        self.pending: List[Dialog] = []
        self.switches = 0
        self.closed = 0
        self._subscribed = False
        self._snapshot: Optional[set] = None
        self._main_target = ""
        self.policy = "switch"     # switch | ignore | close

    # -- lifecycle ----------------------------------------------------
    def start(self) -> None:
        super().start()
        self.policy = getattr(self.ctx.cfg, "tabs_policy", "switch")
        self.subscribe()

    def subscribe(self) -> None:
        """Attach to whatever CDP client exists now. Safe to call again after a
        restart: this is how the engine survives the browser being rebuilt."""
        cdp = self.ctx.cdp
        if cdp is None or self._subscribed:
            return
        self._main_target = cdp.target_id or ""
        if self._main_target:
            self.tabs.setdefault(self._main_target, PageTab(target_id=self._main_target,
                                                            url=self._url(), attached=True))
        on_event = cdp._on_event
        cdp._on_event = self._dispatch  # keep ours only; the client had none by default
        self._subscribed = True

    def reset(self) -> None:
        super().reset()
        self._subscribed = False
        self.pending.clear()

    # -- CDP events ---------------------------------------------------
    def _dispatch(self, payload: dict) -> None:
        method = payload.get("method", "")
        params = payload.get("params") or {}
        try:
            if method == "Target.targetCreated":
                self._tab_created(params.get("targetInfo") or {})
            elif method == "Target.targetInfoChanged":
                self._tab_changed(params.get("targetInfo") or {})
            elif method == "Target.targetDestroyed":
                self._tab_destroyed(params.get("targetId", ""))
            elif method == "Page.javascriptDialogOpening":
                self._dialog_opened(params)
            elif method == "Page.javascriptDialogClosed":
                self._dialog_closed(params)
            elif method == "Page.frameNavigated":
                frame = params.get("frame") or {}
                if frame.get("parentId") is None:
                    tab = self.tabs.get(self._main_target)
                    if tab is not None:
                        tab.url = frame.get("url", tab.url)
        except Exception:  # noqa: BLE001 - an event handler never breaks the session
            pass

    def _tab_created(self, info: Dict[str, Any]) -> None:
        if info.get("type") != "page":
            return
        target = info.get("targetId", "")
        if not target:
            return
        tab = PageTab(target_id=target, url=info.get("url", ""), title=info.get("title", ""),
                      opener_id=info.get("openerId", "") or "")
        self.tabs[target] = tab
        if not self._main_target:
            self._main_target = target
        self.emit("tab_opened", url=truncate(tab.url, 100), by_popup=bool(tab.opener_id))

    def _tab_changed(self, info: Dict[str, Any]) -> None:
        tab = self.tabs.get(info.get("targetId", ""))
        if tab is None:
            return
        tab.url = info.get("url", tab.url)
        tab.title = info.get("title", tab.title)

    def _tab_destroyed(self, target: str) -> None:
        tab = self.tabs.get(target)
        if tab is None:
            return
        tab.closed = True
        self.closed += 1
        if target == self._main_target:
            self.emit("main_tab_closed", url=truncate(tab.url, 100))

    def _dialog_opened(self, params: Dict[str, Any]) -> None:
        dialog = Dialog(kind=params.get("type", "alert"), message=params.get("message", ""),
                        target_id=params.get("targetId", self._main_target))
        self.dialogs.append(dialog)
        self.pending.append(dialog)
        self.emit("dialog", kind=dialog.kind, message=truncate(dialog.message, 120))
        # An unhandled dialog blocks the page. Answering a plain alert is not a
        # decision; a confirm or a prompt is, and those wait for `answer_dialog`.
        if dialog.kind == "alert":
            self.answer_dialog(accept=True)

    def _dialog_closed(self, params: Dict[str, Any]) -> None:
        self.pending = [d for d in self.pending if not d.answered]

    # -- what the rest of the system calls ----------------------------
    def before_action(self) -> None:
        """Snapshot the tab list, so a new tab can be attributed to this click."""
        self.sync()
        # A Target.targetCreated event may land before we get to look again, so
        # the comparison point has to be taken *before* the click, not at wait.
        self._snapshot = set(self.tabs)

    def after_action(self, label: str = "", timeout: float = 2.0) -> Dict[str, Any]:
        """Called after a click: answer a dialog, follow a popup, report both."""
        follow: Dict[str, Any] = {}
        dialog = self.answer_pending_confirm()
        if dialog is not None:
            follow["dialog"] = dialog.to_dict()
        new_tab = self.wait_for_new_tab(timeout=timeout)
        self._snapshot = None
        if new_tab is not None:
            if self.policy == "switch":
                self.switch_tab(new_tab.target_id)
                follow["switched_to"] = new_tab.to_dict()
            elif self.policy == "close":
                self._close_target(new_tab.target_id)
                follow["closed_tab"] = new_tab.to_dict()
            else:
                follow["new_tab"] = new_tab.to_dict()
        return follow

    def sync(self) -> List[PageTab]:
        """Refresh the tab list from CDP and make sure the page session is attached."""
        cdp = self.ctx.cdp
        if cdp is None:
            return []
        try:
            infos = cdp.get_targets()
        except (CdpError, TransportError):
            return list(self.tabs.values())
        for info in infos:
            if info.get("type") != "page":
                continue
            target = info.get("targetId", "")
            tab = self.tabs.get(target)
            if tab is None:
                tab = PageTab(target_id=target, url=info.get("url", ""),
                              title=info.get("title", ""), opener_id=info.get("openerId", "") or "")
                self.tabs[target] = tab
            else:
                tab.url = info.get("url") or tab.url
                tab.title = info.get("title") or tab.title
        if not self._main_target:
            pages = [t for t in self.tabs.values() if not t.closed]
            if pages:
                self._main_target = sorted(pages, key=lambda t: t.created_at)[0].target_id
        self.subscribe()
        return list(self.tabs.values())

    def open_tabs(self) -> List[PageTab]:
        return [t for t in self.sync() if not t.closed]

    def pending_dialogs(self) -> List[Dialog]:
        return [d for d in self.pending if not d.answered]

    def wait_for_new_tab(self, timeout: float = 2.0) -> Optional[PageTab]:
        known = self._snapshot if self._snapshot is not None else set(self.tabs)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for tab in self.open_tabs():
                if tab.target_id not in known:
                    return tab
            time.sleep(0.1)
        return None

    def switch_tab(self, which: str = "last") -> Dict[str, Any]:
        """Point the page session at another tab. `last`, `main`, or a target id."""
        tabs = self.open_tabs()
        if not tabs:
            return {"switched": False, "reason": "no open tabs"}
        if which in ("last", "newest", "popup"):
            target = sorted(tabs, key=lambda t: t.created_at)[-1]
        elif which in ("main", "first", "original"):
            target = self.tabs.get(self._main_target) or sorted(tabs, key=lambda t: t.created_at)[0]
            if target.closed:
                target = sorted(tabs, key=lambda t: t.created_at)[0]
        elif which in self.tabs:
            target = self.tabs[which]
        else:
            matches = [t for t in tabs if which.lower() in (t.url or "").lower()
                       or which.lower() in (t.title or "").lower()]
            if not matches:
                return {"switched": False, "reason": f"no tab matching {which!r}"}
            target = matches[0]
        cdp = self.ctx.ensure_cdp()
        try:
            session = cdp.call("Target.attachToTarget",
                               {"targetId": target.target_id, "flatten": True}, timeout=15)
            cdp.session_id = session.get("sessionId")
            cdp.target_id = target.target_id
            cdp.call("Page.enable", {}, timeout=10)
            cdp.call("Runtime.enable", {}, timeout=10)
        except (CdpError, TransportError) as exc:
            self.fail(exc, tab=target.target_id)
            return {"switched": False, "reason": str(exc)[:160]}
        self.ctx.probe.forget()
        self.switches += 1
        self.subscribed = True
        self.emit("switched", url=truncate(target.url, 100))
        return {"switched": True, "tab": target.to_dict()}

    def answer_dialog(self, accept: bool = True, text: Optional[str] = None) -> Dict[str, Any]:
        """Answer the newest open dialog. Safe to call when there is none."""
        pending = self.pending_dialogs()
        if not pending:
            return {"answered": False, "reason": "no open dialog"}
        dialog = pending[-1]
        params: Dict[str, Any] = {"accept": bool(accept)}
        if text is not None:
            params["promptText"] = text
        try:
            self.ctx.ensure_cdp().call("Page.handleJavaScriptDialog", params, timeout=10)
        except (CdpError, TransportError) as exc:
            return {"answered": False, "reason": str(exc)[:160], "dialog": dialog.to_dict()}
        dialog.answered = "accept" if accept else "dismiss"
        self.pending.remove(dialog)
        self.emit("dialog_answered", kind=dialog.kind, answer=dialog.answered)
        return {"answered": True, "dialog": dialog.to_dict()}

    def answer_pending_confirm(self) -> Optional[Dialog]:
        """A `confirm` that a click raised: accept it, because the click meant yes."""
        pending = [d for d in self.pending_dialogs() if d.kind in ("confirm", "beforeunload")]
        if not pending:
            return None
        result = self.answer_dialog(accept=True)
        return pending[-1] if result.get("answered") else None

    def answer_prompt(self, text: str) -> Dict[str, Any]:
        return self.answer_dialog(accept=True, text=text)

    def close_extra_tabs(self, keep_main: bool = True) -> Dict[str, Any]:
        """Close popups a run no longer needs. The main page is kept."""
        closed = []
        for tab in self.open_tabs():
            if keep_main and tab.target_id == self._main_target:
                continue
            if tab.target_id == self.ctx.cdp.target_id and not keep_main:
                continue
            if self._close_target(tab.target_id):
                closed.append(tab.target_id[:8])
        if closed:
            self.switch_tab("main")
        return {"closed": closed}

    def _close_target(self, target: str) -> bool:
        try:
            self.ctx.ensure_cdp().call("Target.closeTarget", {"targetId": target}, timeout=10)
            if target in self.tabs:
                self.tabs[target].closed = True
            self.closed += 1
            return True
        except (CdpError, TransportError) as exc:
            self.fail(exc, target=target)
            return False

    # -- reporting ----------------------------------------------------
    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"tabs": len(self.open_tabs()), "pending_dialogs": len(self.pending_dialogs()),
                     "switches": self.switches, "closed": self.closed, "policy": self.policy,
                     "subscribed": self._subscribed})
        return info

    def report(self) -> Dict[str, Any]:
        return {"tabs": [t.to_dict() for t in self.open_tabs()],
                "dialogs": [d.to_dict() for d in self.dialogs[-10:]],
                "pending": [d.to_dict() for d in self.pending_dialogs()],
                "switches": self.switches, "closed": self.closed,
                "subscribed": self._subscribed, "policy": self.policy}

    def _url(self) -> str:
        try:
            return self.ctx.cdp.current_url() if self.ctx.cdp else ""
        except Exception:
            return ""
