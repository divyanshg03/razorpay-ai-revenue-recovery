"""Message composer: local Ollama writes the words, code supplies the facts and the link.

## Boundaries, each one set by something the bake-off actually did

- **The model never sees the payment link.** All three models invented placeholder URLs when
  given the chance. The link is appended by code after generation, and the gate rejects any
  URL the model produced on its own.
- **The model is given exactly one amount and no dates.** `ministral-3` hallucinated
  *"failed on 12/05/2024"* into a customer message. The gate rejects any date-like token and
  any rupee figure that is not the one supplied.
- **Every output goes through the copy gate, and every rejection falls back to a template.**
  The template is itself gate-checked in the test suite, so the fallback path cannot be the
  thing that ships a violation.
- **The LLM is never on the decision path.** By the time this module is called, the state
  machine has already committed to sending something on this channel. Wording is the only
  thing left to decide, and it is the only thing the model is asked for.

## No hosted API, no key

`llama3.1:8b` on a local Ollama over plain HTTP. Nothing leaves the machine. Hard rule 6.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..models import Actionability, Channel
from .copy_gate import SMS_LIMIT, Facts, GateResult, Verdict, check

OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "llama3.1:8b"

#: Leave room for the link and a space. Links are ~30 chars.
BODY_LIMIT = 118

_SYSTEM = (
    "You write one short transactional SMS for an Indian merchant about a recurring payment "
    "that did not go through. Rules you must never break: no discounts, offers, cashback, "
    "coupons or rewards. No urgency, deadlines, scarcity or pressure. No threats, penalties, "
    "legal language or warnings. No upselling. Do not invent any date, amount or fact. "
    "Do not include any link or placeholder; the system appends the payment link. "
    f"Plain, polite, factual. Under {BODY_LIMIT} characters. "
    "Output ONLY the message text: no quotation marks, no preamble, no sign-off."
)

#: Deterministic fallbacks, one per actionability. Gate-checked by tests.
TEMPLATES: dict[Actionability, str] = {
    Actionability.NEEDS_FUNDS:
        "Your {merchant}payment of Rs {amount} did not go through. You can retry here: {link}",
    Actionability.NEEDS_NEW_INSTRUMENT:
        "Your {merchant}payment of Rs {amount} failed because the saved payment method is no "
        "longer valid. Update it here: {link}",
    Actionability.NEEDS_CUSTOMER_ACTION:
        "Your {merchant}payment of Rs {amount} was not completed. You can complete it here: {link}",
    Actionability.RETRY_LATER:
        "Your {merchant}payment of Rs {amount} could not be processed. You can retry here: {link}",
    Actionability.DO_NOT_CONTACT:
        "Your {merchant}payment of Rs {amount} did not go through. Details here: {link}",
}

#: Plain-language cause given to the model. No error codes: they are not customer language.
CAUSE_TEXT: dict[Actionability, str] = {
    Actionability.NEEDS_FUNDS: "the bank declined it because the balance was not sufficient",
    Actionability.NEEDS_NEW_INSTRUMENT: "the saved card or mandate is no longer valid",
    Actionability.NEEDS_CUSTOMER_ACTION: "the payment was not completed by the customer",
    Actionability.RETRY_LATER: "there was a temporary problem processing it",
    Actionability.DO_NOT_CONTACT: "it did not go through",
}


@dataclass
class ComposedMessage:
    """`gate` ALWAYS describes `text` — the message actually being sent.

    An earlier version returned the template as `text` while carrying the *rejected LLM
    candidate's* verdict in `gate`, so `gate.ok` was False for a message that was perfectly
    fine to send. Any caller gating on `msg.gate.ok` would have refused to send valid
    fallback copy — silently dropping the customer contact rather than degrading to a
    template, which is the opposite of the intended failure mode.

    The rejection is not lost: it moves to `llm_gate`, which is the copy gate's evidence
    that it caught something, and is written to the audit ledger.
    """

    text: str
    source: str            # "llm" | "template"
    gate: GateResult       # verdict for `text`, whatever `text` ended up being
    model: str | None
    llm_output: str | None = None   # what the model wrote, pre-gate, for the audit trail
    llm_seconds: float | None = None
    #: Set only when the model's words were rejected or refused and a template was used
    #: instead. This is what proves the gate fired.
    llm_gate: GateResult | None = None

    @property
    def template_ref(self) -> str:
        return f"{self.source}:{self.model or 'static'}"

    @property
    def gate_rejected_llm(self) -> bool:
        return self.llm_gate is not None


def template(facts: Facts, actionability: Actionability) -> str:
    merchant = f"{facts.merchant} " if facts.merchant else ""
    return TEMPLATES[actionability].format(merchant=merchant, amount=facts.amount_rupees_text,
                                          link=facts.link)


def _strip(text: str) -> str:
    text = text.strip().strip('"').strip("'").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _ask(model: str, actionability: Actionability, facts: Facts, timeout: float) -> tuple[str | None, float]:
    merchant = facts.merchant or "the merchant"
    user = (f"Merchant: {merchant}. Amount: Rs {facts.amount_rupees_text}. "
            f"What happened: {CAUSE_TEXT[actionability]}. Write the SMS body.")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        "stream": False, "think": False,
        "options": {"temperature": 0, "num_predict": 80},
    }
    req = urllib.request.Request(OLLAMA_CHAT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        return _strip(out["message"]["content"]), time.perf_counter() - t
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, OSError):
        return None, time.perf_counter() - t


def compose(facts: Facts, actionability: Actionability, channel: Channel = Channel.SMS_SERVICE,
            use_llm: bool = True, model: str = DEFAULT_MODEL, timeout: float = 25.0) -> ComposedMessage:
    """Return a sendable message. LLM if it clears the gate; the template otherwise.

    The system must degrade to templates and keep collecting money, never stall on the LLM.
    """
    if use_llm:
        raw, secs = _ask(model, actionability, facts, timeout)
        if raw:
            candidate = f"{raw} {facts.link}"
            verdict = check(candidate, facts, limit=SMS_LIMIT)
            if verdict.ok:
                return ComposedMessage(candidate, "llm", verdict, model, raw, secs)
            # Rejected or refused: fall back to the template. `gate` describes the TEMPLATE
            # (what we are actually sending); the model's rejection is preserved separately
            # in `llm_gate` so the audit trail still shows why its words were not used.
            fallback = template(facts, actionability)
            return ComposedMessage(fallback, "template", check(fallback, facts), model,
                                   raw, secs, llm_gate=verdict)
    text = template(facts, actionability)
    return ComposedMessage(text, "template", check(text, facts), None)


def warm(model: str = DEFAULT_MODEL, timeout: float = 120.0) -> float:
    """Load the model into VRAM before a demo. Cold start was measured at 36s; a stall of
    that length in front of a panel is the risk this exists to remove."""
    body = {"model": model, "messages": [{"role": "user", "content": "ok"}],
            "stream": False, "keep_alive": "30m", "options": {"num_predict": 1}}
    req = urllib.request.Request(OLLAMA_CHAT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return time.perf_counter() - t
