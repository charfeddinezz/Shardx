"""Intelligence Engine — understand the goal, choose the next step.

Two halves, deliberately separated:

* `GoalParser` — turns a sentence in Arabic, English or French into intents
  (`open`, `find`, `fill`, `click`, `read`, …). Deterministic, offline, and
  unit-testable. This is what runs by default.
* `LocalReasoner` — an optional bridge to a model *you* run locally (Ollama
  or any OpenAI-compatible server on this machine). It never gets the only
  vote: the planner rejects anything outside the allowed action set, and the
  Safety Engine sees every action regardless of who proposed it.

Nothing leaves the computer. If no local model is running, the heuristic
planner is still a complete operator for form-filling, searching, reading and
navigation.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.config import IntelligenceConfig
from .base import Engine

# --------------------------------------------------------------------------
# Lexicons
# --------------------------------------------------------------------------

URL_RE = re.compile(
    r"(https?://[^\s'\"<>]+|(?<![@\w./:=])[\w-]+\.(?:com|net|org|io|dev|co|app|ai|local)\b(?:/[^\s'\"@]*)?)")
QUOTED = re.compile(r"[\"'“”«»]([^\"'“”«»]{1,120})[\"'“”«»]")

VERBS = {
    "open": ["open", "go to", "goto", "visit", "navigate", "head to", "افتح", "اذهب", "زر", "انتقل",
             "ouvre", "ouvrir", "va sur", "aller sur", "visite"],
    "find": ["find", "search", "look for", "look up", "ابحث", "جد", "recherche",
             "chercher", "trouve", "trouver"],
    "click": ["click", "press", "tap", "hit", "اضغط", "انقر", "clique", "cliquer", "appuie"],
    "fill": ["fill", "type", "enter", "write", "put", "املأ", "اكتب", "ادخل", "remplis", "remplir",
             "écris", "ecris", "saisis", "saisir"],
    "read": ["read", "get", "extract", "show", "what is", "what's", "tell me", "list", "اقرأ", "استخرج",
             "ما هو", "اخبرني", "ماذا", "lis", "lire", "extrais", "montre", "quel", "quelle"],
    "login": ["log in", "login", "sign in", "signin", "authenticate", "سجل الدخول", "تسجيل الدخول",
              "connecte", "connexion", "se connecter"],
    "submit": ["submit", "send", "confirm", "أرسل", "ارسل", "تأكيد", "envoie", "envoyer", "confirme"],
    "buy": ["buy", "purchase", "order", "checkout", "add to cart", "اشتري", "اشتر", "اطلب", "achète",
            "acheter", "commande", "panier"],
    "scroll": ["scroll", "مرر", "اسحب", "défile", "defile", "fais défiler"],
    "back": ["go back", "back", "ارجع", "رجوع", "retour", "reviens"],
    "wait": ["wait", "انتظر", "attend", "attends"],
}

FIELD_HINTS = {
    "email": ["email", "e-mail", "mail", "البريد", "بريد", "courriel"],
    "password": ["password", "pass", "pwd", "كلمة المرور", "كلمة السر", "mot de passe"],
    "username": ["username", "user", "login", "اسم المستخدم", "utilisateur"],
    "search": ["search", "query", "بحث", "ابحث", "recherche"],
    "message": ["message", "comment", "رسالة", "تعليق", "message"],
    "name": ["name", "full name", "الاسم", "nom"],
    "phone": ["phone", "tel", "mobile", "الهاتف", "رقم", "téléphone"],
    "address": ["address", "street", "العنوان", "adresse"],
    "quantity": ["quantity", "qty", "الكمية", "quantité"],
}

FILL_RE = re.compile(
    r"(?:fill|type|enter|write|put|املأ|اكتب|ادخل|remplis|écris|saisis)\s+"
    r"(?:in\s+)?(?:the\s+|le\s+|la\s+)?(?:champ\s+(?:de\s+)?)?(?P<field>[^=:]{1,40}?)\s*"
    r"(?:=|:|\bwith\b|\bavec\b|\bbis\b|\bبـ\b|\bب\b)\s*"
    r"(?P<value>[\"'“”«»]?[^\"'“”«»]{1,120}?)[\"'“”«»]?(?:\s*(?:,|;|\bthen\b|\bpuis\b|ثم|،)|$)",
    re.I,
)

# Words that end one instruction and start the next, in the three languages.
CLAUSE = re.compile(r"\s*(?:[,;،]|\bthen\b|\band then\b|\band\b(?=\s+(?:click|press|fill|open|go|read|find))|"
                    r"\bpuis\b|\bet puis\b|\bensuite\b|\bet\b|ثم|وبعد|بعد ذلك|"
                    r"\b(?:on|in|at|from)\b(?=\s+\S*(?:\.|/|https?:))|"          # on shop.local/contact
                    r"(?<=\w)\s+في\s+(?=\S*(?:\.|/|https?:))|"                  # في http://…
                    r"\bsur\s+(?=\S*(?:\.|/|https?:)))\s*", re.I)

# Small leading particles that are grammar, not part of a target label.
LEAD = re.compile(r"^(?:on|onto|the|le|la|les|un|une|sur|على|إلى|الى|في|عن)\s+", re.I)


@dataclass
class Intent:
    kind: str
    target: str = ""
    value: str = ""
    raw: str = ""
    confidence: float = 0.5
    pos: int = 10 ** 6

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "value": self.value,
                "confidence": round(self.confidence, 2)}


@dataclass
class Interpretation:
    goal: str
    language: str = "en"
    intents: List[Intent] = field(default_factory=list)
    url: str = ""
    question: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "language": self.language,
                "intents": [i.to_dict() for i in self.intents],
                "url": self.url, "question": self.question}


# --------------------------------------------------------------------------
# GoalParser
# --------------------------------------------------------------------------

class GoalParser:
    """Deterministic reading of a goal sentence."""

    def parse(self, goal: str) -> Interpretation:
        text = (goal or "").strip()
        # Verbs are looked for in prose, never inside a URL: "http://login.local"
        # is a place to go, not an instruction to log in.
        prose = URL_RE.sub(" ", text)
        low = prose.lower()
        interp = Interpretation(goal=text, language=_language(text))

        url_match = URL_RE.search(text)
        if url_match:
            url = url_match.group(1)
            if not url.startswith("http"):
                url = "https://" + url
            interp.url = url

        # open / navigate
        if interp.url and _any(low, VERBS["open"]) or (interp.url and low.startswith(("http", "www"))):
            interp.intents.append(Intent("open", target=interp.url, raw=text, confidence=0.95,
                                         pos=url_match.start() if url_match else 0))
        elif interp.url:
            interp.intents.append(Intent("open", target=interp.url, raw=text, confidence=0.8,
                                         pos=url_match.start() if url_match else 0))

        # login: "<site> user: x pass: y" or "log in as x / y"
        creds = self._credentials(text)
        if _any(low, VERBS["login"]) or (creds.get("user") and creds.get("password")):
            interp.intents.append(Intent("login", target=creds.get("user", ""),
                                         value=creds.get("password", ""), raw=text, confidence=0.85,
                                         pos=_at(text, _first_match(text, VERBS["login"]))))

        # fill "field = value" / "field: value"
        for match in FILL_RE.finditer(text):
            field_name = _clean(match.group("field"))
            value = _first_clause(_strip_quotes(match.group("value")))
            if field_name and value:
                interp.intents.append(Intent("fill", target=field_name, value=value,
                                             raw=match.group(0), confidence=0.8, pos=match.start()))

        # explicit "field=value" pairs anywhere
        for field_name, value in re.findall(r"\b([\w\u0600-\u06ff-]{2,24})\s*=\s*([^\s,;]+)", text):
            if field_name.lower() in ("http", "https"):
                continue
            if not any(i.kind == "fill" and i.value == value for i in interp.intents):
                interp.intents.append(Intent("fill", target=field_name, value=value,
                                             raw=f"{field_name}={value}", confidence=0.9,
                                             pos=_at(text, field_name)))

        # find / search — read the sentence with fill clauses removed, so the
        # word "search" inside "fill the search field" is not a second intent
        searchable = FILL_RE.sub(" ", prose)
        if _any(searchable.lower(), VERBS["find"]):
            without_url = URL_RE.sub(" ", searchable)
            # A find intent needs an explicit term. "Search shop.local" is a
            # navigation, not a query; only the quoted or "search X" form counts.
            query = self._search_term(without_url)
            if query:
                interp.intents.append(Intent("find", target=query, raw=text, confidence=0.8,
                                             pos=_at(text, _first_match(text, VERBS["find"]))))

        # click
        for verb in VERBS["click"]:
            pattern = re.compile(re.escape(verb) + r"\s+(?:on\s+)?(?:the\s+)?(.{2,60}?)(?:\s+(?:button|link|tab))?\s*$", re.I)
            match = pattern.search(text)
            if match:
                label = _clean(_first_clause(match.group(1)))
                label = re.sub(r"\s+(button|link|tab)$", "", label, flags=re.I)
                if label and label.lower() not in ("it",):
                    interp.intents.append(Intent("click", target=label, raw=match.group(0),
                                                 confidence=0.85, pos=match.start()))
                break

        # read / extract
        if _any(low, VERBS["read"]):
            selector = _extract_selector(text)
            question = _extract_question(text)
            interp.intents.append(Intent("read", target=selector or question or "page",
                                         value=question, raw=text, confidence=0.7,
                                         pos=_at(text, _first_match(text, VERBS["read"]))))
            interp.question = question

        # submit / send
        if _any(low, VERBS["submit"]):
            interp.intents.append(Intent("submit", target=_submit_label(text), raw=text, confidence=0.75,
                                         pos=_at(text, _first_match(text, VERBS["submit"]))))

        # purchase — an explicit, guarded intent
        if re.search(r"\b(buy|purchase|checkout|order now|اشتري|اشتر|achète|acheter)\b", low):
            interp.intents.append(Intent("purchase", target=text, raw=text, confidence=0.9,
                                         pos=_at(text, _first_match(text, VERBS["buy"]))))

        if _any(low, VERBS["scroll"]):
            direction = "down" if not re.search(r"\b(up|أعلى|haut)\b", low) else "up"
            interp.intents.append(Intent("scroll", target=direction, raw=text, confidence=0.8,
                                         pos=_at(text, _first_match(text, VERBS["scroll"]))))

        if _any(low, VERBS["back"]):
            interp.intents.append(Intent("back", raw=text, confidence=0.7,
                                         pos=_at(text, _first_match(text, VERBS["back"]))))

        if not interp.intents:
            interp.intents.append(Intent("read", target="page", value=text, raw=text, confidence=0.4))

        # Order by where the instruction sits in the sentence, with the
        # navigation first and the reading last — not by a fixed kind table,
        # which would put "press Search" before "fill the search field".
        interp.intents.sort(key=lambda i: (_BUCKET.get(i.kind, 1), i.pos))
        return interp

    # -- pieces -------------------------------------------------------
    @staticmethod
    def _credentials(text: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for user_label in ("user", "username", "login", "utilisateur", "المستخدم", "اسم المستخدم"):
            match = re.search(user_label + r"\s*[:=]\s*([^\s,;]+)", text, re.I)
            if match:
                out["user"] = match.group(1)
                break
        for pass_label in ("pass", "password", "pwd", "mot de passe", "كلمة المرور", "كلمة السر"):
            match = re.search(pass_label + r"\s*[:=]\s*([^\s,;]+)", text, re.I)
            if match:
                out["password"] = match.group(1)
                break
        as_match = re.search(r"(?:as|باسم)\s+([^\s/]+)\s*/\s*([^\s,;]+)", text, re.I)
        if as_match:
            out.setdefault("user", as_match.group(1))
            out.setdefault("password", as_match.group(2))
        return out

    @staticmethod
    def _search_term(text: str) -> str:
        quoted = QUOTED.search(text)
        if quoted:
            return quoted.group(1).strip()
        match = re.search(r"(?:search|find|look for|look up|ابحث عن|ابحث|جد|recherche|cherche)\s+(?:for\s+)?(.+)$",
                          text, re.I)
        if match:
            head = _clean(_first_clause(match.group(1)))
            head = CLAUSE.split(head, maxsplit=1)[0]
            head = re.split(r"\s+(?:on|in|في|sur|dans)\b", head)[0]
            head = re.sub(r"^(?:the|a|an|le|la|les|ال)\s+", "", head.strip(" .'\""), flags=re.I)
            return head.strip()
        return ""


_BUCKET = {"open": 0, "back": 0, "read": 2, "purchase": 2}


def _at(text: str, needle: str, default: int = 10 ** 6) -> int:
    """Where an instruction starts in the sentence (for ordering)."""
    if not needle:
        return default
    found = (text or "").lower().find(needle.lower()[:24])
    return found if found >= 0 else default


def _first_match(text: str, words: List[str]) -> str:
    best = ""
    low = (text or "").lower()
    for word in words:
        at = low.find(word)
        if at >= 0 and (not best or low.find(best) > at):
            best = word
    return best


def _any(low: str, words: List[str]) -> bool:
    """Whole-word match: "lis" must not fire inside "remplis", nor "login"
    inside a URL like http://login.local/ — callers pass URL-free prose.

    `\b` is not enough on Arabic: Python's word boundary is Unicode-aware, but
    an Arabic verb carries its conjunction, so "وابحث" (and-search) has no
    boundary before the verb. `_starts_word` looks at the letter before the
    match and accepts it when it is a single-letter prefix like و / ف / ل.
    """
    for word in words:
        for match in re.finditer(re.escape(word), low, re.I):
            before = low[match.start() - 1] if match.start() > 0 else " "
            after = low[match.end()] if match.end() < len(low) else " "
            if not _is_word_char(before) or before in _PREFIXES:
                if not _is_word_char(after):
                    # an Arabic verb with a pronoun suffix ("أرسله") still counts
                    if not after or not _ARABIC_LETTER.match(after) or len(word) >= 4:
                        return True
    return False


_PREFIXES = set("وفبلك")
_ARABIC_LETTER = re.compile(r"[\u0621-\u064a]")


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch == "_")


def _language(text: str) -> str:
    if re.search(r"[\u0600-\u06ff]", text or ""):
        return "ar"
    low = (text or "").lower()
    if re.search(r"[àâçéèêëîïôûùüÿœ]", low):
        return "fr"
    french = sum(1 for w in ("le", "la", "les", "un", "une", "des", "puis", "avec", "et",
                             "clique", "remplis", "ouvre", "champ", "prix")
                 if re.search(rf"(?<!\w){w}(?!\w)", low))
    return "fr" if french >= 2 else "en"


def _extract_selector(text: str) -> str:
    match = re.search(r"(?:the\s+)?(?:text|content|value|heading|title|price)\s+(?:of|in|from)\s+(?:the\s+)?"
                      r"(?:element|node)?\s*([#.][\w-]+|[\w-]+\.[\w-]+)", text, re.I)
    if match:
        return match.group(1)
    if "#" in text:
        match = re.search(r"(#[\w-]+)", text)
        if match:
            return match.group(1)
    hints = {
        "price": "price", "total": "total", "amount": "total", "title": "title",
        "heading": "title", "name": "name", "summary": "page", "content": "page", "text": "page",
        "prix": "price", "montant": "total", "titre": "title", "nom": "name", "résumé": "page",
        "السعر": "price", "الثمن": "price", "العنوان": "title", "الاسم": "name",
        "المحتوى": "page", "النص": "page",
    }
    for word, target in hints.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.I):
            return target
    return ""


def _extract_question(text: str) -> str:
    match = re.search(r"(?:what(?:'s| is)|whats|ما هو|ما هي|ماذا|quel(?:le)? est|quels? sont)\s+(.+?)\?*$", text, re.I)
    if match:
        return match.group(1).strip(" ?.")
    return ""


def _first_clause(text: str) -> str:
    parts = CLAUSE.split(text, maxsplit=1)
    return (parts[0] if parts else text).strip(" .'\"")


def _strip_quotes(text: str) -> str:
    return (text or "").strip().strip("\"'“”«»").strip()


def _clean(text: str) -> str:
    out = _strip_quotes(text).strip()
    while True:
        again = LEAD.sub("", out)
        if again == out:
            break
        out = again
    return out.strip(" .:\"")


def _submit_label(text: str) -> str:
    match = re.search(r"(?:press|click|hit|اضغط|انقر|clique)\s+(?:the\s+)?(.{2,40}?)\s*$", text, re.I)
    if match:
        return match.group(1).strip(" .'\"")
    return ""


# --------------------------------------------------------------------------
# Local reasoner (optional)
# --------------------------------------------------------------------------

class LocalReasoner:
    """Bridge to a model running on this computer."""

    SYSTEM = (
        "You are the planner inside SHADOW AI, an operator that drives a browser through the "
        "Chrome DevTools Protocol. Reply with ONE JSON object and nothing else: "
        '{"thought": string, "action": string, "target": string, "value": string}. '
        "action must be one of: ACTIONS. target is a CSS selector, a visible label, or a URL. "
        "Never propose running shell commands, reading credentials, or paying for anything. "
        'If the goal is done, use action "finish".'
    )

    def __init__(self, cfg: IntelligenceConfig):
        self.cfg = cfg
        self.last_raw = ""

    @property
    def available(self) -> bool:
        return self.cfg.provider != "heuristic"

    def plan(self, goal: str, page: Dict[str, Any], history: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        if not self.available:
            return None
        prompt = self._prompt(goal, page, history)
        raw = ""
        for attempt in range(max(1, int(self.cfg.retries))):
            try:
                raw = self._call(prompt if attempt == 0 else self._retry_prompt(prompt))
            except Exception:
                continue
            decision = self._parse(raw)
            if decision is not None:
                return decision
        self.last_raw = raw[:400]
        return None

    @staticmethod
    def _retry_prompt(prompt: str) -> str:
        return prompt + ("\n\nYour previous answer was not valid JSON. Reply with the object only, "
                         'for example {"thought": "...", "action": "click", "target": "#buy", "value": ""}')

    # -- transport ----------------------------------------------------
    def _call(self, prompt: str) -> str:
        if self.cfg.provider == "ollama":
            url = self.cfg.base_url.rstrip("/") + "/api/generate"
            body = {"model": self.cfg.model, "prompt": prompt, "stream": False,
                    "format": "json",     # Ollama's own constrained decoding
                    "options": {"temperature": self.cfg.temperature,
                                "num_predict": self.cfg.max_tokens,
                                "stop": ["\n\n"]}}
            data = self._post(url, body)
            return str(data.get("response", ""))
        if self.cfg.provider == "openai_compatible":
            url = self.cfg.base_url.rstrip("/") + "/v1/chat/completions"
            body = {"model": self.cfg.model, "temperature": self.cfg.temperature,
                    "max_tokens": self.cfg.max_tokens,
                    # Both keys are sent because servers differ on which one
                    # they honour; a server that knows neither ignores it.
                    "response_format": {"type": "json_object"},
                    "grammar": self._grammar(),
                    "messages": [{"role": "system", "content": self._system()},
                                 {"role": "user", "content": prompt}]}
            data = self._post(url, body)
            choices = data.get("choices") or []
            if not choices:
                return ""
            return str(((choices[0].get("message") or {}).get("content")) or "")
        return ""

    def _post(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as res:
            return json.loads(res.read().decode("utf-8", "replace"))

    def _system(self) -> str:
        """The system prompt, with the allow-list spliced in.

        Deliberately not `str.format`: the prompt is mostly JSON, and braces in
        a format string are placeholders — this exact bug made every model call
        raise KeyError and quietly fall back to the heuristics.
        """
        return self.SYSTEM.replace("ACTIONS", ", ".join(self.cfg.allow_actions))

    def _prompt(self, goal: str, page: Dict[str, Any], history: List[Dict[str, Any]]) -> str:
        system = self._system()
        elements = "\n".join(
            f"- {e.get('role') or e.get('tag')} \"{(e.get('name') or e.get('text') or '')[:50]}\" "
            f"selector={e.get('selector')}" for e in (page.get("elements") or [])[:40])
        done = "\n".join(f"- {h.get('action')} {h.get('target')}: {h.get('result')}"
                         for h in history[-8:]) or "- (nothing yet)"
        return (f"{system}\n\nGOAL: {goal}\n\nURL: {page.get('url')}\nTITLE: {page.get('title')}\n"
                f"PAGE TEXT (trimmed): {(page.get('text') or '')[:1500]}\n\nINTERACTIVE ELEMENTS:\n{elements}\n\n"
                f"STEPS SO FAR:\n{done}\n\nNext single step as JSON:")

    def _grammar(self) -> Dict[str, Any]:
        """A GBNF-ish schema a llama.cpp server can enforce token by token."""
        return {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "action": {"type": "string", "enum": list(self.cfg.allow_actions)},
                "target": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["action"],
        }

    def _parse(self, raw: str) -> Optional[Dict[str, str]]:
        """Whatever the model said, get one usable decision out of it.

        Small local models break JSON in the same four ways every time, so the
        repair is a fixed ladder rather than a hope: fenced blocks, a bare
        object, a single quoted pair, or the verb itself.
        """
        text = (raw or "").strip()
        if not text:
            return None
        candidate = None
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if fenced:
            candidate = fenced.group(1)
        if candidate is None:
            balanced = self._balanced_object(text)
            candidate = balanced or text
        data: Optional[Dict[str, Any]] = None
        try:
            data = json.loads(candidate)
        except ValueError:
            data = self._loose_json(candidate)
        if data is None:
            data = self._from_prose(text)
        if not isinstance(data, dict):
            return None
        action = str(data.get("action") or data.get("verb") or "").strip().lower()
        aliases = {"open": "goto", "navigate": "goto", "visit": "goto", "type": "type",
                   "fill": "type", "enter": "type", "tap": "click", "press": "press",
                   "read": "extract", "get": "extract", "done": "finish", "stop": "finish",
                   "choose": "select", "back": "back"}
        action = aliases.get(action, action)
        if action not in self.cfg.allow_actions:
            return None
        target = str(data.get("target") or data.get("selector") or data.get("element") or "")
        value = str(data.get("value") or data.get("text") or "")
        thought = str(data.get("thought") or data.get("reason") or "")
        return {"action": action, "target": target[:200].strip(),
                "value": value[:400], "thought": thought[:300].strip()}

    @staticmethod
    def _balanced_object(text: str) -> Optional[str]:
        """The first complete {...} in the text, brackets counted properly, so a
        pretty-printed answer with braces inside strings still parses."""
        start = text.find("{")
        if start < 0:
            return None
        depth, in_string, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    @staticmethod
    def _loose_json(text: str) -> Optional[Dict[str, Any]]:
        """Single quotes, trailing commas, unquoted keys: the usual damage."""
        fixed = re.sub(r"(?<=[:\[,])\s*'([^']*)'", lambda m: ' "' + m.group(1) + '"', text)
        fixed = re.sub(r"'([^']*)'\s*:", lambda m: '"' + m.group(1) + '":', fixed)
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        fixed = re.sub(r"([{,]\s*)([A-Za-z_][\w]*)\s*:", r'\1"\2":', fixed)
        try:
            data = json.loads(fixed)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    HEDGE = re.compile(r"(?i)\b(not sure|unsure|unclear|don't know|do not know|maybe|"
                       r"perhaps|cannot|can't|unable|sorry|لا أعرف|ربما|peut-être|pas sûr)")

    def _from_prose(self, text: str) -> Optional[Dict[str, Any]]:
        """Last resort: read a decision out of a sentence.

        A hedged answer ("maybe click something?") is not a decision: refusing
        it makes the caller retry instead of acting on a coin flip.
        """
        if self.HEDGE.search(text):
            return None
        lowered = text.lower()
        for verb, canonical in (("click", "click"), ("type", "type"), ("fill", "type"),
                                ("open", "goto"), ("go to", "goto"), ("navigate", "goto"),
                                ("press", "press"), ("scroll", "scroll"), ("finish", "finish"),
                                ("done", "finish"), ("extract", "extract"), ("read", "extract")):
            if verb in lowered:
                quoted = QUOTED.search(text)
                return {"action": canonical, "target": quoted.group(1) if quoted else "",
                        "value": "", "thought": text[:200]}
        return None


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class IntelligenceEngine(Engine):
    name = "intel"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.parser = GoalParser()
        self.reasoner = LocalReasoner(ctx.cfg.intelligence)
        self.model_calls = 0
        self.model_failures = 0

    def understand(self, goal: str) -> Interpretation:
        self.note(goal=goal[:120])
        interp = self.parser.parse(goal)
        self.emit("understood", language=interp.language,
                  intents=[i.kind for i in interp.intents])
        return interp

    def reason(self, goal: str, page: Dict[str, Any], history: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """Ask the local model, if one is configured. Never raises."""
        if not self.reasoner.available:
            return None
        self.model_calls += 1
        step = None
        try:
            step = self.reasoner.plan(goal, page, history)
        except Exception as exc:  # noqa: BLE001 - the model is optional
            self.model_failures += 1
            self.fail(exc)
        if step:
            self.emit("proposed", action=step["action"], target=step["target"][:80])
        return step

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info.update({"provider": self.ctx.cfg.intelligence.provider,
                     "local_model": self.reasoner.available,
                     "model_calls": self.model_calls,
                     "model_failures": self.model_failures})
        return info

    def probe_model(self) -> Dict[str, Any]:
        """Is the configured local model reachable? Safe to call anytime."""
        if not self.reasoner.available:
            return {"reachable": False, "reason": "provider is heuristic"}
        try:
            step = self.reasoner.plan("say nothing", {"url": "about:blank", "title": "", "elements": [], "text": ""}, [])
            return {"reachable": True, "reply": bool(step)}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "reason": str(exc)[:200]}
