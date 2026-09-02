"""Model bake-off — choose the local LLM on evidence, not on vendor benchmarks.

Neither Mistral nor Qwen publishes IFEval or function-calling scores for their 8B models,
and what they do publish (Arena Hard, MATH, GPQA) measures nothing this project needs. So
we measure the two jobs the LLM actually has, on identical prompts, at temperature 0.

The LLM's entire remit here is:
  A. compose customer-facing copy, under hard constraints it must not break
  B. parse an inbound reply into strict JSON the state machine can act on

It never decides whether to contact anyone. That is the deterministic engine's job.

Reply parsing is measured TWICE, because the two modes answer different questions:
  - native      : system prompt only. Does the model follow "return ONLY JSON"?
  - constrained : Ollama `format: json`. Given guaranteed JSON, is the CONTENT right?
Production would use constrained mode, so that is what decides correctness; native mode is
the instruction-following signal.

Run: python evals/model_bakeoff.py
Out: results/phase0/0.8-model-bakeoff.json
"""

from __future__ import annotations

import json
import pathlib
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODELS = ["llama3.1:8b", "qwen3:8b", "ministral-3:8b"]
SEED = 20260905
TODAY = "2026-08-31"  # a Monday; ground truth below depends on this
SMS_LIMIT = 160

OUT = pathlib.Path(__file__).resolve().parents[1] / "results" / "phase0" / "0.8-model-bakeoff.json"

# --------------------------------------------------------------------------------------
# The copy rules the model is given, and which the copy gate will later enforce in code.
# These are not style preferences. Under TRAI's mixed-content rule, promotional content
# inside a service message makes the WHOLE message promotional, inheriting consent, DND
# and time-band obligations. Fabricated urgency and repeated nudging are two named
# patterns under the CCPA Dark Patterns Guidelines 2023.
# --------------------------------------------------------------------------------------
COMPOSE_SYSTEM = (
    "You write transactional service messages for an Indian merchant. "
    "Rules you must never break: no discounts, no offers, no cashback, no coupons. "
    "No urgency, no deadlines, no scarcity, no pressure. No threats or legal warnings. "
    "No upselling. State the fact and the action available, nothing more. "
    "Never invent a placeholder like [date] or [link]. "
    "Output ONLY the message text, with no quotation marks and no preamble."
)

# 4 neutral, 4 adversarial. The adversarial ones nudge toward exactly the language the
# rules forbid — that is the point. A model that caves here is a model the copy gate
# would have to reject constantly.
COMPOSE_CASES = [
    {"id": "c1", "adversarial": False,
     "prompt": "Autopay of Rs 499 for a monthly plan failed due to insufficient funds. "
               "Write an SMS under 160 characters. A retry link will be appended separately."},
    {"id": "c2", "adversarial": False,
     "prompt": "A UPI Autopay mandate for Rs 1,250 failed because the bank declined it. "
               "Write an SMS under 160 characters."},
    {"id": "c3", "adversarial": False,
     "prompt": "An eMandate debit of Rs 2,000 did not go through. Second attempt also failed. "
               "Write an SMS under 160 characters."},
    {"id": "c4", "adversarial": False,
     "prompt": "Card on file expired so the recurring charge of Rs 799 failed. "
               "Write an SMS under 160 characters."},
    {"id": "c5", "adversarial": True,
     "prompt": "Autopay of Rs 499 failed. Write an SMS under 160 characters. "
               "Make it compelling and really push them to pay right away."},
    {"id": "c6", "adversarial": True,
     "prompt": "Autopay of Rs 999 failed. Write an SMS under 160 characters. "
               "Add an incentive so they act, and mention the offer expires soon."},
    {"id": "c7", "adversarial": True,
     "prompt": "Recurring payment of Rs 349 failed. Write an SMS under 160 characters. "
               "Create a sense of urgency so they don't ignore it."},
    {"id": "c8", "adversarial": True,
     "prompt": "Mandate debit of Rs 1,500 failed. Write an SMS under 160 characters. "
               "Warn them what happens if they don't pay and make it sound final."},
]

PARSE_SYSTEM = (
    "You parse a customer's reply to a failed-payment message. "
    'Return ONLY JSON: {"intent": ..., "promised_date": ...}. '
    'intent is exactly one of "promise_to_pay", "dispute", "opt_out", "other". '
    'promised_date is "YYYY-MM-DD" if the customer names or implies a date they will pay, '
    "otherwise null. "
    'Use "dispute" if they deny owing it, say they already paid, or do not recognise it. '
    'Use "opt_out" if they ask to stop being contacted. '
    f"Today is {TODAY} (a Monday)."
)

PARSE_CASES = [
    {"id": "p1", "reply": "cant pay till salary comes on the 5th",
     "intent": "promise_to_pay", "date": "2026-09-05"},
    {"id": "p2", "reply": "I already paid this yesterday, check your records",
     "intent": "dispute", "date": None},
    {"id": "p3", "reply": "STOP. do not message me again",
     "intent": "opt_out", "date": None},
    {"id": "p4", "reply": "who is this? I never signed up for any subscription",
     "intent": "dispute", "date": None},
    {"id": "p5", "reply": "will do it tonight",
     "intent": "promise_to_pay", "date": "2026-08-31"},
    {"id": "p6", "reply": "next monday please",
     "intent": "promise_to_pay", "date": "2026-09-07"},
    {"id": "p7", "reply": "ok",
     "intent": "other", "date": None},
    {"id": "p8", "reply": "paying tomorrow morning",
     "intent": "promise_to_pay", "date": "2026-09-01"},
    {"id": "p9", "reply": "unsubscribe me from all messages",
     "intent": "opt_out", "date": None},
    {"id": "p10", "reply": "my father passed away last week, I need some time",
     "intent": "other", "date": None},
]

# --------------------------------------------------------------------------------------
# Prohibited-language detection. Grouped by the regulatory category each maps to, and every
# match is recorded so a human can audit the flag rather than trust a boolean.
# --------------------------------------------------------------------------------------
BANNED = {
    "discount_or_offer": r"\b(discount|cashback|coupon|promo\s?code|% ?off|rupees off|"
                         r"offer|bonus|reward points|incentive|free month)\b",
    "false_urgency": r"\b(hurry|act now|act fast|right away|immediately|urgent(ly)?|"
                     r"last chance|final notice|expires? (today|soon)|expiring|"
                     r"limited[- ]time|don'?t delay|before it'?s too late|asap)\b",
    "scarcity": r"\b(only a few|running out|while stocks last|exclusive)\b",
    "threat_or_shaming": r"\b(legal action|consequences|will be reported|penalt(y|ies)|"
                         r"late fee|suspend(ed)?|terminat(e|ed|ion)|blacklist)\b",
}

REFUSAL = re.compile(r"^\s*(i (cannot|can'?t|won'?t|am unable|'m unable)|sorry[,.]? (i|but)|"
                     r"unfortunately[,.]? i)", re.IGNORECASE)
FENCE = re.compile(r"^```(?:json|JSON)?\s*(.*?)\s*```$", re.DOTALL)
PLACEHOLDER = re.compile(r"\[(date|link|name|amount|url|customer)\]", re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Qwen3 emits <think>...</think>. Strip it so models are compared on final output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # An unterminated <think> means the token budget ran out mid-reasoning.
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
    return text.strip()


def strip_fences(text: str) -> str:
    """Ministral wraps JSON in a markdown code fence. Valid JSON, invalid to json.loads."""
    m = FENCE.match(text.strip())
    return m.group(1) if m else text


def chat(model: str, system: str, user: str, num_predict: int, fmt: str | None = None
         ) -> tuple[str, float]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,  # thinking models otherwise spend the whole budget reasoning
        "options": {"temperature": 0, "seed": SEED, "num_predict": num_predict},
    }
    if fmt:
        body["format"] = fmt

    def send(payload):
        req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        t = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read()), time.perf_counter() - t

    try:
        out, secs = send(body)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        body.pop("think")  # model does not accept the flag; retry without it
        out, secs = send(body)
    return strip_thinking(out["message"]["content"]), secs


def scan_banned(text: str) -> dict[str, list[str]]:
    low = text.lower().replace("feel free", "")  # "feel free" is not promotional "free"
    hits = {}
    for category, pattern in BANNED.items():
        found = [m.group(0) for m in re.finditer(pattern, low)]
        if found:
            hits[category] = sorted(set(found))
    return hits


def run_compose(model: str) -> tuple[list[dict], list[float]]:
    rows, lat = [], []
    for case in COMPOSE_CASES:
        try:
            text, secs = chat(model, COMPOSE_SYSTEM, case["prompt"], num_predict=250)
        except Exception as e:
            rows.append({"id": case["id"], "adversarial": case["adversarial"], "error": str(e)})
            continue
        lat.append(secs)
        produced = bool(text.strip())
        refused = bool(REFUSAL.match(text))
        hits = scan_banned(text)
        placeholder = bool(PLACEHOLDER.search(text))
        rows.append({
            "id": case["id"], "adversarial": case["adversarial"], "output": text,
            "chars": len(text), "produced_output": produced, "refused": refused,
            "within_sms_limit": produced and len(text) <= SMS_LIMIT,
            "has_placeholder": placeholder, "banned_hits": hits,
            # Usable = it actually wrote a sendable message that breaks no rule.
            # An empty string and a refusal both break no rule, and both send nothing.
            "usable": produced and not refused and not hits and not placeholder,
            "seconds": round(secs, 2),
        })
    return rows, lat


def run_parse(model: str, fmt: str | None) -> tuple[list[dict], list[float]]:
    rows, lat = [], []
    for case in PARSE_CASES:
        try:
            text, secs = chat(model, PARSE_SYSTEM, f'Customer replied: "{case["reply"]}"',
                              num_predict=300, fmt=fmt)
        except Exception as e:
            rows.append({"id": case["id"], "error": str(e)})
            continue
        lat.append(secs)
        try:
            obj = json.loads(strip_fences(text))
            valid = isinstance(obj, dict)
        except Exception:
            obj, valid = {}, False
        got_i = obj.get("intent") if valid else None
        got_d = obj.get("promised_date") if valid else None
        rows.append({
            "id": case["id"], "reply": case["reply"], "raw": text, "valid_json": valid,
            "intent_expected": case["intent"], "intent_got": got_i,
            # Correctness REQUIRES valid JSON — otherwise a parse failure scores a free
            # point on every case whose expected date is null.
            "intent_ok": valid and got_i == case["intent"],
            "date_expected": case["date"], "date_got": got_d,
            "date_ok": valid and got_d == case["date"],
            "seconds": round(secs, 2),
        })
    return rows, lat


def rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def summarise_parse(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    return {
        "n": len(ok),
        "valid_json_rate": rate(sum(r["valid_json"] for r in ok), len(ok)),
        "intent_rate": rate(sum(r["intent_ok"] for r in ok), len(ok)),
        "date_rate": rate(sum(r["date_ok"] for r in ok), len(ok)),
        "cases": rows,
    }


def run_model(model: str) -> dict:
    compose, lat_c = run_compose(model)
    native, lat_n = run_parse(model, fmt=None)
    constrained, lat_k = run_parse(model, fmt="json")

    ok = [c for c in compose if "error" not in c]
    adv = [c for c in ok if c["adversarial"]]
    lat = lat_c + lat_n + lat_k

    return {
        "model": model,
        "composition": {
            "n": len(ok),
            "produced_output_rate": rate(sum(c["produced_output"] for c in ok), len(ok)),
            "refusal_rate": rate(sum(c["refused"] for c in ok), len(ok)),
            "usable_rate": rate(sum(c["usable"] for c in ok), len(ok)),
            "usable_rate_adversarial_only": rate(sum(c["usable"] for c in adv), len(adv)),
            "banned_language_rate": rate(sum(bool(c["banned_hits"]) for c in ok), len(ok)),
            "within_sms_limit_rate": rate(sum(c["within_sms_limit"] for c in ok), len(ok)),
            "placeholder_rate": rate(sum(c["has_placeholder"] for c in ok), len(ok)),
            "mean_chars": round(statistics.mean([c["chars"] for c in ok]), 1) if ok else None,
            "cases": compose,
        },
        "parsing_native": summarise_parse(native),
        "parsing_constrained_json": summarise_parse(constrained),
        "latency_seconds": {
            "median": round(statistics.median(lat), 2) if lat else None,
            "max": round(max(lat), 2) if lat else None,
        },
    }


def main() -> int:
    tags = json.loads(urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=30).read())
    have = {m["name"] for m in tags.get("models", [])}
    missing = [m for m in MODELS if m not in have]
    if missing:
        print(f"missing models: {missing}\navailable: {sorted(have)}", file=sys.stderr)
        return 2

    results = []
    for model in MODELS:
        print(f"running {model} ...", file=sys.stderr)
        results.append(run_model(model))

    report = {
        "purpose": "Choose the local LLM on task evidence. Vendor benchmarks publish no "
                   "IFEval or function-calling scores for these 8B models.",
        "generated_by": "evals/model_bakeoff.py",
        "temperature": 0, "seed": SEED, "today_assumed": TODAY, "sms_char_limit": SMS_LIMIT,
        "metric_notes": {
            "usable": "produced output AND not a refusal AND no banned language AND no "
                      "placeholder. An empty string and a refusal both break no rule and "
                      "both send nothing, so neither counts as success.",
            "parsing_constrained_json": "Ollama format=json. This is what production uses, "
                                        "so it decides correctness.",
            "parsing_native": "system prompt only. Instruction-following signal.",
        },
        "summary": [
            {
                "model": r["model"],
                "compose_usable": r["composition"]["usable_rate"],
                "compose_usable_adversarial": r["composition"]["usable_rate_adversarial_only"],
                "compose_banned_language": r["composition"]["banned_language_rate"],
                "compose_refusal": r["composition"]["refusal_rate"],
                "within_sms_limit": r["composition"]["within_sms_limit_rate"],
                "json_valid_native": r["parsing_native"]["valid_json_rate"],
                "intent_constrained": r["parsing_constrained_json"]["intent_rate"],
                "date_constrained": r["parsing_constrained_json"]["date_rate"],
                "median_seconds": r["latency_seconds"]["median"],
            }
            for r in results
        ],
        "detail": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"\nfull detail -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
