"""Actions Engine — the verbs, in one place, independent of who asks.

A plan resolves a step; a reasoning loop decides one; a person types one into
the CLI. All three end here, so an action behaves the same however it was
chosen, and Safety sees every one of them through the same call.

Nothing in this file plans. It takes a `Step` and carries it out against the
page as it is right now, or raises an error that names what was missing.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..errors import (ApprovalRequired, ElementNotFound, ShadowError, StepFailed)
from ..util import truncate
from .base import Engine

MAX_TEXT = 1200
READABLE = ("title", "page title", "heading", "url", "price", "prices", "total", "amount",
            "page", "text", "", "links", "fields", "table")


class ActionsEngine(Engine):
    """Carries out one step. Engines below it are looked up on each call, so a
    restart of one (the browser, the keyboard) is picked up without wiring."""

    name = "actions"
    depends_on = ("safety", "discovery", "keyboard", "mouse", "page", "data", "memory",
                  "browser", "pages")

    def __init__(self, ctx):
        super().__init__(ctx)
        self.performed = 0

    # -- the entry point ----------------------------------------------
    def perform(self, step, task=None, index: int = -1) -> Any:
        """Return a small, JSON-safe description of what happened."""
        self.performed += 1
        self.note(action=step.action, target=step.target[:60])
        handler = getattr(self, f"_do_{step.action}", None)
        if handler is None:
            raise StepFailed(f"unknown step action {step.action!r}", step=index)
        return handler(step, task, index)

    # -- navigation ---------------------------------------------------
    def _do_goto(self, step, task, index):
        safety = self.engine("safety")
        safety.enforce(safety.check_url(step.target))
        cdp = self.ctx.ensure_cdp()
        cdp.navigate(step.target)
        return {"url": cdp.current_url(), "title": cdp.title()}

    def _do_back(self, step, task, index):
        cdp = self.ctx.ensure_cdp()
        cdp.evaluate("history.back()")
        cdp.wait_ready()
        return {"url": cdp.current_url()}

    def _do_forward(self, step, task, index):
        cdp = self.ctx.ensure_cdp()
        cdp.evaluate("history.forward()")
        cdp.wait_ready()
        return {"url": cdp.current_url()}

    def _do_reload(self, step, task, index):
        cdp = self.ctx.ensure_cdp()
        cdp.call("Page.reload", {"ignoreCache": False}, timeout=30)
        cdp.wait_ready(timeout=30)
        return {"url": cdp.current_url()}

    def _do_verify_loaded(self, step, task, index):
        cdp = self.ctx.ensure_cdp()
        return {"loaded": cdp.wait_ready(timeout=30), "url": cdp.current_url()}

    def _do_wait(self, step, task, index):
        seconds = float(step.value or 1.0)
        if step.target and step.target.startswith(("#", ".", "[")):
            # waiting for an element is more honest than waiting for a clock
            deadline = time.time() + max(seconds, 15)
            probe = self.ctx.page()
            while time.time() < deadline:
                if probe.count(step.target):
                    return {"appeared": step.target, "after_s": round(max(0.0, max(seconds, 15) - (deadline - time.time())), 2)}
                time.sleep(0.2)
            raise ElementNotFound(f"{step.target!r} did not appear within {max(seconds, 15):.0f}s")
        time.sleep(seconds)
        return {"waited_s": seconds}

    def _do_finish(self, step, task, index):
        return {"finished": True}

    # -- the page -----------------------------------------------------
    def _do_find(self, step, task, index):
        keyboard, mouse = self.engine("keyboard"), self.engine("mouse")
        discovery, probe = self.engine("discovery"), self.ctx.page()
        field = discovery.find_field(probe, "search")
        if field is not None:
            keyboard.fill_field(probe, field.node, step.target)
            keyboard.press("Enter")
            self.ctx.cdp.wait_ready()
            return {"searched": step.target, "url": self.ctx.cdp.current_url()}
        candidate = discovery.pick_or_none(probe, step.target)
        if candidate is not None:
            mouse.click_element(probe, candidate.node)
            self.ctx.cdp.wait_ready()
            return {"clicked": candidate.label, "url": self.ctx.cdp.current_url()}
        raise ElementNotFound(f"no search field and no link matching {step.target!r}")

    def _do_fill(self, step, task, index):
        keyboard, discovery = self.engine("keyboard"), self.engine("discovery")
        safety, memory = self.engine("safety"), self.engine("memory")
        probe = self.ctx.page()
        field = discovery.find_field(probe, step.target)
        if field is None:
            raise ElementNotFound(f"no field matching {step.target!r}")
        verdict = safety.check_action("fill", target=step.target, value=step.value)
        # the value of a password field is never written to memory or the journal
        remember = safety.redact(step.value) if verdict else step.value
        filled = keyboard.fill_field(probe, field.node, step.value)
        if filled.get("verified") is False:
            raise StepFailed(f"field {step.target!r} did not keep the value", step=index)
        memory.learn(probe.ask("ready") and self._url(), "field:" + step.target,
                     field.node.get("selector"))
        # The value itself is never returned: a step output goes to the journal.
        return {"field": field.node.get("selector"), "verified": filled.get("verified"),
                "chars": len(step.value), "value_kept": remember != "<redacted>"}

    def _do_login(self, step, task, index):
        keyboard, mouse = self.engine("keyboard"), self.engine("mouse")
        discovery, safety = self.engine("discovery"), self.engine("safety")
        memory = self.engine("memory")
        probe = self.ctx.page()
        user = step.target or memory.recall("user", namespace="credentials", default="")
        password = step.value or memory.recall("password", namespace="credentials", default="")
        if not user or not password:
            raise ElementNotFound("login needs a user and a password: 'log in with user: X pass: Y'")
        safety.check_action("login", target="login", value="password")
        user_field = discovery.find_field(probe, "username") or discovery.find_field(probe, "email")
        pass_field = discovery.find_field(probe, "password")
        if user_field is None or pass_field is None:
            raise ElementNotFound("this page has no username/password pair")
        keyboard.fill_field(probe, user_field.node, user)
        keyboard.fill_field(probe, pass_field.node, password)
        submit = (discovery.pick_or_none(probe, "sign in button")
                  or discovery.pick_or_none(probe, "log in button"))
        pages = self.engine("pages")
        pages.before_action()
        if submit is not None:
            mouse.click_element(probe, submit.node)
        else:
            keyboard.press("Enter")
        self.ctx.cdp.wait_ready()
        follow = pages.after_action(label="sign in")
        memory.remember("user", user, "credentials")
        memory.remember("password", safety.redact(password), "credentials")
        return {"logged_in": True, "url": self.ctx.cdp.current_url(), **follow}

    def _do_click(self, step, task, index):
        safety, discovery, mouse = self.engine("safety"), self.engine("discovery"), self.engine("mouse")
        memory, pages = self.engine("memory"), self.engine("pages")
        probe = self.ctx.page()
        url = self._url()
        safety.check_action("click", target=step.target, url=url,
                            approved=bool(task and getattr(task, "approved", False)))
        candidate = discovery.pick(probe, step.target)
        pages.before_action()
        mouse.click_element(probe, candidate.node)
        self.ctx.cdp.wait_ready()
        follow = pages.after_action(label=candidate.label)
        memory.learn(url, "click:" + step.target, candidate.selector)
        return {"clicked": candidate.label, "selector": candidate.selector,
                "url": self.ctx.cdp.current_url(), **follow}

    def _do_submit(self, step, task, index):
        safety, discovery = self.engine("safety"), self.engine("discovery")
        keyboard, mouse = self.engine("keyboard"), self.engine("mouse")
        probe = self.ctx.page()
        label = step.target or "submit"
        safety.check_action("submit", target=label, url=self._url(),
                            approved=bool(task and getattr(task, "approved", False)))
        pages = self.engine("pages")
        candidate = discovery.pick_or_none(probe, label) or discovery.pick_or_none(probe, "submit button")
        pages.before_action()
        if candidate is not None:
            mouse.click_element(probe, candidate.node)
        else:
            keyboard.press("Enter")
        self.ctx.cdp.wait_ready()
        follow = pages.after_action(label=label)
        return {"submitted": True, "url": self.ctx.cdp.current_url(), **follow}

    def _do_hover(self, step, task, index):
        discovery, mouse, probe = self.engine("discovery"), self.engine("mouse"), self.ctx.page()
        candidate = discovery.pick(probe, step.target)
        mouse.hover(probe, candidate.node)
        return {"hovered": candidate.label}

    def _do_select(self, step, task, index):
        discovery, mouse, keyboard = self.engine("discovery"), self.engine("mouse"), self.engine("keyboard")
        data, probe = self.engine("data"), self.ctx.page()
        field = discovery.find_field(probe, step.target)
        if field is None:
            raise ElementNotFound(f"no select field matching {step.target!r}")
        mouse.click_element(probe, field.node)
        # Native select popups are not DOM. Choosing by option text through the
        # page is what a person's keyboard does, and it is what stays reliable.
        self.ctx.cdp.evaluate(
            "(() => { const el = document.querySelector(%r); if (!el) return false;"
            " for (const o of el.options) { if (o.text.trim().toLowerCase().includes(%r)) "
            "{ el.value = o.value; el.dispatchEvent(new Event('change', {bubbles:true})); return true; } }"
            " return false; })()" % (field.node.get("selector"), (step.value or "").lower()))
        return {"selected": step.value, "field": field.node.get("selector"),
                "options": len(data.list_items(probe, f"{field.node.get('selector')} option"))}

    def _do_press(self, step, task, index):
        return self.engine("keyboard").press(step.target)

    def _do_type(self, step, task, index):
        return self.engine("keyboard").type(step.value or step.target)

    def _do_scroll(self, step, task, index):
        return self.engine("mouse").scroll(step.target or "down", int(step.value or 480))

    # -- reading ------------------------------------------------------
    def _do_read(self, step, task, index):
        discovery, page, data = self.engine("discovery"), self.engine("page"), self.engine("data")
        probe = self.ctx.page()
        goal = getattr(task, "goal", "")
        model = page.understand(probe, self.ctx.cdp)
        target = step.target
        if target in ("title", "page title", "heading"):
            return model.title
        if target == "url":
            return model.url
        if target in ("", "page", "text"):
            answer = page.answer(model, step.value or goal)
            return answer["answer"] or truncate(model.text, MAX_TEXT)
        if target in ("price", "prices", "total", "amount"):
            prices = data.prices(probe)
            if prices:
                return prices[0]["amount"]
        if target.startswith(("#", ".", "[")) or " " in target:
            try:
                text = probe.text(target)
            except Exception:
                text = None
            if text:
                return text
        candidate = discovery.pick_or_none(probe, target)
        if candidate is not None:
            return candidate.node.get("text") or candidate.node.get("name")
        answer = page.answer(model, step.value or target)
        if answer["answer"]:
            return answer["answer"]
        return {"text": truncate(model.text, 400), "title": model.title}

    def _do_extract(self, step, task, index):
        data, probe = self.engine("data"), self.ctx.page()
        if step.target in ("table", "tables"):
            return data.table(probe)
        if step.target in ("links", "link"):
            return data.links(probe)
        if step.target in ("fields", "form"):
            return data.fields(probe)
        return data.list_items(probe, step.target or "li")

    def _do_assert(self, step, task, index):
        probe = self.ctx.page()
        if step.target.startswith(("#", ".", "[")):
            actual = probe.text(step.target)
            ok = bool(actual and (step.value in actual if step.value else actual))
            return {"assert": ok, "actual": truncate(actual or "", 200)}
        count = probe.count(step.target)
        return {"assert": count > 0, "count": count}

    # -- side channels ------------------------------------------------
    def _do_remember(self, step, task, index):
        self.engine("memory").remember(step.target or "note", step.value,
                                       "task:" + getattr(task, "id", "-"))
        return {"remembered": step.target}

    def _do_approve(self, step, task, index):
        if task is not None and getattr(task, "approved", False):
            return {"approved": True, "action": truncate(step.target, 100)}
        raise ApprovalRequired(
            f"the goal asks for an irreversible action: {truncate(step.target, 100)}",
            category="purchase", task=getattr(task, "id", ""))

    def _do_screenshot(self, step, task, index):
        return self.engine("vision").shot().to_dict()

    def _do_ocr(self, step, task, index):
        return self.engine("vision").ocr()

    def _do_dialog(self, step, task, index):
        """Answer the dialog that is open: accept / dismiss, with an optional text."""
        pages = self.engine("pages")
        answer = (step.target or "accept").lower()
        return pages.answer_dialog(accept=answer not in ("dismiss", "cancel", "no"),
                                   text=step.value)

    def _do_switch_tab(self, step, task, index):
        pages = self.engine("pages")
        return pages.switch_tab(step.target or "last")

    def _do_close_tab(self, step, task, index):
        return self.engine("pages").close_extra_tabs()

    def _do_record(self, step, task, index):
        """Never silently ignored: recording is not a page action."""
        return {"recorded": True, "note": "this step only marks the run for a person"}

    # -- helpers ------------------------------------------------------
    def _url(self) -> str:
        try:
            return self.ctx.cdp.current_url() if self.ctx.cdp else ""
        except Exception:
            return ""


def summarise(output: Any, limit: int = MAX_TEXT) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return truncate(output, limit)
    if isinstance(output, dict):
        import json
        try:
            return truncate(json.dumps(output, ensure_ascii=False, default=str), limit)
        except Exception:
            return truncate(str(output), limit)
    return truncate(str(output), limit)
