"""Stored health transitions — v0 §8, amended by v1 §5.

    consecutive failures >= fail threshold      -> down
    consecutive successes >= recovery threshold -> up   (when recovering)
    never checked                               -> unknown

**v0 §8 said "any success -> up immediately"; v1 §5 replaces that with damping in
both directions.** Down-transitions were always damped by `fail_threshold`; an
undamped up-transition means one flapping service alternates down/up/down/up and
floods the Discord channel until somebody mutes it — which mutes the real alerts
with it. AS_BUILT §3 records the amendment.

**The damping applies to a recovery, not to first contact.** A service whose
stored status is `unknown` has never been checked, so there is no flap to damp:
it goes `up` on its first success, as it always did. Holding it at `unknown` for
two cycles would make a bulk import look broken for as long as the recovery
threshold says, and would buy nothing — nothing is notified on first contact
either, because "is back up" is not true of a service that was never down.

`stale` is deliberately absent from all of this: it is derived on read (see
`Service.effective_status`), because the condition that produces it is the worker
being dead, and a dead worker cannot write it.

Thresholds come from a `HealthPolicy` (see `policy.py`) — per-service when
pinned, global otherwise, re-read live. Callers that omit it get a live read.
"""

from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from ..models import Service
from .policy import current_policy


@dataclass(frozen=True)
class Applied:
    """The outcome of one check result.

    `changed` goes to `save(update_fields=...)` so the worker cannot clobber a
    concurrent catalog edit with the copy it loaded at the start of the pass.
    `transition` is the notification trigger — `None` for the overwhelming
    majority of checks, which is why notification is edge-driven rather than
    something the caller has to diff for itself.
    """

    changed: list[str] = field(default_factory=list)
    #: "up" or "down" when this result moved the stored status; otherwise None.
    transition: str | None = None
    #: Status held before this result. Lets a caller tell recovery from first
    #: contact without re-reading the row.
    previous: str | None = None
    #: `health_changed_at` as it stood before this result — on a recovery that
    #: is the moment the service went down, which is what lets the notification
    #: say "was down for 14 minutes" without a history table.
    previous_changed_at: datetime | None = None


def apply_result(service: Service, ok: bool, now=None, policy=None) -> Applied:
    """Mutate `service` in place for one check result."""
    now = now or timezone.now()
    policy = policy or current_policy()
    previous = service.health_status
    previous_changed_at = service.health_changed_at
    changed = [
        "health_checked_at",
        "health_consec_failures",
        "health_consec_successes",
        "health_status",
    ]
    service.health_checked_at = now

    if ok:
        service.health_consec_failures = 0
        service.health_consec_successes += 1
        new_status = _status_after_success(service, policy)
    else:
        service.health_consec_successes = 0
        service.health_consec_failures += 1
        if service.health_consec_failures >= policy.fail_threshold_for(service):
            new_status = Service.HealthStatus.DOWN
        else:
            # Below threshold: hold the previous verdict rather than flapping.
            new_status = service.health_status

    if new_status != service.health_status:
        service.health_status = new_status
        service.health_changed_at = now
        changed.append("health_changed_at")
        return Applied(
            changed=changed,
            transition=new_status,
            previous=previous,
            previous_changed_at=previous_changed_at,
        )
    return Applied(
        changed=changed,
        transition=None,
        previous=previous,
        previous_changed_at=previous_changed_at,
    )


def _status_after_success(service: Service, policy) -> str:
    if service.health_status == Service.HealthStatus.DOWN:
        # A recovery — the flap case, so damp it.
        if service.health_consec_successes >= policy.recovery_threshold_for(service):
            return Service.HealthStatus.UP
        return service.health_status
    # First contact, or already up. Nothing to damp.
    return Service.HealthStatus.UP


def is_due(service: Service, now=None, policy=None) -> bool:
    """Per-service intervals are honored (§8) — the worker must not sweep
    everything on a fixed cycle. A service with no interval of its own follows
    the global setting rather than a stamped copy of it."""
    if not service.health_enabled:
        return False
    if service.health_checked_at is None:
        return True
    now = now or timezone.now()
    interval = service.effective_interval_s(policy)
    return (now - service.health_checked_at).total_seconds() >= interval


def should_notify(service: Service, applied: Applied) -> bool:
    """Whether this transition is worth a message (§4, §5).

    Two gates and they are not the same gate. `notify_enabled=False` silences a
    service that is still fully checked and still renders red on the dashboard —
    that separation is the entire point of the flag. And a move to `up` is only
    announced when it is a recovery: "is back up" is a lie about a service that
    was never down, and first contact after an import would otherwise post once
    per imported service.
    """
    if applied.transition is None or not service.notify_enabled:
        return False
    if applied.transition == Service.HealthStatus.DOWN:
        return True
    return applied.previous == Service.HealthStatus.DOWN
