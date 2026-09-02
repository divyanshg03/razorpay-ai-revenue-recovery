"""Failure-cause diagnosis.

## Why this layer exists

Razorpay's Dashboard groups failure reasons into four buckets — Customer Drop-Offs, Bank
Failures, Business Failures, Other. Those buckets are **Dashboard-only and not exposed via
the API**, so any engine working off the API has to rebuild them. Rebuilding them is table
stakes; the value is in going one step further, to **actionability**.

Attribution says *whose fault it was*. Actionability says *what to do next*, and they are not
the same question. `insufficient_funds` and `card_expired` are both customer-side, but one is
a timing problem that a well-timed silent retry solves for free, and the other can never be
fixed by retrying at all — you must obtain a new instrument first. Retrying a dead card forty
times is the single most common way recovery systems waste money and goodwill.

## Provenance of the vocabulary — stated because gate 0.2 forced it

`error_reason` has ~121 published values. **We could not observe them.** Gate 0.2 established
that driving the test-mode success/failure screen to FAILURE overrides the test card's reason
mapping and returns the generic `payment_failed` every time, so the taxonomy below is
**documentation-derived from Razorpay's published enumeration, not observed in test mode**.
What *was* observed is the field SHAPE: all five fields populate, and `error_source` genuinely
discriminates (`gateway` for a card decline, `issuer` for a wallet decline).
See `results/phase0/0.2c-error-fields.json`.

Unknown reasons fall through to OTHER / RETRY_LATER rather than being guessed at, and are
counted so the unmapped share is reportable instead of invisible.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Actionability, CauseBucket, PaymentFailure

#: Documentation-derived. Keyed on error_reason, refined by error_source where it matters.
#: NEVER keyed on error_code alone: it has only three values, and BAD_REQUEST_ERROR carries
#: most customer-side declines, so classifying on it would put insufficient funds and a
#: malformed request in the same bucket.
_REASON_MAP: dict[str, tuple[CauseBucket, Actionability]] = {
    # --- funds: a timing problem, not a persuasion problem -----------------------------
    "insufficient_funds": (CauseBucket.BANK_FAILURE, Actionability.NEEDS_FUNDS),
    "insufficient_fund": (CauseBucket.BANK_FAILURE, Actionability.NEEDS_FUNDS),  # docs use both
    "payment_failed_due_to_insufficient_balance": (CauseBucket.BANK_FAILURE, Actionability.NEEDS_FUNDS),
    # --- dead or unusable instrument: retrying can NEVER work --------------------------
    "card_expired": (CauseBucket.CUSTOMER_DROP_OFF, Actionability.NEEDS_NEW_INSTRUMENT),
    "card_blocked": (CauseBucket.BANK_FAILURE, Actionability.NEEDS_NEW_INSTRUMENT),
    "card_disabled": (CauseBucket.BANK_FAILURE, Actionability.NEEDS_NEW_INSTRUMENT),
    "invalid_card": (CauseBucket.CUSTOMER_DROP_OFF, Actionability.NEEDS_NEW_INSTRUMENT),
    "mandate_revoked": (CauseBucket.CUSTOMER_DROP_OFF, Actionability.NEEDS_NEW_INSTRUMENT),
    "payment_method_not_available": (CauseBucket.BANK_FAILURE, Actionability.NEEDS_NEW_INSTRUMENT),
    # --- transient: time alone fixes these --------------------------------------------
    "payment_timed_out": (CauseBucket.BANK_FAILURE, Actionability.RETRY_LATER),
    "gateway_technical_error": (CauseBucket.BANK_FAILURE, Actionability.RETRY_LATER),
    "server_error": (CauseBucket.BUSINESS_FAILURE, Actionability.RETRY_LATER),
    "issuer_down": (CauseBucket.BANK_FAILURE, Actionability.RETRY_LATER),
    "network_error": (CauseBucket.BANK_FAILURE, Actionability.RETRY_LATER),
    # --- needs the customer to do something -------------------------------------------
    "payment_cancelled": (CauseBucket.CUSTOMER_DROP_OFF, Actionability.NEEDS_CUSTOMER_ACTION),
    "authentication_failed": (CauseBucket.CUSTOMER_DROP_OFF, Actionability.NEEDS_CUSTOMER_ACTION),
    "incorrect_otp": (CauseBucket.CUSTOMER_DROP_OFF, Actionability.NEEDS_CUSTOMER_ACTION),
    "payment_pending": (CauseBucket.OTHER, Actionability.NEEDS_CUSTOMER_ACTION),
    # --- our own fault, or a hard block: contacting is pure noise ----------------------
    "invalid_request_error": (CauseBucket.BUSINESS_FAILURE, Actionability.DO_NOT_CONTACT),
    "international_transaction_not_allowed": (CauseBucket.BUSINESS_FAILURE, Actionability.DO_NOT_CONTACT),
    "payment_frequency_exceeded": (CauseBucket.BUSINESS_FAILURE, Actionability.DO_NOT_CONTACT),
    "fraudulent_payment": (CauseBucket.OTHER, Actionability.DO_NOT_CONTACT),
}

#: What actually came back in test mode. Gate 0.2 found EVERY manufactured failure returns
#: this, whatever card was used, so it must be handled explicitly rather than treated as an
#: unmapped surprise.
_GENERIC_TEST_MODE_REASON = "payment_failed"


@dataclass(frozen=True)
class Diagnosis:
    bucket: CauseBucket
    actionability: Actionability
    reason: str
    #: True when the reason was not in the published enumeration. Reported rather than hidden:
    #: a diagnosis layer that silently buckets the unknown is lying about its coverage.
    unmapped: bool = False
    #: True when the reason is the generic value test mode returns for every failure.
    generic: bool = False

    @property
    def retryable(self) -> bool:
        """Whether a silent re-attempt of the charge could ever succeed.

        This is the money question. A dead instrument is NOT retryable, and every retry spent
        on one is wasted — which is exactly what a fixed ladder does, because it never asks.
        """
        return self.actionability in (
            Actionability.RETRY_LATER,
            Actionability.NEEDS_FUNDS,
        )

    @property
    def contactable(self) -> bool:
        return self.actionability is not Actionability.DO_NOT_CONTACT


def diagnose(failure: PaymentFailure) -> Diagnosis:
    """Classify on (error_code, error_reason) + error_source, never on error_code alone."""
    reason = (failure.error_reason or "").strip().lower()

    if reason == _GENERIC_TEST_MODE_REASON:
        # Test mode collapses every failure to this. Treat conservatively as retryable-later
        # rather than pretending to know the real cause.
        return Diagnosis(
            bucket=CauseBucket.OTHER,
            actionability=Actionability.RETRY_LATER,
            reason=reason,
            generic=True,
        )

    hit = _REASON_MAP.get(reason)
    if hit is None:
        return Diagnosis(
            bucket=CauseBucket.OTHER,
            actionability=Actionability.RETRY_LATER,
            reason=reason or "unknown",
            unmapped=True,
        )

    bucket, actionability = hit

    # error_source refines attribution where the reason alone is ambiguous. A timeout blamed
    # on `internal` is our problem, not the bank's, and putting it in BANK_FAILURE would
    # quietly flatter the merchant in exactly the report a panel reads.
    if failure.error_source == "internal" and bucket is CauseBucket.BANK_FAILURE:
        bucket = CauseBucket.BUSINESS_FAILURE

    return Diagnosis(bucket=bucket, actionability=actionability, reason=reason)


def coverage(failures: list[PaymentFailure]) -> dict[str, float]:
    """What fraction of a population the taxonomy actually maps.

    Exists so the README can state coverage as a measured number rather than assert the
    taxonomy is comprehensive.
    """
    if not failures:
        return {"mapped": 0.0, "unmapped": 0.0, "generic": 0.0, "n": 0}
    diagnoses = [diagnose(f) for f in failures]
    n = len(diagnoses)
    unmapped = sum(d.unmapped for d in diagnoses)
    generic = sum(d.generic for d in diagnoses)
    return {
        "mapped": round((n - unmapped - generic) / n, 4),
        "unmapped": round(unmapped / n, 4),
        "generic": round(generic / n, 4),
        "n": n,
    }
