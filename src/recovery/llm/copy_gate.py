"""The copy gate. Every LLM-composed message passes through here or it is not sent.

## Why this is a regulatory control and not a style check

Under TRAI's mixed-content rule (TCCCPR, Second Amendment 2025), promotional content inside a
service message makes the **whole message** promotional — and a promotional message inherits
explicit-consent, DND-scrubbing and 09:00–21:00 obligations that a failed-payment notice does
not otherwise carry. One "10% off" turns a compliant service message into a non-compliant
promotional one. Separately, fabricated urgency and repeated nudging are two of the thirteen
named patterns in the CCPA Dark Patterns Guidelines 2023.

So the gate blocks, by regulatory category: discounts/offers, false urgency, scarcity,
threats and confirm-shaming — plus fabricated facts (amounts and dates not in the supplied
facts), placeholder tokens, a missing or wrong link, and over-length copy.

## What the bake-off taught, and what changed here because of it

- **Refusals are a separate verdict.** The v0 lexicon flagged llama3.1's *"I cannot write a
  message that pushes the customer to pay right away"* as a violation because it contained
  "right away". A refusal produces nothing sendable, but it is not promotional drift; it gets
  `REFUSED`, so the composer falls back to a template instead of logging a false breach.
- **`suspension` was missed.** `\\bsuspend(ed)?\\b` never matched *"avoid service suspension"*.
  Fixed by matching the stem.
- **Every match is recorded**, so a rejection is auditable rather than a boolean to be
  trusted. The canonical test fixture is `ministral-3`'s real bake-off output — a genuine
  model-generated violation, not a hand-written strawman.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field

SMS_LIMIT = 160


class Verdict(str, enum.Enum):
    PASS = "pass"
    REJECTED = "rejected"
    REFUSED = "refused"       # model declined to write anything; not a compliance breach


@dataclass(frozen=True)
class Facts:
    """The only facts the message may contain. Anything numeric or date-like outside this
    set is a fabrication — the bake-off caught a model inventing "failed on 12/05/2024"."""

    amount_paise: int
    link: str
    merchant: str = ""

    @property
    def amount_rupees_text(self) -> str:
        rupees = self.amount_paise / 100
        return f"{rupees:,.0f}" if rupees == int(rupees) else f"{rupees:,.2f}"


@dataclass
class GateResult:
    verdict: Verdict
    categories: dict[str, list[str]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.PASS


# Grouped by the regulatory category each maps to. Stems rather than whole words where the
# v0 lexicon missed inflections.
BANNED: dict[str, re.Pattern] = {
    "discount_or_offer": re.compile(
        r"\b(discount|cashback|cash back|coupon|promo\s?code|% ?off|percent off|rupees off|"
        r"offer|bonus|reward|incentive|free month|extra data|upgrade|voucher|deal)\b", re.I),
    "false_urgency": re.compile(
        r"\b(hurry|act now|act fast|right away|immediately|urgent(ly)?|last chance|"
        r"final (notice|warning|reminder)|expir(e|es|ing|y)|limited[- ]time|"
        r"don'?t delay|before it'?s too late|asap|today only|within \d+ (hours?|minutes?))\b", re.I),
    "scarcity": re.compile(
        r"\b(only a few|running out|while stocks? last|exclusive|limited (slots?|seats?|spots?))\b",
        re.I),
    "threat_or_shaming": re.compile(
        r"\b(legal action|legal notice|consequences|will be reported|report(ed)? to|"
        r"penalt(y|ies)|late fee|fine|suspen(d|ded|sion)|terminat(e|ed|ion)|blacklist|"
        r"blocked|disconnect(ed|ion)?|cancel(led|lation)|lose access|police|court|"
        r"credit score|cibil)\b", re.I),
}

REFUSAL = re.compile(
    r"^\s*(i (cannot|can'?t|won'?t|am unable|'m unable|will not)|sorry[,.]? (i|but)|"
    r"unfortunately[,.]? i|as an ai)", re.I)
PLACEHOLDER = re.compile(r"\[(date|link|name|amount|url|customer|merchant|x+)\]|\{\{.*?\}\}|<[a-z_ ]+>", re.I)
# Any date-like token. Dates in a payment notice are facts the model does not have.
DATE_LIKE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(st|nd|rd|th)?)\b", re.I)
MONEY = re.compile(r"(?:rs\.?|₹|inr)\s?([\d,]+(?:\.\d{1,2})?)", re.I)
URL = re.compile(r"https?://\S+", re.I)


def _money_values(text: str) -> set[float]:
    out = set()
    for m in MONEY.finditer(text):
        try:
            out.add(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return out


def check(text: str, facts: Facts, limit: int = SMS_LIMIT) -> GateResult:
    """Decide whether `text` may be sent. Never mutates; the composer owns fallbacks."""
    if not text or not text.strip():
        return GateResult(Verdict.REFUSED, reasons=["empty output"])
    if REFUSAL.match(text):
        return GateResult(Verdict.REFUSED, reasons=["model refused to compose"])

    categories: dict[str, list[str]] = {}
    reasons: list[str] = []

    for name, pattern in BANNED.items():
        hits = sorted({m.group(0).lower() for m in pattern.finditer(text)})
        if hits:
            categories[name] = hits

    if PLACEHOLDER.search(text):
        categories["placeholder"] = sorted({m.group(0) for m in PLACEHOLDER.finditer(text)})
    if DATE_LIKE.search(text):
        categories["fabricated_date"] = sorted({m.group(0) for m in DATE_LIKE.finditer(text)})

    stated = _money_values(text)
    allowed = {facts.amount_paise / 100}
    foreign = {v for v in stated if v not in allowed}
    if foreign:
        categories["fabricated_amount"] = [f"{v:g}" for v in sorted(foreign)]

    urls = URL.findall(text)
    if facts.link and facts.link not in text:
        reasons.append("payment link missing")
    if any(u.rstrip(".,") != facts.link for u in urls):
        categories["foreign_url"] = [u for u in urls if u.rstrip(".,") != facts.link]

    if len(text) > limit:
        reasons.append(f"{len(text)} chars exceeds {limit}")

    if categories or reasons:
        return GateResult(Verdict.REJECTED, categories=categories, reasons=reasons)
    return GateResult(Verdict.PASS)
