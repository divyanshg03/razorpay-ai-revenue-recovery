# Razorpay test-mode: what's actually buildable

Verified against `razorpay.com/docs` on 23–24 Aug 2026. Uncertainties flagged explicitly —
do not treat an unflagged claim as verified beyond what's linked.

## Keys: no KYC needed

Test keys need **signup only**. Dashboard → Test mode → Account & Settings → API Keys →
Generate. Prefix `rzp_test_`. *"You can generate API keys in Test Mode without adding a
website."* Live mode is the gated one (website verification ~3 working days + KYC).

Auth is HTTP Basic: `curl -u KEY_ID:KEY_SECRET`, base `https://api.razorpay.com/v1`.

## The failure taxonomy — our core signal

`GET /v1/payments/:id` returns five error fields: `error_code`, `error_description`,
`error_source`, `error_step`, `error_reason`.

**`error_code` has only three values** — `BAD_REQUEST_ERROR`, `GATEWAY_ERROR`,
`SERVER_ERROR`. Counter-intuitively `BAD_REQUEST_ERROR` carries most customer-side
declines (`insufficient_funds`, `card_expired`, `payment_cancelled`), so **do not classify
on `error_code`.** Classify on `error_reason` + `error_source` + `error_step`.

**`error_reason` has ~121 published values** — 68 under `BAD_REQUEST_ERROR`, 53 under
`GATEWAY_ERROR`, with ~12 appearing under both. So the composite key is
**`(error_code, error_reason)`**, never `error_reason` alone.

Full enumerations: [list of errors](https://razorpay.com/docs/errors/payments/list/) ·
[per-method parameters](https://razorpay.com/docs/errors/payments/payment-methods-error-parameters/) ·
downloadable `payments_error_reasons.xlsx` from Razorpay's own assets.

`error_source` by method — Cards: `customer|business|internal|gateway|issuer_bank`.
UPI adds `customer_psp|network|beneficiary_bank`. Emandate adds `bank`.

`error_step` is a lifecycle position — Cards: `payment_initiation` →
`card_enrollment_check` → `payment_authentication` → `payment_authorization` →
`payment_capture`. UPI has 15 steps. Emandate: `payment_initiation` →
`payment_authentication` → `payment_authorization`.

**Prior art to reimplement:** Razorpay's Dashboard groups reasons into four buckets —
Customer Drop-Offs, Bank Failures, Business Failures, Other. This is **Dashboard-only, not
exposed via API**, so our diagnosis layer rebuilds it from `error_reason` and can then go
further (actionability, not just attribution).

## The product opportunity, in Razorpay's own words

On recurring token debits:

> *"We will not attempt any retry if the debit fails for tokens with the notification
> object in the created order. **You should manually retry the debit attempt.**"*

And on subscriptions, the default is simply *"we automatically retry the payment on the
following day"*, four attempts (T+0…T+3), then `halted` — after which:

> *"Invoices for such Subscriptions are still created. However, we will not charge these
> invoices. **You will have to charge them manually.**"*

That manual-retry gap is the wedge. Programmatic lever:
`POST /v1/payments/create/recurring` with `customer_id`, `token`, `order_id`, `recurring: true`.

## Two webhook behaviours that will break a naive recovery engine

1. **`payment.failed` does not fire on first-payment authorisation failure.**
   Verbatim: *"payment.failed is not triggered if the payment fails during authorisation
   (while making the first payment)."*
2. **Ordering is not guaranteed and payments resurrect.** Verbatim: *"The webhook sequence
   is not fixed in the JSON payload for payment events"* — you can receive `payment.failed`
   **followed by** `payment.captured` for the same transaction (late authorisation or user
   retry, common on UPI).

→ **Design consequence: every action must re-check payment state immediately before
executing, or we will dun customers who have already paid.** This is a stopping rule, and
it's worth calling out in the pitch — it's the kind of detail that only shows up if you
actually read the docs.

Delivery is **at-least-once**, 5s ACK timeout, exponential backoff for 24h then auto-disable.
Dedup on the **`x-razorpay-event-id`** header. Signature is HMAC-SHA256 over the **raw**
body in header `X-Razorpay-Signature` — *"Do not parse or cast the webhook request body."*

## Simulating failures

**Card decline reasons map 1:1 to test cards** — e.g. Visa `4100 2800 0008 0001` →
`insufficient_fund`, `4100 2800 0009 0000` → `payment_timed_out`, `4100 2800 0002 0007` →
`gateway_technical_error`. But: *"in success/failure screen, you must select failure to get
the right error"* — **these require driving the hosted checkout UI, not a server call.**

UPI: `success@razorpay` / `failure@razorpay` only — one generic reason, no per-reason VPAs.
Quirk: in test mode UPI *cancellation* results in a **successful** payment.

**Subscriptions have a genuinely good simulator**: Dashboard "Charge this now" → pick
success or failure. Each failure increments `auth_attempts` and advances next charge by one
day; **4 consecutive failures → `halted`**. Fires `subscription.pending` then
`subscription.halted`.

## Hard limits that constrain the batch design

| Limit | Value | Consequence |
|---|---|---|
| **Payment Links in test mode** | **30 per business** | Cannot create a link per failed payment at 200+ scale. Sample, or request a raise on day 1. |
| **Card token validity (test)** | **3 days** | The subscription demo has a 72-hour clock. Re-seed near demo day. |
| `GET /v1/payments` | `count` max 100, **no `status` filter** | Must page everything and filter client-side. |
| Auth → capture | 3 days, else auto-refund | Don't leave test payments hanging. |
| Webhook ACK | 5 seconds | Queue and ACK immediately; never process inline. |
| Rate limits | **Undocumented** | Only "429 exists, back off." Build a configurable throttle (~2–5 req/s) with jittered retry and a resumable cursor. |

## Local webhook testing

**`ngrok.io`, `localhost` and `webhook.site` are blacklisted by Razorpay.** They
explicitly recommend **`zrok`**. Ports **80 or 443 only**. Test-mode Dashboard OTP for
creating/editing a webhook is **`754081`**.

## Scope verdict

**Green** — test keys without KYC · the full 121-reason taxonomy · Orders/Payments/Refunds ·
Payment Links create + `POST /v1/payment_links/:id/notify_by/:medium` · Invoices `notify_by` ·
Subscriptions lifecycle with a working failure simulator · webhooks with HMAC verification.

**Amber** — 30-link cap · 3-day token validity · specific decline reasons need browser
automation · no `status` filter · undocumented rate limits.

**Red, do not scope** — Disputes (no creation, no simulation) · Settlements (no test-mode
generation) · Smart Collect (activation-gated, no API simulator) · Payment Pages (no API).

## Unverified — check on day 1

1. **Whether Subscriptions works on a fresh unactivated test account.** Docs list only
   "Flash Checkout enabled" as a prerequisite. **If Subscriptions is gated, the entire
   mandate-recovery pillar disappears** — this is the single highest-value 30-minute
   de-risk and it happens before anything else gets built.
2. Whether `error_source`/`error_step`/`error_reason` are populated in test mode identically
   to live. Verify empirically with one card.
3. Whether `notify_by` actually delivers SMS/email in test mode, or just returns
   `{"success": true}`.
4. Docs use both `insufficient_fund` and `insufficient_funds`. Log what actually arrives.
5. Refunds and Settlements in test mode — inferred, never stated.
