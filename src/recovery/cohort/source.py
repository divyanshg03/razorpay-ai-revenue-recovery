"""The cohort source interface.

The whole point of this file is that it is boring. Subscriptions is gated on our test account
(gate 0.1), so today the cohort comes from a seeded simulator. If Razorpay grants activation
on ticket #20706328, a `RazorpayCohortSource` implements the same two methods and **nothing
above this interface changes** — not the diagnosis layer, not the state machine, not the
guardrails, not the measurement.

That is a stronger architectural claim than a system welded to one vendor's subscription
product, and it is the reason the day-0 blocker cost a day rather than the submission.
"""

from __future__ import annotations

from typing import Protocol

from ..models import Customer, Debt


class CohortSource(Protocol):
    """Anything that can produce a population of failed recurring collections."""

    #: Human-readable provenance, printed in the README and the audit trail. A simulated
    #: cohort must SAY it is simulated; an implied real one is the dishonesty the repo's
    #: rules exist to prevent.
    provenance: str

    def customers(self) -> list[Customer]:
        """Every customer in the cohort, including those with no reachable channel."""
        ...

    def debts(self) -> list[Debt]:
        """Every failed collection awaiting recovery."""
        ...
