"""Data Engine — extract, store, and generate.

Extraction reads the page through the DOM reader (tables, lists, fields,
links). Storage appends to JSONL or CSV under the state directory, one file
per job, so a long scrape survives a crash. Generation is the form-filling
half: a coherent person (name, address, email, phone, card) derived
deterministically from a seed, so a profile keeps its own identity across
runs while two profiles never share one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List

from .base import Engine

FIRST = ["Amina", "Youssef", "Leila", "Omar", "Sofia", "Karim", "Nadia", "Hugo", "Camille",
         "Mehdi", "Ines", "Rami", "Lina", "Adam", "Sara", "Thomas", "Alice", "Noah"]
LAST = ["Ben Salah", "Trabelsi", "Haddad", "Mansour", "Dupont", "Martin", "Bernard", "Gharbi",
        "Moreau", "Bouazizi", "Laurent", "Chaabane", "Rousseau", "Amari"]
STREETS = ["Rue de Marseille", "Avenue Habib Bourguiba", "Rue du Lac", "Boulevard Yves Couzy",
           "Avenue de la Liberté", "Rue Ibn Khaldoun", "Chemin des Oliviers"]
CITIES = [("Tunis", "1001", "TN"), ("Sfax", "3000", "TN"), ("Sousse", "4000", "TN"),
          ("Paris", "75011", "FR"), ("Lyon", "69003", "FR"), ("Berlin", "10115", "DE")]
WORDS = ["lamp", "desk", "chair", "table", "vase", "rug", "shelf", "mirror", "clock", "bowl"]


@dataclass
class Person:
    first_name: str
    last_name: str
    email: str
    phone: str
    street: str
    city: str
    postal: str
    country: str
    birth_date: str
    card_number: str = ""
    card_expiry: str = ""
    card_cvv: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DataEngine(Engine):
    name = "data"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.rows_written = 0

    # -- extraction ---------------------------------------------------
    def table(self, probe, selector: str = "table") -> Dict[str, Any]:
        header = [h for h in probe.texts(f"{selector} th") if h]
        rows = probe.snapshot(f"{selector} tr", limit=300)
        grouped: Dict[str, List[str]] = {}
        for row in rows:
            key = (row.get("selector") or "").rsplit(" ", 1)[0]
            grouped.setdefault(key, []).append(row.get("text", ""))
        body = [cells for cells in grouped.values() if cells and cells != header]
        return {"header": header, "rows": body, "count": len(body)}

    def list_items(self, probe, selector: str) -> List[Dict[str, Any]]:
        items = probe.snapshot(selector, limit=300)
        return [{"text": n.get("text", ""), "href": n.get("href", ""),
                 "selector": n.get("selector", "")} for n in items if n.get("visible")]

    def fields(self, probe) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for node in probe.snapshot("input,textarea,select", limit=200):
            ident = node.get("id") or node.get("selector")
            out[node.get("name") or ident] = node.get("value", "")
        return out

    def links(self, probe) -> List[Dict[str, str]]:
        return probe.links()

    def prices(self, probe, selector: str = "") -> List[Dict[str, Any]]:
        import re
        nodes = probe.snapshot(selector or "[class*=price],.price,td", limit=200)
        out = []
        for node in nodes:
            for amount in re.findall(r"(?:[$€£]|USD|EUR|TND|DT)\s?\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s?(?:USD|EUR|TND|DT)",
                                     node.get("text", "")):
                out.append({"text": node.get("text", ""), "amount": amount})
                break
        return out

    # -- storage ------------------------------------------------------
    def store(self, job: str, rows: Iterable[Dict[str, Any]], fmt: str = "jsonl") -> str:
        path = self.ctx.cfg.data_path() / f"{_safe(job)}.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = list(rows)
        if fmt == "csv":
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                if rows:
                    writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)
        else:
            with open(path, "a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self.rows_written += len(rows)
        self.note(job=job, rows=len(rows))
        self.emit("stored", job=job, rows=len(rows), path=str(path))
        return str(path)

    def read(self, job: str, fmt: str = "jsonl") -> List[Dict[str, Any]]:
        path = self.ctx.cfg.data_path() / f"{_safe(job)}.{fmt}"
        if not path.exists():
            return []
        if fmt == "csv":
            with open(path, encoding="utf-8-sig") as fh:
                return list(csv.DictReader(fh))
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    # -- generation ---------------------------------------------------
    def person(self, seed: str = "", card: bool = False) -> Person:
        rng = random.Random(_seed(seed or self.ctx.profile_id or "shadow"))
        first = rng.choice(FIRST)
        last = rng.choice(LAST)
        city, postal, country = rng.choice(CITIES)
        slug = (first + "." + last.split()[0]).lower().replace(" ", "")
        person = Person(
            first_name=first, last_name=last,
            email=f"{slug}{rng.randint(11, 99)}@example.com",
            phone=f"+{216 if country == 'TN' else (33 if country == 'FR' else 49)} "
                  f"{rng.randint(20, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)}",
            street=f"{rng.randint(1, 180)} {rng.choice(STREETS)}",
            city=city, postal=postal, country=country,
            birth_date=f"{rng.randint(1975, 2003)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        )
        if card:
            person.card_number = "4" + "".join(str(rng.randint(0, 9)) for _ in range(14))
            person.card_expiry = f"{rng.randint(1, 12):02d}/{rng.randint(27, 32)}"
            person.card_cvv = f"{rng.randint(100, 999)}"
        self.note(kind="person")
        return person

    def product_names(self, count: int = 5, seed: str = "") -> List[str]:
        rng = random.Random(_seed(seed or "names"))
        return [f"{rng.choice(['Linen','Oak','Cane','Brass','Stone','Wool'])} {rng.choice(WORDS).title()}"
                for _ in range(count)]

    def query(self, count: int = 5, seed: str = "") -> List[str]:
        names = self.product_names(count, seed)
        return [n.lower() for n in names]

    # -- forms --------------------------------------------------------
    def fill_person(self, probe, person: Person) -> Dict[str, str]:
        """Type a person into whatever fields the page actually has."""
        from .input import KeyboardEngine  # local import: no engine imports engines at module load
        keyboard: KeyboardEngine = self.ctx.registry.get("keyboard")
        mapping = {
            "email": person.email, "user": person.email, "name": person.first_name,
            "first": person.first_name, "last": person.last_name, "fullname": f"{person.first_name} {person.last_name}",
            "phone": person.phone, "tel": person.phone, "address": person.street,
            "street": person.street, "city": person.city, "zip": person.postal,
            "postal": person.postal, "country": person.country, "birth": person.birth_date,
        }
        filled: Dict[str, str] = {}
        for node in probe.snapshot("input,textarea", limit=120):
            if not node.get("visible") or node.get("type") in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            hay = " ".join([node.get("name", ""), node.get("id", ""),
                            (node.get("attrs") or {}).get("name", ""),
                            (node.get("attrs") or {}).get("placeholder", ""),
                            node.get("type", "")]).lower()
            for key, value in mapping.items():
                if key in hay:
                    keyboard.fill_field(probe, node, value)
                    filled[node.get("id") or node.get("selector")] = value
                    break
        self.note(kind="fill_person", fields=len(filled))
        return filled

    def health(self) -> Dict[str, Any]:
        info = super().health()
        info["rows_written"] = self.rows_written
        return info


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:12], 16)


def _safe(name: str) -> str:
    return "".join(c for c in (name or "job") if c.isalnum() or c in "-_.")[:60] or "job"
