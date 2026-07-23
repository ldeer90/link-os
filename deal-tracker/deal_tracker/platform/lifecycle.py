"""Domain lifecycle validation and automatic refresh rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .enums import LifecycleStage


class InvalidLifecycleTransition(ValueError):
    """Raised when code attempts to skip a protected lifecycle step."""


TRANSITIONS: dict[LifecycleStage, frozenset[LifecycleStage]] = {
    LifecycleStage.IMPORTED: frozenset(
        {LifecycleStage.QUEUED, LifecycleStage.SUPPRESSED, LifecycleStage.FAILED}
    ),
    LifecycleStage.QUEUED: frozenset(
        {LifecycleStage.SCRAPING, LifecycleStage.SUPPRESSED, LifecycleStage.FAILED}
    ),
    LifecycleStage.SCRAPING: frozenset(
        {
            LifecycleStage.EMAIL_FOUND,
            LifecycleStage.NO_EMAIL,
            LifecycleStage.FAILED,
            LifecycleStage.SUPPRESSED,
        }
    ),
    LifecycleStage.EMAIL_FOUND: frozenset(
        {
            LifecycleStage.VERIFIED,
            LifecycleStage.OUTREACH_READY,
            LifecycleStage.SUPPRESSED,
            LifecycleStage.FAILED,
        }
    ),
    LifecycleStage.NO_EMAIL: frozenset(
        {LifecycleStage.QUEUED, LifecycleStage.SUPPRESSED, LifecycleStage.LOST}
    ),
    LifecycleStage.FAILED: frozenset(
        {LifecycleStage.QUEUED, LifecycleStage.SUPPRESSED, LifecycleStage.LOST}
    ),
    LifecycleStage.VERIFIED: frozenset(
        {LifecycleStage.OUTREACH_READY, LifecycleStage.SUPPRESSED}
    ),
    LifecycleStage.OUTREACH_READY: frozenset(
        {LifecycleStage.CAMPAIGN_QUEUED, LifecycleStage.SUPPRESSED}
    ),
    LifecycleStage.CAMPAIGN_QUEUED: frozenset(
        {
            LifecycleStage.UPLOADED,
            LifecycleStage.OUTREACH_READY,
            LifecycleStage.SUPPRESSED,
            LifecycleStage.FAILED,
        }
    ),
    LifecycleStage.UPLOADED: frozenset(
        {LifecycleStage.CONTACTED, LifecycleStage.SUPPRESSED, LifecycleStage.FAILED}
    ),
    LifecycleStage.CONTACTED: frozenset(
        {LifecycleStage.REPLIED, LifecycleStage.SUPPRESSED, LifecycleStage.LOST}
    ),
    LifecycleStage.REPLIED: frozenset(
        {
            LifecycleStage.OFFER_APPROVED,
            LifecycleStage.OFFER_REVIEW,
            LifecycleStage.LOST,
        }
    ),
    LifecycleStage.OFFER_REVIEW: frozenset(
        {LifecycleStage.OFFER_APPROVED, LifecycleStage.LOST}
    ),
    LifecycleStage.OFFER_APPROVED: frozenset(
        {LifecycleStage.LISTED, LifecycleStage.OFFER_REVIEW, LifecycleStage.LOST}
    ),
    LifecycleStage.LISTED: frozenset({LifecycleStage.LOST}),
    LifecycleStage.SUPPRESSED: frozenset(),
    LifecycleStage.LOST: frozenset(),
}


NEVER_AUTOMATICALLY_RECONTACT = frozenset(
    {
        LifecycleStage.SUPPRESSED,
        LifecycleStage.CONTACTED,
        LifecycleStage.REPLIED,
        LifecycleStage.OFFER_REVIEW,
        LifecycleStage.OFFER_APPROVED,
        LifecycleStage.LISTED,
        LifecycleStage.LOST,
    }
)


def coerce_stage(value: LifecycleStage | str) -> LifecycleStage:
    return value if isinstance(value, LifecycleStage) else LifecycleStage(value)


def validate_transition(
    current: LifecycleStage | str,
    target: LifecycleStage | str,
    *,
    allow_same: bool = True,
) -> LifecycleStage:
    """Validate and return the target stage.

    Same-stage writes are allowed for idempotent event consumers. Any override
    needed for a manual data repair should be performed by a separate audited
    administrative path rather than weakening this transition graph.
    """

    current_stage = coerce_stage(current)
    target_stage = coerce_stage(target)
    if allow_same and current_stage == target_stage:
        return target_stage
    if target_stage not in TRANSITIONS[current_stage]:
        raise InvalidLifecycleTransition(
            f"cannot transition domain from {current_stage.value!r} to {target_stage.value!r}"
        )
    return target_stage


def is_refresh_eligible(
    stage: LifecycleStage | str,
    *,
    last_attempt_at: datetime | None,
    now: datetime | None = None,
    refresh_after_days: int = 90,
) -> bool:
    """Return whether an unsuccessful domain may be automatically retried."""

    current = coerce_stage(stage)
    if current not in {LifecycleStage.NO_EMAIL, LifecycleStage.FAILED}:
        return False
    if last_attempt_at is None:
        return True
    instant = now or datetime.now(timezone.utc)
    if last_attempt_at.tzinfo is None:
        last_attempt_at = last_attempt_at.replace(tzinfo=timezone.utc)
    return last_attempt_at <= instant - timedelta(days=refresh_after_days)


def can_automatically_contact(stage: LifecycleStage | str) -> bool:
    """Fail closed for any domain that has reached a protected stage."""

    return coerce_stage(stage) not in NEVER_AUTOMATICALLY_RECONTACT
