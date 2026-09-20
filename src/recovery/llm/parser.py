"""Inbound reply parser: the LLM reads, the code decides.

## Division of labour, fixed by the bake-off

The model is asked for two things only: an **intent** label and the **date phrase** the
customer used, verbatim. It is never asked for a date.

Every model in the bake-off got dates wrong, and `qwen3` resolved *"cant pay till salary comes
on the 5th"* to **2026-08-05 — a date in the past**. A deterministic engine that schedules a
follow-up from a model-supplied date would have silenced itself for a promise that had
already expired. So `resolve_date()` is a pure function with its own tests, and the phrase is
the only thing that crosses the boundary.

## Code-level overrides that outrank the model

Three intents are too consequential to delegate:

- **opt-out** — statutory stop under DPDP s.7(a); a missed one is a legal exposure
- **dispute** — routes to a human and freezes contact
- **bereavement / hardship** — the bake-off saw `ministral-3` label *"my father passed away
  last week"* as `promise_to_pay`. That is the one misclassification with no acceptable
  frequency, so keyword rules catch it in code regardless of what the model says.

They are evaluated in that order, and the order is itself a rule: a statutory stop outranks a
policy pause. *"My father passed away. Stop messaging me."* carries both signals, and while
hardship won, an explicit objection became a seven-day timer that resumed contact on day 8.

## The model may stop us. It may never start us.

Where the keyword list is silent the model's label is used, with one asymmetry. If the model
reports `opt_out` or `dispute`, that is honoured as a stop — it does not have to agree with an
English regex first. The reverse is never true: no model output can lift a stop, start contact,
or supply a date. Until 20 Sept 2026 a model-detected opt-out was discarded as unreliable,
which sounds cautious and is the opposite: *"please cease all communication with me"* and
*"yeh messages band karo"* were both read correctly by the model, thrown away by this file, and
followed by an SMS and a human call. A keyword list of English phrases cannot cover the way
people actually write, so the model's reading is the only thing standing behind those replies.
"""

from __future__ import annotations

import calendar
import datetime as dt
import enum
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "llama3.1:8b"


class Intent(str, enum.Enum):
    PROMISE_TO_PAY = "promise_to_pay"
    DISPUTE = "dispute"
    OPT_OUT = "opt_out"
    HARDSHIP = "hardship"
    OTHER = "other"


@dataclass(frozen=True)
class ParsedReply:
    intent: Intent
    promised_date: dt.date | None
    date_phrase: str | None
    source: str            # "override" | "llm_stop" | "llm" | "fallback"
    model: str | None = None


# ---- overrides -----------------------------------------------------------------------

_OPT_OUT = re.compile(
    r"\b(stop|unsubscribe|opt ?out|do not (message|contact|text|call)|don'?t (message|contact|text|call)|"
    r"remove (me|my number)|no more messages|leave me alone|"
    # Romanised Hinglish, because that is what a customer actually types into WhatsApp. The
    # model reads these when it is running, but the keyword list is the floor: it holds when
    # Ollama is down, and llama3.1:8b labels "mujhe message mat bhejo" as `other` anyway, so
    # relying on the model alone left the most common Indian phrasing unstopped. This is a
    # list, not a design: it covers the frequent forms and no more, and every reply it misses
    # falls to the model, then to OTHER.
    # The `\w*` tails matter: "mat bhej" against "mat bhejo" matched the letters and then
    # failed the closing \b, so the first version of this list stopped nothing at all.
    r"band kar\w*|mat bhej\w*|mat kar\w*|mat call\w*|message band|msg band|"
    r"pareshan (mat|na)\w*|nahin? chahiye|hata (do|de|dijiye))\b", re.I)
_DISPUTE = re.compile(
    r"\b(already paid|paid (this|it|that) (yesterday|already|last)|never (signed|subscribed|ordered)|"
    r"who (is|are) (this|you)|not my (payment|account|subscription)|didn'?t (sign|subscribe|order)|"
    r"fraud|scam|wrong (person|number)|dispute|chargeback|check your records)\b", re.I)
_HARDSHIP = re.compile(
    r"\b(passed away|died|death|funeral|bereave|hospital|surgery|accident|lost my job|laid off|"
    r"unemployed|medical emergency|cancer|icu|critical condition)\b", re.I)


#: A hardship reply carries a date only when the customer is naming a time to come BACK.
#: Without this clause the entire reply went to `resolve_date`, which reads any one or two
#: digit number as a day of the month: *"my mother died 2 days ago"* scheduled the callback
#: for the 2nd, which was the following day. A number is not a date unless the sentence is
#: pointing forward, so the date is resolved from the callback clause alone and a reply with
#: no such clause falls back to the policy pause.
_CALLBACK = re.compile(
    r"\b(?:after|until|call me|contact me|message me|text me|ring me|try me|try again|"
    r"reach me|come back|get back|revert)\b.{0,40}", re.I | re.S)


def override_intent(text: str) -> Intent | None:
    """Code outranks the model on the three intents that carry legal or human weight.

    ORDER IS A RULE HERE, not an accident of writing. Opt-out and dispute are tested before
    hardship, because a statutory stop must outrank a policy pause. "My father passed away.
    Stop messaging me." matches both lists, and with hardship first the objection was
    recorded as hardship, paused for seven days, and then contact resumed - a human call on
    day 12. The bereavement wording is the more emotive of the two signals, which is exactly
    why it must not also be the more permissive one.
    """
    if _OPT_OUT.search(text):
        return Intent.OPT_OUT
    if _DISPUTE.search(text):
        return Intent.DISPUTE
    if _HARDSHIP.search(text):
        return Intent.HARDSHIP
    return None


# ---- deterministic date resolution ---------------------------------------------------

_WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
_WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_abbr)})


def _add_months(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return dt.date(y, m, day)


def resolve_date(phrase: str | None, today: dt.date) -> dt.date | None:
    """Turn a customer's date phrase into a date that is NEVER in the past.

    Pure, tested, and the only place a promise-to-pay date is ever produced. Unknown phrases
    resolve to None — the engine then treats the reply as a promise without a date, which
    means "wait a few days", never "wait until a date the model made up".
    """
    if not phrase:
        return None
    p = phrase.strip().lower()

    if re.search(r"\b(today|tonight|this evening|now|right now)\b", p):
        return today
    if re.search(r"\btomorrow\b", p):
        return today + dt.timedelta(days=1)
    if re.search(r"\bday after tomorrow\b", p):
        return today + dt.timedelta(days=2)

    m = re.search(r"\b(?:in|after) (\d+) days?\b", p)
    if m:
        return today + dt.timedelta(days=int(m.group(1)))
    m = re.search(r"\b(?:in|after) (a|one|\d+) weeks?\b", p)
    if m:
        n = 1 if m.group(1) in ("a", "one") else int(m.group(1))
        return today + dt.timedelta(days=7 * n)
    if re.search(r"\bnext week\b", p):
        return today + dt.timedelta(days=7)
    if re.search(r"\b(end of (the )?month|month[- ]end)\b", p):
        return dt.date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    if re.search(r"\bnext month\b", p):
        return _add_months(today, 1)

    # "next monday" / "monday" -> the next occurrence strictly after today.
    m = re.search(r"\b(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                  r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b", p)
    if m:
        key = m.group(2)
        key = {"tues": "tue", "thur": "thu", "thurs": "thu"}.get(key, key)
        target = _WEEKDAYS[key]
        ahead = (target - today.weekday()) % 7
        ahead = 7 if ahead == 0 else ahead
        return today + dt.timedelta(days=ahead)

    # "the 5th", "on 5th", "5th of next month" -> next occurrence of that day-of-month,
    # rolling forward if it has already passed. A resolved date is never in the past.
    m = re.search(r"\b(?:on |the )?(\d{1,2})(?:st|nd|rd|th)?\b", p)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            months_ahead = 1 if "next month" in p else 0
            candidate_base = _add_months(today, months_ahead)
            last = calendar.monthrange(candidate_base.year, candidate_base.month)[1]
            candidate = dt.date(candidate_base.year, candidate_base.month, min(day, last))
            if candidate <= today:
                nxt = _add_months(candidate_base, 1)
                last = calendar.monthrange(nxt.year, nxt.month)[1]
                candidate = dt.date(nxt.year, nxt.month, min(day, last))
            return candidate

    return None


# ---- the LLM leg ---------------------------------------------------------------------

_SYSTEM = (
    "You read a customer's reply to a failed-payment message. Return ONLY JSON with keys "
    '"intent" and "date_phrase". intent is exactly one of "promise_to_pay", "dispute", '
    '"opt_out", "other". date_phrase is the customer\'s OWN words about when they will pay, '
    'copied verbatim (for example "the 5th", "next monday", "tonight"), or null if they gave '
    "none. Do NOT convert it to a date. Do NOT add anything else."
)


def _ask_llm(reply: str, model: str, timeout: float) -> dict | None:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": f'Customer replied: "{reply}"'}],
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": 0, "num_predict": 120},
    }
    req = urllib.request.Request(OLLAMA_CHAT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        return json.loads(out["message"]["content"])
    # TypeError covers `content: null`, which json.loads rejects with a TypeError rather than
    # a JSONDecodeError and so escaped the original tuple. Every failure here means "no usable
    # model answer", and every one of them must land on the keyword fallback rather than
    # ending the batch.
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, TypeError,
            OSError):
        return None


def _fallback_intent(text: str) -> tuple[Intent, str | None]:
    """Keyword-only path when the LLM is unavailable. Conservative: unknown -> OTHER."""
    t = text.lower()
    if re.search(r"\b(will pay|pay (tonight|tomorrow|on|by|next)|paying|salary|after|"
                 r"once i get|can pay|sending|transfer)\b", t):
        return Intent.PROMISE_TO_PAY, text
    return Intent.OTHER, None


def parse_reply(reply: str, today: dt.date, model: str = DEFAULT_MODEL,
                timeout: float = 20.0, use_llm: bool = True) -> ParsedReply:
    """Classify a reply. Overrides first, model second, keywords last; dates always in code."""
    forced = override_intent(reply)
    if forced is not None:
        # A hardship reply often names a date when contact would be welcome again - "call me
        # after the 15th", "try me next month". Honouring that is the customer exercising
        # control over their own file, not us overriding their hardship: the default remains
        # an indefinite stop, and only a date THEY supplied lifts it, never earlier.
        #
        # Opt-out and dispute get no such treatment. An objection under DPDP s.7(a) is not a
        # scheduling preference, and a dispute must be resolved by a human rather than by a
        # timer. Only HARDSHIP carries a date out of this branch, and only from the clause in
        # which the customer actually names the callback - see `_CALLBACK`.
        phrase = None
        if forced is Intent.HARDSHIP:
            found = _CALLBACK.search(reply)
            phrase = found.group(0) if found else None
        date = resolve_date(phrase, today) if phrase else None
        return ParsedReply(forced, date, phrase, source="override")

    if use_llm:
        raw = _ask_llm(reply, model, timeout)
        # `format: "json"` guarantees VALID json, not an OBJECT. A model may answer with an
        # array, a bare string or a number, and `.get` on any of those raises AttributeError.
        # Four of eight plausible responses crashed here, and because parse_reply runs per
        # reply inside run_engine, one bad response aborted a 5,000-customer batch with the
        # ledger half written. A malformed reply must degrade to the keyword fallback, which
        # is what the fallback is for.
        if isinstance(raw, dict):
            intent_text = str(raw.get("intent", "other")).strip().lower()
            phrase = raw.get("date_phrase")
            phrase = str(phrase).strip() if phrase not in (None, "", "null") else None
            try:
                intent = Intent(intent_text)
            except ValueError:
                intent = Intent.OTHER
            # A stop the model spots is HONOURED, in that direction only. The model can
            # never start contact, lift a stop or supply a date; it can only close a file.
            # This used to downgrade `opt_out` and `dispute` to OTHER "for a human look",
            # which no queue in this codebase ever performed: the reply was simply discarded
            # and the ladder carried on. Every paraphrased or non-English objection lands
            # here, because the regex above is a list of English phrases, so discarding it
            # was the unsafe direction of the only error that matters on this path.
            if intent in (Intent.OPT_OUT, Intent.DISPUTE):
                return ParsedReply(intent, None, None, source="llm_stop", model=model)
            date = resolve_date(phrase, today) if intent is Intent.PROMISE_TO_PAY else None
            return ParsedReply(intent, date, phrase, source="llm", model=model)

    intent, phrase = _fallback_intent(reply)
    date = resolve_date(phrase, today) if intent is Intent.PROMISE_TO_PAY else None
    return ParsedReply(intent, date, phrase, source="fallback")
