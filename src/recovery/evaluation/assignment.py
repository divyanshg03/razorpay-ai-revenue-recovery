"""Arm assignment, exactly as frozen in docs/metric-definition.md at commit 8d14dbe.

Nothing here is a choice made now. The seed, the bucketing function, the split and the unit
were all fixed on 31 August, before a line of engine code existed — and the git commit that
recorded them is an ancestor of this file. That ordering is the whole point: choosing an
assignment rule after seeing a result is indistinguishable from choosing the one that
flatters it.

## Why the customer, not the payment

A customer with two failed mandates would otherwise land in different arms, and contacting
them about one debt plainly changes their behaviour on the other. That is interference
between units, and it breaks the estimator. Assignment is therefore per `customer_ref`, and
every debt inherits its customer's arm.

## Why hash bucketing rather than a shuffled list

It is order-independent, resumable after a crash, needs no stored assignment table that
could drift, and anyone with the seed can reproduce it. A shuffled list would make customer
N's arm depend on how many customers were generated before them.

## Exclusions happen BEFORE bucketing, never after

Customers with a pre-existing opt-out, open dispute, hardship flag, or no reachable channel
are removed from the population before assignment. **No post-randomisation exclusion, ever.**
Dropping people afterwards because of something that happened to them *during* the
experiment is how a recovery number gets quietly inflated — the customers an engine annoys
into opting out are exactly the ones that must stay in the denominator.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..models import Arm, Customer

#: Frozen 31 Aug 2026. Do not change without an amendment in docs/metric-definition.md.
SEED = 20260905

#: Cumulative basis points out of 10,000. A 20/20/60 split.
_A_MAX = 2_000
_B_MAX = 4_000


def arm_for(customer_ref: str, seed: int = SEED) -> Arm:
    """Deterministic, order-independent, stable across runs and processes."""
    digest = hashlib.sha256(f"{seed}:{customer_ref}".encode()).hexdigest()
    bucket = int(digest, 16) % 10_000
    if bucket < _A_MAX:
        return Arm.DO_NOTHING
    if bucket < _B_MAX:
        return Arm.INCUMBENT_LADDER
    return Arm.ENGINE


def is_excluded(customer: Customer) -> str | None:
    """Reason this customer never enters the experiment, or None.

    Evaluated on the customer as GENERATED, before any arm touches them.
    """
    if customer.opted_out:
        return "pre_existing_opt_out"
    if customer.disputed:
        return "pre_existing_dispute"
    if customer.bereaved_or_hardship:
        return "pre_existing_hardship"
    if not (customer.has_whatsapp or customer.has_sms):
        return "no_reachable_channel"
    return None


@dataclass
class Assignment:
    arms: dict[str, Arm]
    excluded: dict[str, str]

    def counts(self) -> dict[str, int]:
        out = {a.value: 0 for a in Arm}
        for arm in self.arms.values():
            out[arm.value] += 1
        return out

    def shares(self) -> dict[str, float]:
        n = len(self.arms) or 1
        return {k: round(v / n, 4) for k, v in self.counts().items()}

    def refs_in(self, arm: Arm) -> set[str]:
        return {ref for ref, a in self.arms.items() if a is arm}


def assign(customers: list[Customer], seed: int = SEED) -> Assignment:
    arms: dict[str, Arm] = {}
    excluded: dict[str, str] = {}
    for c in customers:
        reason = is_excluded(c)
        if reason:
            excluded[c.ref] = reason
            continue
        arms[c.ref] = arm_for(c.ref, seed)
    return Assignment(arms=arms, excluded=excluded)
