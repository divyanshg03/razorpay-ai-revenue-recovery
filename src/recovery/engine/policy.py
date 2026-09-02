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
#: .2 — raised the human-call floor Rs 500 -> Rs 2,000 after measuring that the rung was
#:      91.7% of spend for no measurable recovery. Decisions taken under .1 keep that
#:      version in the ledger; that is what pinning it at write time is for.
POLICY_VERSION = "2026-09-02.2"


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
    #: A customer who cannot receive a rung SKIPS to the next one they can (see
    #: machine.py `_first_reachable_rung`), so someone without WhatsApp still gets an SMS —
    #: they simply start at the SMS rung. What they do not get is a replacement for the
    #: rung they skipped: they receive a mean of 1.31 contacts against 1.94.
    #:
    #: DECISION, 2 Sept 2026 — measured, then declined. We built the equal-touch version
    #: (steps rather than channels, so an SMS-only customer got SMS -> SMS -> human) and
    #: reverted it. What the measurement showed:
    #:
    #:   * the touch deficit is real and structural — one lost step, deterministically
    #:   * the RECOVERY gap is not. A first look at n=248 suggested 4.9pp, but the standard
    #:     error there is ~3.2pp. At n=8,000 the difference is -0.76pp, 95% CI
    #:     [-3.73, +2.20], z=-0.50, with the sub-populations identical on payday, debt size
    #:     and cause mix
    #:   * equalising cost +149 contacts and +Rs 774 per 1,500 customers, for overall
    #:     recovery of 52.3% against 52.4% — statistically unchanged
    #:
    #: So it was spending real money for no measurable return, which is precisely what a
    #: cost-sensitive policy exists to refuse. Reverted at the user's direction. Reopen this
    #: only with evidence of a genuine recovery difference by channel availability — the
    #: equal-treatment argument alone did not survive contact with the numbers.
    ladder: tuple[Channel, ...] = (
        Channel.WHATSAPP_UTILITY,
        Channel.SMS_SERVICE,
        Channel.HUMAN_CALL,
    )
    #: Days to wait after the first contact before escalating one rung.
    escalation_wait_days: int = 4
    #: Human escalation is the one rung where cost genuinely bites (1.53 pp break-even on a
    #: Rs 1,000 debt). Below this outstanding amount it is never worth it.
    #:
    #: RAISED Rs 500 -> Rs 2,000 on 2 Sept 2026, from measurement. At the old floor the human
    #: rung was 12% of sends and **91.7% of all modelled spend** (558 sends, Rs 8,559 of
    #: Rs 9,339 at n=3,000) while recovery was flat: 53.63% with it, 53.67% without, a
    #: difference inside noise at that n. Raising the floor cuts modelled cost 77% to
    #: Rs 2,160 with identical recovery.
    #:
    #: WHY THE EV GATE DID NOT CATCH THIS. `contact_uplift_prior` gives a human call 0.25,
    #: decayed to 0.0625 by the third contact - enough to clear Rs 15.34 on any debt over
    #: ~Rs 250. The measured incremental value is approximately zero, so the prior is
    #: uncalibrated for this rung. The floor is a blunt correction for that; a calibrated
    #: prior would be the principled one.
    #:
    #: HONEST CAVEAT, and it should be said aloud to a panel. The simulator treats a human
    #: call as just another message subject to fatigue decay. A real conversation almost
    #: certainly converts far better than a third text, so this model UNDERSTATES the human
    #: rung. "Human calls add nothing" is a finding about the model as much as the policy,
    #: which is exactly why the rung is kept for larger debts rather than deleted.
    min_amount_for_human_call_paise: int = 200_000

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
