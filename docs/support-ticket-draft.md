# Gate 0.0 — Razorpay support ticket

> **FILED 2 September 2026. Ticket #20706328.** Acknowledged by Razorpay with a stated SLA of
> 4–8 business hours. Gate 0.0 passes: its condition was *"request filed and acknowledged"*,
> not *"request granted"*. Track the reply in Dashboard → Support.
>
> **Route that actually worked**, recorded because the obvious ones do not:
> Dashboard → Help & Support → category **Web Integration** → question *"Can the transaction
> flow be tested in Test mode?"* → mark the suggested answer **not helpful** → the contact form
> appears. The FAQ deflection will not surrender a ticket form until you reject its answer.

**Owner: Divyansh.** This is the one Phase 0 item an agent cannot do — it needs a logged-in
dashboard session.

> **Email does not work — verified 2 Sept 2026.** Sending to `support@razorpay.com` returns an
> automated reply: *"This is a non monitored email id."* It redirects to the Dashboard. The
> **only** working route is **Dashboard → Help & Support** (sign in at
> `dashboard.razorpay.com`), or the web form at `razorpay.com/support/#request`.
> `runtime/file-support-ticket.html` has copy-to-clipboard buttons for the subject and body.

**File it as one ticket covering all three asks.** They are handled by the same activation
team, and three separate tickets will be slower, not faster. Ask 3 is the one with a real
deadline attached, so it is stated first in the body even though it is the smallest request.

Fill in `<KEY_ID>` from `.env` (`RAZORPAY_KEY_ID`, the `rzp_test_…` value — the public half;
**never paste `RAZORPAY_KEY_SECRET` into a ticket**).

---

## Subject

```
Test mode: activate Subscriptions + recurring payments, and raise Payment Links cap (hackathon, deadline 5 Sept)
```

## Body

```
Hello,

I am building a submission for the Razorpay AI Buildathon (Track 03 — AI Revenue Recovery)
and need three things enabled on my test account. My submission deadline is 5 September 2026,
so a quick turnaround would help a great deal.

Account key ID (test mode): <KEY_ID>

1. PAYMENT LINKS CAP (most time-sensitive)
   Test mode is capped at 30 Payment Links per business. My evaluation batch needs to create
   links across a few thousand simulated debts. Could this cap be raised on the test account,
   or could you tell me the maximum available so I can size the batch to it? If a raise is not
   possible, please say so — I will sample the batch instead and document the sampling.

2. SUBSCRIPTIONS
   GET /v1/plans and GET /v1/subscriptions both return HTTP 401 {"error": "Unauthorized"} on
   this key. This is not a credential problem: the same key returns HTTP 200 on /payments,
   /orders, /customers, /payment_links, /invoices, /items, /refunds, /settlements and
   /disputes. /plans was retried three times over about two seconds and returned 401 each
   time, so it is not transient either.

   Your documentation notes Subscriptions is an on-demand feature requiring a request to
   Support to activate. Please could you activate it on this test account? I understand card
   payments must also be enabled under Subscriptions → Settings afterwards.

   Note for the docs team: the Subscriptions integration guide lists only "Flash Checkout
   enabled" as a prerequisite and does not mention that the feature itself must be activated
   by Support. That gap cost me a day.

3. RECURRING PAYMENTS
   POST /v1/payments/create/recurring returns HTTP 400 "The requested URL was not found on the
   server" — byte-identical to the response for /virtual_accounts, which is definitively not
   enabled on this account. So the recurring charge endpoint appears gated as well. Please
   enable it alongside Subscriptions.

Together, 2 and 3 mean I can register emandate and UPI Autopay mandates (both of these do
work — POST /orders with method=emandate and method=upi succeed) but cannot charge them or
observe the retry lifecycle.

Thank you,
Divyansh Gupta
```

---

## What to do with the reply

| Reply | Action |
|---|---|
| All three granted | Re-run `results/phase0/` probes to confirm, then swap the cohort source behind the existing interface. Nothing above the interface changes — that was the point of the design. |
| Subscriptions granted, cap refused | Sample the batch to the cap and state the sampling in the README. |
| Refused or no reply by 3 Sept | No change. This is already the assumed case: the simulator is the declared cohort source and the ladder is a reimplemented baseline. Record the refusal (or the silence) in `docs/phase-0-findings.md` as evidence the fallback was necessary rather than chosen for convenience. |

**The schedule does not depend on this ticket.** Decided 24 Aug and unchanged: the cohort comes
from the seeded simulator regardless. A grant is an upgrade, not a dependency — which is exactly
why it is worth filing even now, with days to go.

**If it lands late**, note that test-mode card tokens live only 3 days, so any token-dependent
seeding has to be redone close to demo day.
