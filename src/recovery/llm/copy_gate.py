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
    # Widened 4 Sept 2026 after a review found 16 of 17 realistic non-compliant messages
    # passing. The v1 lexicon matched the words a marketer would use; it missed the words a
    # collections agent actually writes. "Pay Rs 499 now and we will waive this month's
    # charge" is the TRAI mixed-content trigger in one sentence and it sailed through.
    "discount_or_offer": re.compile(
        r"\b(discount|cashback|cash back|coupon|promo\s?code|% ?off|percent off|percent back|"
        r"rupees off|waiv(e|er|ed|ing)|writ(e|ing) off|write-off|settle for|settlement offer|"
        r"reduc(e|ed|tion) (your|the) (amount|due|balance)|no (charge|fee)|"
        r"offer|bonus|reward|incentive|free month|extra data|upgrade|voucher|deal)\b", re.I),
    "false_urgency": re.compile(
        r"\b(hurry|act (now|fast|quickly|immediately)|right away|immediately|urgent(ly)?|"
        r"last chance|final (notice|warning|reminder)|expir(e|es|ing|y)|limited[- ]time|"
        r"don'?t delay|before it'?s too late|asap|today only|"
        r"(with)?in the next \d+\s*(hours?|minutes?|days?)|within \d+\s*(hours?|minutes?|days?)|"
        r"before (midnight|end of day|eod|today|tomorrow)|"
        r"only \d+\s*(hours?|days?|minutes?)\s*(left|remain)|"
        r"\d+\s*(hours?|days?)\s*(left|remaining|to go))\b", re.I),
    "scarcity": re.compile(
        r"\b(only a few|running out|while stocks? last|exclusive|limited (slots?|seats?|spots?))\b",
        re.I),
    "threat_or_shaming": re.compile(
        r"\b(legal action|legal notice|consequences|will be reported|report(ed)? to|"
        r"credit bureau|credit report|recovery agent|collections? agenc(y|ies)|"
        r"hand(ed)? (over )?to collections|account will be (closed|frozen|suspended)|"
        r"default(ed|er|ers|ing)?|embarrass(ing|ment|ed)?|shame(ful)?|irresponsible|"
        r"penalt(y|ies)|late fee|fine|suspen(d|ded|sion)|terminat(e|ed|ion)|blacklist|"
        r"blocked|disconnect(ed|ion)?|cancel(led|lation)|lose access|police|court|"
        r"credit score|cibil)\b", re.I),
}

REFUSAL = re.compile(
    r"^\s*(i (cannot|can'?t|won'?t|am unable|'m unable|will not)|sorry[,.]? (i|but)|"
    r"unfortunately[,.]? i|as an ai)", re.I)
#: The model leaking its own scaffolding into the message body. A composer prompt that asks
#: for JSON elsewhere in the system means a model will sometimes answer this one in JSON too,
#: and `[{"intent":"promise_to_pay"}] https://rzp.io/...` passed every other rule here: it has
#: the right link, no banned word, and no fabricated number. Sending it to a customer would be
#: the most obviously broken thing this system could do.
MACHINE_OUTPUT = re.compile(
    r'^\s*[\[{]'            # starts with a JSON array or object
    r'|"[a-z_]+"\s*:'       # a quoted key followed by a colon
    r'|^\s*```'             # a fenced code block
    r'|\}\s*\]?\s*$',       # ends by closing an object or array
    re.I | re.M)

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

    if MACHINE_OUTPUT.search(text):
        categories["machine_output"] = sorted({m.group(0).strip()
                                               for m in MACHINE_OUTPUT.finditer(text)})[:4]
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
