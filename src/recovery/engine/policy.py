"""Versioned policy configuration and the cost-sensitive decision rule.

Every tunable the engine consults lives here, under ONE version string. Phase 1's ledger pins
`policy_version` into every record at write time; this file is what that string points at.
Change a number, bump the version, and every decision taken before the change still resolves
to the rule that governed it.

## Provenance, stated exactly — this is where gate 0.6 pays off

| Rule | Status | Why it is labelled that way |
|---|---|---|
| 08:00–19:00 IST contact window | **product invariant** | RBI 454Y(4) binds Regulated Entities, not a plain merchant. Adopted as the de-facto standard; makes the product sellable to a lender unchanged. |
| 7 contacts / 7 days | **policy choice** | US Reg F, 12 CFR 1006.14(b). **No force in India.** RBI names no number — only "excessively calling / messaging" (454Z(4)). Never cite a numeric cap to RBI. |
| cap covers messaging, not just calls | RBI 454Z(4) | verified at source in gate 0.6 |
| opt-out is a hard stop | **statutory** | DPDP s.7(a) is conditional on the person not having objected |
| dispute is a hard stop | **policy choice** | gate 0.6 found the "454J grievance hard stop" claim UNSUPPORTED. Do not re-cite it. |
| bereavement / hardship is a hard stop | **policy choice** | the bake-off showed a model classify "my father passed away" as promise_to_pay |

## Costs — frozen in docs/metric-definition.md, taken at the EXPENSIVE end of each cited range

Stored in integer paise, rounded UP from the frozen rupee figures (conservative in the same
direction as the rest of the cost model).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from ..models import Actionability, Channel

#: Bump on ANY change below. Format: date.revision.
POLICY_VERSION = "2026-09-02.1"


@dataclass(frozen=True)
class Policy:
    version: str = POLICY_VERSION

    # -- contact window: product invariant, narrowing-only override per tenant ----------
    contact_window_start: dt.time = dt.time(8, 0)
    contact_window_end: dt.time = dt.time(19, 0)

    # -- frequency caps: POLICY CHOICE (US Reg F), applied to messaging per RBI 454Z(4) ---
    max_contacts_per_debt_7d: int = 7
    max_whatsapp_per_24h: int = 1
    max_sms_per_24h: int = 1

    # -- retries ----------------------------------------------------------------------
    #: A silent retry costs nothing in the frozen cost model, which would make unlimited
    #: retries a free lunch. Networks and issuers throttle repeated mandate execution, so
    #: the count is bounded as a policy choice. Spread across the window rather than
    #: clustered — that is the whole difference from the incumbent's T+0..T+3.
    max_retries_per_debt: int = 6
    retry_spacing_days: int = 3
    retry_horizon_days: int = 21

    # -- escalation ladder: terminates, never loops, tone never escalates --------------
    ladder: tuple[Channel, ...] = (
        Channel.WHATSAPP_UTILITY,
        Channel.SMS_SERVICE,
        Channel.HUMAN_CALL,
    )
    #: Days to wait after the first contact before escalating one rung.
    escalation_wait_days: int = 4
    #: Human escalation is the one rung where cost genuinely bites (1.53 pp break-even on a
    #: Rs 1,000 debt). Below this outstanding amount it is never worth it.
    min_amount_for_human_call_paise: int = 50_000

    # -- promise to pay ---------------------------------------------------------------
    #: Silence until the promised date + this many days; broken-promise handling after.
    ptp_grace_days: int = 1

    # -- cost model (paise, rounded UP from docs/metric-definition.md) -------------------
    cost_paise: dict[Channel, int] = field(default_factory=lambda: {
        Channel.WHATSAPP_UTILITY: math.ceil(17.11),  # Rs 0.1711 incl. GST
        Channel.SMS_SERVICE: math.ceil(21.24),       # Rs 0.2124 incl. GST
        Channel.HUMAN_CALL: 1534,                    # Rs 15.34 incl. GST
        Channel.RETRY: 0,
    })

    # -- uplift priors for the EV rule ------------------------------------------------
    #: POLICY PRIORS, not learned and NOT the simulator's parameters. The engine never sees
    #: the generative model; these are round numbers a collections lead would defend, and
    #: they exist so the cost gate has something to multiply. Decays per prior contact so
    #: the rule stops recommending a fourth message to someone who ignored three.
    contact_uplift_prior: dict[Actionability, float] = field(default_factory=lambda: {
        Actionability.NEEDS_FUNDS: 0.25,
        Actionability.NEEDS_NEW_INSTRUMENT: 0.20,
        Actionability.NEEDS_CUSTOMER_ACTION: 0.15,
        Actionability.RETRY_LATER: 0.05,   # the retry does the work; a message adds little
        Actionability.DO_NOT_CONTACT: 0.0,
    })
    uplift_decay_per_contact: float = 0.5


def in_contact_window(now: dt.datetime, policy: Policy) -> bool:
    """Written as a permission, not a prohibition: outside the window is denied by default.

    That mirrors the drafting of RBI 454Y(4) — "only between 08:00 hours and 19:00 hours" —
    so the state machine treats out-of-window as the default state to be earned out of, not
    a blocklist to check against.
    """
    t = now.time()
    return policy.contact_window_start <= t < policy.contact_window_end


def expected_value_paise(policy: Policy, actionability: Actionability, amount_paise: int,
                         prior_contacts: int, channel: Channel) -> float:
    """P(incremental recovery) x amount - cost. Act only when positive."""
    p = policy.contact_uplift_prior.get(actionability, 0.0)
    p *= policy.uplift_decay_per_contact ** prior_contacts
    return p * amount_paise - policy.cost_paise[channel]


def worth_acting(policy: Policy, actionability: Actionability, amount_paise: int,
                 prior_contacts: int, channel: Channel) -> bool:
    return expected_value_paise(policy, actionability, amount_paise,
                                prior_contacts, channel) > 0


def retry_schedule(policy: Policy) -> tuple[int, ...]:
    """Day offsets on which the engine retries a retryable cause.

    Spread across the horizon — 0, 3, 6, 9, 12, 15 — rather than the incumbent's 0, 1, 2, 3.
    Same number of attempts either way is NOT the point; coverage of the salary cycle is.
    """
    days = tuple(range(0, policy.retry_horizon_days + 1, policy.retry_spacing_days))
    return days[: policy.max_retries_per_debt]
