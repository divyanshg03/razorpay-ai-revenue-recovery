"""Domain model.

Deliberately plain dataclasses. The decisioning layer is ordinary deterministic Python and
gains nothing from a validation framework at this size; every dependency is another way a
live demo breaks on someone else's machine.

Money is in PAISE (integers) everywhere. Rupee floats invite rounding drift, and the headline
metric of this project is a rupee figure that has to survive a panel checking the arithmetic.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class Arm(str, enum.Enum):
    """Experiment arms, frozen in docs/metric-definition.md.

    Three, not two. Beating DO_NOTHING proves nothing; the question a Razorpay panel cares
    about is whether we beat what Razorpay already ships, which is INCUMBENT_LADDER.
    """

    DO_NOTHING = "A"
    INCUMBENT_LADDER = "B"
    ENGINE = "C"


class Channel(str, enum.Enum):
    WHATSAPP_UTILITY = "whatsapp_utility"
    SMS_SERVICE = "sms_service"
    HUMAN_CALL = "human_call"
    RETRY = "retry"  # a silent re-attempt of the charge; no customer contact


class MandateType(str, enum.Enum):
    UPI_AUTOPAY = "upi_autopay"
    EMANDATE = "emandate"
    CARD = "card"


class CauseBucket(str, enum.Enum):
    """Razorpay's own Dashboard groups reasons into four buckets. They are Dashboard-only and
    not exposed via API, so the diagnosis layer rebuilds them — then goes further, because
    attribution alone does not tell you what to DO."""

    CUSTOMER_DROP_OFF = "customer_drop_off"
    BANK_FAILURE = "bank_failure"
    BUSINESS_FAILURE = "business_failure"
    OTHER = "other"


class Actionability(str, enum.Enum):
    """What the merchant can actually do about it — the part Razorpay's buckets do not answer.

    This is the reason the diagnosis layer earns its place: `insufficient_funds` and
    `card_expired` land in different buckets but demand completely different interventions,
    and `payment_frequency_exceeded` should be left alone entirely.
    """

    RETRY_LATER = "retry_later"          # transient; time fixes it
    NEEDS_FUNDS = "needs_funds"          # customer must top up; timing to payday matters
    NEEDS_NEW_INSTRUMENT = "needs_new_instrument"  # expired/blocked; a retry can never work
    NEEDS_CUSTOMER_ACTION = "needs_customer_action"  # authentication, app action
    DO_NOT_CONTACT = "do_not_contact"    # our own fault or a hard block; contacting is noise


class StopReason(str, enum.Enum):
    """Every one of these is enforced in code and logged. Hard stops are absolute."""

    PAID = "payment_received"
    OPTED_OUT = "opt_out"
    DISPUTED = "dispute_raised"
    BEREAVEMENT = "bereavement_or_hardship"
    PROMISE_TO_PAY = "promise_to_pay_active"
    CONTACT_CAP = "contact_cap_reached"
    QUIET_HOURS = "outside_contact_window"
    LADDER_EXHAUSTED = "escalation_ladder_exhausted"
    NOT_ACTIONABLE = "cause_not_actionable"
    NO_REACHABLE_CHANNEL = "no_reachable_channel"


@dataclass
class PaymentFailure:
    """The five error fields Razorpay returns, exactly as observed in gate 0.2.

    Classification NEVER keys on error_code alone: it has only three values and
    BAD_REQUEST_ERROR carries most customer-side declines. The composite key is
    (error_code, error_reason), refined by error_source and error_step.
    """

    error_code: str
    error_reason: str
    error_source: str | None = None
    error_step: str | None = None
    error_description: str | None = None


@dataclass
class Customer:
    ref: str
    has_whatsapp: bool = True
    has_sms: bool = True
    # Consent basis under DPDP s.7(a): data given voluntarily for a specified purpose.
    # It is conditional on the person not having objected, which is why opted_out is a
    # statutory stopping rule and not a product preference.
    opted_out: bool = False
    disputed: bool = False
    bereaved_or_hardship: bool = False

    @property
    def reachable(self) -> bool:
        return (self.has_whatsapp or self.has_sms) and not self.opted_out


@dataclass
class Debt:
    """One failed recurring collection. The unit of recovery, but NOT the unit of
    randomisation — that is the customer, so treating one debt cannot contaminate another."""

    debt_id: str
    customer_ref: str
    amount_paise: int
    mandate_type: MandateType
    failed_at: dt.datetime
    failure: PaymentFailure
    # Set once the debt is settled; None while outstanding.
    recovered_paise: int = 0
    reversed_paise: int = 0
    settled_at: dt.datetime | None = None

    @property
    def outstanding_paise(self) -> int:
        return max(0, self.amount_paise - self.recovered_paise + self.reversed_paise)


@dataclass
class Action:
    """A single thing the engine did. Every action is written to the audit ledger BEFORE its
    outcome is known, so the trail records intent, not a tidied-up history."""

    debt_id: str
    customer_ref: str
    channel: Channel
    at: dt.datetime
    cost_paise: int
    policy_version: str
    rendered_text: str | None = None
    template_ref: str | None = None
    rules_fired: list[str] = field(default_factory=list)
    rules_passed: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """The output of the deterministic state machine.

    `act` False with a reason is just as important as `act` True: a submission that only
    records what it did, and not what it declined to do and why, cannot evidence its own
    stopping rules.
    """

    act: bool
    channel: Channel | None = None
    stop_reason: StopReason | None = None
    rules_fired: list[str] = field(default_factory=list)
    rules_passed: list[str] = field(default_factory=list)
    expected_value_paise: float = 0.0
