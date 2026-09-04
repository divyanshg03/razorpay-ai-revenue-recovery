"""The two baselines the engine is measured against.

Neither of these is our engine. They are fixed, dumb policies, and they exist so the headline
number is an *incremental* one rather than a gross one.

**Arm A — do nothing.** No retry, no contact. Measures how much of the cohort self-cures, which
is the floor any recovery system must clear before it has done anything at all.

**Arm B — Razorpay's own ladder, reimplemented.** Their documentation: *"we automatically retry
the payment on the following day"*, four attempts T+0…T+3, then `halted` — after which *"you
will have to charge them manually."* Reimplemented rather than observed, because gate 0.3 was
voided when Subscriptions turned out to be gated on this account.

Arm B is the comparison that matters. Beating arm A proves nothing; every recovery vendor beats
doing nothing. The question a Razorpay panel will actually ask is whether we beat what Razorpay
already ships.

## The structural weakness this exposes

The ladder retries on four *consecutive* days. The dominant documented failure cause is an
empty account (NPCI; see `cohort/PARAMETERS.md`), and accounts refill on a roughly monthly
salary cycle. Four consecutive days therefore cover about an eighth of the cycle, and the
ladder misses most insufficient-funds cases no matter how good each individual retry is.

That is not a criticism the simulator was told to produce. It falls out of modelling paydays at
all, and it is the clearest single thing to say about where this project sits relative to
Razorpay's existing product.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..cohort.simulator import SimulatedCohort
from ..diagnosis.taxonomy import diagnose
from ..models import Debt

#: Razorpay's documented schedule: T+0, T+1, T+2, T+3, then halted.
INCUMBENT_RETRY_DAYS = (0, 1, 2, 3)


@dataclass
class ArmOutcome:
    """Per-debt result. `recovered_paise` is rupees actually settled, not a binary flag —
    the frozen metric counts money, so a token payment cannot score like full settlement."""

    debt_id: str
    customer_ref: str
    amount_paise: int
    recovered_paise: int = 0
    settled_on_day: int | None = None
    contacts: int = 0
    retries: int = 0
    contact_cost_paise: int = 0
    cause_bucket: str = ""
    actionability: str = ""
    stop_reasons: list[str] = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        return self.recovered_paise > 0


def _window_days(window_days: int) -> range:
    return range(0, window_days + 1)


def run_do_nothing(cohort: SimulatedCohort, debts: list[Debt], start: dt.date,
                   window_days: int) -> list[ArmOutcome]:
    """Arm A. We take no action at all; only self-cure can settle these."""
    outcomes = []
    for debt in debts:
        d = diagnose(debt.failure)
        out = ArmOutcome(debt.debt_id, debt.customer_ref, debt.amount_paise,
                         cause_bucket=d.bucket.value, actionability=d.actionability.value)
        for day_offset in _window_days(window_days):
            day = start + dt.timedelta(days=day_offset)
            if cohort.organic_settle(debt, day):
                out.recovered_paise = debt.amount_paise
                out.settled_on_day = day_offset
                break
        outcomes.append(out)
    return outcomes


def run_spread_retry_control(cohort: SimulatedCohort, debts: list[Debt], start: dt.date,
                             window_days: int, days: tuple[int, ...]) -> list[ArmOutcome]:
    """Arm D. Retry on the engine's OWN schedule and do nothing else whatsoever.

    No diagnosis, no guardrails, no contact, no ladder, no ledger, no model, no cost. Four
    lines of behaviour. It exists to answer the one question arms A, B and C cannot:

        how much of the engine's result is DECISIONING, and how much is just the calendar?

    Arm B differs from arm C in two ways at once - the retry SPACING (0,1,2,3 against
    0,4,8,13,17,21) and the entire decisioning layer - so C vs B cannot separate them. Arm D
    holds the spacing fixed at arm C's and removes everything else. C minus D is therefore
    what the decisioning layer is worth, and D minus B is what the calendar is worth.

    Added 4 Sept 2026 after an external review pointed out that the submission credited the
    decisioning layer for a result the spacing produces. It is the control that makes the
    headline interpretable, and it was missing. See amendment A10.

    It is deliberately NOT a candidate policy. It ignores opt-outs, disputes and hardship,
    which is unlawful, and it retries causes a silent retry can never fix. It is a
    measurement instrument, and `metrics.json` labels it as one.
    """
    outcomes = []
    for debt in debts:
        d = diagnose(debt.failure)
        out = ArmOutcome(debt.debt_id, debt.customer_ref, debt.amount_paise,
                         cause_bucket=d.bucket.value, actionability=d.actionability.value)
        for day_offset in days:
            if day_offset > window_days:
                break
            day = start + dt.timedelta(days=day_offset)
            out.retries += 1
            if cohort.attempt_charge(debt, day, d.actionability):
                out.recovered_paise = debt.amount_paise
                out.settled_on_day = day_offset
                break
        outcomes.append(out)
    return outcomes


def run_incumbent_ladder(cohort: SimulatedCohort, debts: list[Debt], start: dt.date,
                         window_days: int) -> list[ArmOutcome]:
    """Arm B. Retry T+0..T+3 then stop. No customer contact, so no contact cost.

    Note it retries *unconditionally* — including on dead instruments, where a retry can never
    succeed. That is faithful to the documented behaviour, and it is one of the two places the
    engine finds free money.
    """
    outcomes = []
    for debt in debts:
        d = diagnose(debt.failure)
        out = ArmOutcome(debt.debt_id, debt.customer_ref, debt.amount_paise,
                         cause_bucket=d.bucket.value, actionability=d.actionability.value)
        settled = False
        for day_offset in INCUMBENT_RETRY_DAYS:
            if day_offset > window_days:
                break
            day = start + dt.timedelta(days=day_offset)
            out.retries += 1
            if cohort.attempt_charge(debt, day, d.actionability):
                out.recovered_paise = debt.amount_paise
                out.settled_on_day = day_offset
                settled = True
                break
        if not settled:
            out.stop_reasons.append("ladder_exhausted")
            # After halting, the debt is simply left. Self-cure can still happen, and
            # excluding it would unfairly understate the incumbent.
            for day_offset in _window_days(window_days):
                if day_offset <= max(INCUMBENT_RETRY_DAYS):
                    continue
                day = start + dt.timedelta(days=day_offset)
                if cohort.organic_settle(debt, day):
                    out.recovered_paise = debt.amount_paise
                    out.settled_on_day = day_offset
                    break
        outcomes.append(out)
    return outcomes


def recovery_rate(outcomes: list[ArmOutcome]) -> float:
    return round(sum(o.recovered for o in outcomes) / len(outcomes), 4) if outcomes else 0.0


def recovered_paise(outcomes: list[ArmOutcome]) -> int:
    return sum(o.recovered_paise for o in outcomes)
