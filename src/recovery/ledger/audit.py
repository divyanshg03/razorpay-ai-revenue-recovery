"""Append-only, tamper-evident audit ledger.

## Built before the decision engine, on purpose

The design commitment is that *every decision is reconstructable under the policy version that
applied at the time*. Retrofit a ledger after an engine exists and that stops being true: you
end up with decisions whose governing rules were never recorded, and no amount of later
tooling recovers them. Writing this first makes the claim true by construction rather than by
assertion.

## What gets written, and when

Records are written **before the outcome is known**. A ledger assembled after the fact is a
tidied-up history, not an audit trail.

**Refusals are recorded as fully as actions.** A system that logs only what it did cannot
evidence its own stopping rules — and stopping rules are exactly what the track brief asks
for. `DECISION` entries carry both `rules_fired` and `rules_passed`, so "we did not contact
this person, and here are the five checks that ran and the one that stopped us" is
reconstructable from the file alone.

## Tamper evidence

Each record carries the SHA-256 of **the whole previous record**, itself included in the next
one's digest. Editing, deleting or reordering any line breaks the chain from that point on,
and `verify_chain()` reports the first index where it breaks.

The digest covers every field except `record_hash` itself. That is narrower than it sounds and
worth stating: until 4 Sept 2026 it covered `prev_hash + body` only, which left
`policy_version`, `timestamp_ist`, `type`, `event_id` and `model_version` rewritable with the
chain still reporting intact — the exact fields the claims above rest on. See `_hash`.

This is not cryptographic custody — anyone who can rewrite the file can recompute the chain —
but it makes silent, casual edits detectable, which is the realistic threat for a submission
artifact.

## Two ledgers, not one

The consent and objection ledger is separate. DPDP s.6(10) puts the burden of proving consent
on us, and s.7(a) makes the lawful basis conditional on the person not having objected — so
consent, objection and propagation have to be immutable events with their own trail, not a
mutable flag on a customer row.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from ..models import IST, Action, Decision

GENESIS_HASH = "0" * 64


class RecordType(str, Enum):
    DECISION = "decision"          # what the engine chose, including refusals
    ACTION = "action"              # something we actually did to a customer
    OUTCOME = "outcome"            # what happened afterwards
    STATE_RECHECK = "state_recheck"  # payment state re-read immediately before acting
    CONSENT = "consent"            # consent given / objection raised / propagated
    INBOUND = "inbound"            # what the customer wrote back, and how it was read


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dt.datetime):
        return value.astimezone(IST).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class AuditLedger:
    """Append-only JSONL with a hash chain. One instance per run."""

    def __init__(self, path: str | pathlib.Path, policy_version: str,
                 model_version: str = "none", now: dt.datetime | None = None,
                 fresh: bool = False):
        """`fresh=True` starts a NEW chain at `path`, discarding any file already there.

        Appending is the default and stays the default, because that is what an append-only
        ledger is for. But a batch RUN is a new experiment producing a new artifact, and
        silently appending one run onto another produces a file that is not a ledger of
        anything: two chains of the same debt ids interleaved, where an invariant comparing
        "was this debt contacted after it settled" reads the second run's settlement against
        the first run's contacts and reports violations that never happened.

        That is not hypothetical - it is exactly what happened on the first re-run after the
        `retry_schedule` fix (2 Sept 2026). 56 phantom `contact_after_payment` violations, and
        the batch correctly refused to write metrics.json off the back of them. The ledger was
        60 MB against 27 MB for the two that had only ever been written once.

        This is a flag rather than a default because the choice must be visible at the call
        site. Nothing silently truncates an audit trail; the caller says so, in writing.
        """
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fresh and self.path.exists():
            self.path.unlink()
        self.policy_version = policy_version
        self.model_version = model_version
        self._lock = threading.Lock()
        self._fixed_now = now
        #: Optional clock. A simulation runs on virtual days, and an audit trail stamped with
        #: the wall-clock time of the machine that replayed it would be useless for replaying
        #: the ORDER of events - every invariant check compares timestamps. Production leaves
        #: this None and gets real time.
        self.clock: Callable[[], dt.datetime] | None = None
        self._last_hash = self._recover_last_hash()

    # -- internals ------------------------------------------------------------------

    def _recover_last_hash(self) -> str:
        """Resume an existing chain rather than starting a new one.

        A run that crashes and restarts must extend the same chain; silently beginning a
        second chain in the same file would look exactly like tampering.
        """
        if not self.path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    last = json.loads(line)["record_hash"]
                except (json.JSONDecodeError, KeyError):
                    continue
        return last

    def _now(self) -> dt.datetime:
        if self.clock is not None:
            return self.clock()
        return self._fixed_now or dt.datetime.now(IST)

    @staticmethod
    def _hash(entry: dict) -> str:
        """Digest the WHOLE record except its own hash.

        This used to be `sha256(prev_hash + body)`, which left every field outside `body`
        unprotected: `policy_version`, `timestamp_ist`, `type`, `event_id` and
        `model_version` could all be rewritten and `verify_chain()` still returned intact.
        Verified by forging each one in turn - five out of five went undetected.

        That gap sat directly under the claims the ledger exists to support. "Every decision
        reconstructable under the policy version that applied at the time" leans on
        `policy_version`; "replayable in order" leans on `timestamp_ist`; and `type` is what
        the replay invariants dispatch on, so an ACTION relabelled as a DECISION would slip
        past the check for contacting someone after they paid. None of it changed a figure,
        and tamper-evidence you have to qualify is worth much less than tamper-evidence you
        do not.

        `prev_hash` lives inside the entry, so hashing the whole record still chains it.
        `sort_keys` keeps the digest independent of dict ordering. Found 4 Sept 2026 by
        review; see amendment A9.
        """
        material = {k: v for k, v in entry.items() if k != "record_hash"}
        payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _append(self, record_type: RecordType, body: dict) -> dict:
        with self._lock:
            entry = {
                "event_id": str(uuid.uuid4()),
                "timestamp_ist": self._now().isoformat(),
                "type": record_type.value,
                # Pinned at write time. This is the whole point: a decision taken under
                # policy v3 stays attributed to v3 even after v4 ships.
                "policy_version": self.policy_version,
                "model_version": self.model_version,
                "body": _jsonable(body),
                "prev_hash": self._last_hash,
            }
            entry["record_hash"] = self._hash(entry)   # covers every field but itself
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            self._last_hash = entry["record_hash"]
            return entry

    # -- public API -----------------------------------------------------------------

    def record_decision(self, debt_id: str, customer_ref: str, decision: Decision,
                        diagnosis: str, extra: dict | None = None) -> dict:
        """Written for EVERY evaluation, whether or not we act.

        The refusals are the compliance evidence. `rules_passed` matters as much as
        `rules_fired`: it shows the check ran and cleared, rather than never having run.
        """
        return self._append(RecordType.DECISION, {
            "debt_id": debt_id,
            "customer_ref": customer_ref,
            "acted": decision.act,
            "channel": decision.channel,
            "stop_reason": decision.stop_reason,
            "rules_fired": decision.rules_fired,
            "rules_passed": decision.rules_passed,
            "expected_value_paise": round(decision.expected_value_paise, 2),
            "diagnosis": diagnosis,
            **(extra or {}),
        })

    def record_state_recheck(self, debt_id: str, already_paid: bool, source: str) -> dict:
        """Razorpay webhooks are at-least-once and unordered: `payment.failed` can be followed
        by `payment.captured` for the same transaction. Every action re-reads payment state
        immediately beforehand, and that read is itself logged — otherwise "we never dun
        someone who has already paid" is unfalsifiable."""
        return self._append(RecordType.STATE_RECHECK, {
            "debt_id": debt_id, "already_paid": already_paid, "source": source,
        })

    def record_action(self, action: Action, message_class: str = "service",
                      consent_basis: str = "dpdp_s7a_voluntarily_provided",
                      copy_gate_rejected: dict | None = None,
                      llm_output: str | None = None) -> dict:
        """Something was actually sent. `message_class` is load-bearing: under TRAI's
        mixed-content rule, promotional content inside a service message makes the WHOLE
        message promotional and inherits consent, DND and time-band obligations."""
        return self._append(RecordType.ACTION, {
            "debt_id": action.debt_id,
            "customer_ref": action.customer_ref,
            "channel": action.channel,
            "at": action.at,
            "cost_paise": action.cost_paise,
            "message_class": message_class,
            "consent_basis": consent_basis,
            "template_ref": action.template_ref,
            "rendered_text": action.rendered_text,
            "rules_fired": action.rules_fired,
            "rules_passed": action.rules_passed,
            # When the copy gate rejected the model's wording and a template was sent
            # instead, record BOTH: what the model wrote, and the categories it tripped.
            # Without this the gate's interventions are invisible - and a gate nobody can
            # see firing is indistinguishable from one that never fires.
            "copy_gate_rejected_llm": copy_gate_rejected,
            "llm_output_rejected": llm_output if copy_gate_rejected else None,
        })

    def record_outcome(self, debt_id: str, customer_ref: str, recovered_paise: int,
                       at: dt.datetime | None = None, note: str = "") -> dict:
        return self._append(RecordType.OUTCOME, {
            "debt_id": debt_id, "customer_ref": customer_ref,
            "recovered_paise": recovered_paise, "at": at, "note": note,
        })

    def record_inbound(self, debt_id: str, customer_ref: str, text: str, intent: str,
                       promised_date: dt.date | None, source: str) -> dict:
        """The customer's reply, verbatim, plus how the parser read it and by what means
        (code override, model, or keyword fallback). The verbatim text is what lets a human
        later judge whether the machine read it right."""
        return self._append(RecordType.INBOUND, {
            "debt_id": debt_id, "customer_ref": customer_ref, "text": text,
            "intent": intent, "promised_date": promised_date, "parser_source": source,
        })

    def record_consent(self, customer_ref: str, event: str, basis: str,
                       propagated_to: list[str] | None = None) -> dict:
        """Consent, objection and propagation as immutable events.

        s.6(10) puts the burden of proof on us, and Rule 8(3) requires 48 hours' notice before
        erasure — neither is satisfiable from a mutable boolean on a customer record.
        """
        return self._append(RecordType.CONSENT, {
            "customer_ref": customer_ref, "event": event, "basis": basis,
            "propagated_to": propagated_to or [],
        })

    # -- reading and verification ----------------------------------------------------

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def verify_chain(self) -> tuple[bool, int | None]:
        """Returns (intact, first_broken_index).

        Detects edits, deletions and reordering, across EVERY field of every record - not
        just `body`. See `_hash`: the digest used to cover `prev_hash + body` only, so the
        metadata the ledger's own claims rest on could be rewritten freely.
        """
        prev = GENESIS_HASH
        for i, entry in enumerate(self.read()):
            if entry.get("prev_hash") != prev:
                return False, i
            if entry.get("record_hash") != self._hash(entry):
                return False, i
            prev = entry["record_hash"]
        return True, None

    def replay_debt(self, debt_id: str) -> list[dict]:
        """Everything that happened to one debt, in order, with the policy version that
        governed each step. This is the "reconstructable" claim, executable."""
        return [e for e in self.read() if e["body"].get("debt_id") == debt_id]

    def summary(self) -> dict:
        """Counts for the metrics artifact. Guardrail figures are assertions elsewhere; these
        are descriptive."""
        entries = self.read()
        decisions = [e for e in entries if e["type"] == RecordType.DECISION.value]
        actions = [e for e in entries if e["type"] == RecordType.ACTION.value]
        stops: dict[str, int] = {}
        for d in decisions:
            reason = d["body"].get("stop_reason")
            if reason:
                stops[reason] = stops.get(reason, 0) + 1
        intact, broken_at = self.verify_chain()
        return {
            "records": len(entries),
            "decisions": len(decisions),
            "actions_taken": len(actions),
            "decisions_declined": sum(1 for d in decisions if not d["body"]["acted"]),
            "stop_reasons": dict(sorted(stops.items(), key=lambda kv: -kv[1])),
            "total_contact_cost_paise": sum(a["body"]["cost_paise"] for a in actions),
            "chain_intact": intact,
            "chain_broken_at": broken_at,
        }
