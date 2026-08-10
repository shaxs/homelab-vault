"""The one place a provider is actually called — v1 §7c.

Deliberately its own module rather than a function in `enrichment.py`: enrichment
guarantees it makes no outbound call, and that guarantee is worth being able to
read off the import list. Only the worker imports this.

**A failed poll is recorded, not discarded.** The previous `payload` is left
intact and `ok`/`detail` carry the reason, because an operator asking "why does
nothing have a location" needs an answer, and a snapshot that merely stops
updating cannot give one. Stale data plus a stated staleness beats no data.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from .. import notifications
from ..models import ProviderSnapshot
from .base import Provider, ProviderUnavailable

logger = logging.getLogger("services.providers.poll")

#: Consecutive failed polls before Discord hears about it (BACKLOG.6). Matches
#: the deadman's "three tolerates one slow cycle plus a retry" reasoning (§5a)
#: rather than inventing a second damping philosophy. A constant, not a
#: settings key, until provider noise proves it needs its own knob.
ALERT_AFTER_FAILURES = 3


def poll_provider(provider: Provider) -> ProviderSnapshot:
    """Fetch the provider's guests and cache them. Never raises.

    "Never raises" is enforced at this boundary, not assumed of the provider
    (audit S6): a decoding surprise the provider did not anticipate — a 200
    carrying a shape `ProviderUnavailable` was never raised for — must still be
    recorded as a failed poll. Before the broad catch below, it escaped to the
    worker's own handler, which logs it **and still beats**, so the snapshot
    stayed `ok=True` with stale data, the deadman stayed happy, and Settings
    showed no failure.
    """
    now = timezone.now()
    row, _ = ProviderSnapshot.objects.get_or_create(
        provider=provider.name, defaults={"fetched_at": now}
    )

    if not provider.is_configured():
        # Not an error state (§7d): no token means no panel, no enrichment, no
        # worker, and everything else identical.
        row.ok = False
        row.detail = "Not configured."
        row.fetched_at = now
        row.save(update_fields=["ok", "detail", "fetched_at"])
        return row

    try:
        status = provider.status()
        if not status.usable:
            raise ProviderUnavailable(status.detail or "Provider is not usable.")
        # The status is handed through so one poll asks `/access/permissions`
        # once, not three times through three TLS handshakes (audit
        # informational).
        guests = provider.guests(status=status)
    except ProviderUnavailable as exc:
        # The message can name an environment variable but never carries its
        # value — `ProxmoxProvider` raises on status codes and exception types,
        # never on the credential itself.
        logger.warning("provider %s poll failed: %s", provider.name, exc)
        return _record_failure(row, now, str(exc))
    except Exception:  # noqa: BLE001 - the docstring's "never raises" is this line
        # A closed string, never the exception text: an unexpected error can
        # quote whatever the remote sent, and this detail reaches the admin
        # settings screen.
        logger.exception("provider %s poll failed unexpectedly", provider.name)
        return _record_failure(
            row, now, "Provider returned an unexpected response; see the worker log."
        )

    infrastructure, infrastructure_detail = _infrastructure(provider, status)

    row.payload = {
        "guests": [
            {
                "foreign_id": guest.foreign_id,
                "location": provider.location_for(guest),
                **guest.as_payload(),
            }
            for guest in guests
        ],
        "status": status.as_dict(),
        # §7d's panel data, in the same row as the guests it describes — one
        # cache, one `fetched_at`, one thing to reason about staleness with.
        "infrastructure": infrastructure,
        "infrastructure_detail": infrastructure_detail,
    }
    row.ok = True
    row.detail = ""
    row.fetched_at = now
    _alert_on_recovery(row)
    row.consec_failures = 0
    row.save(
        update_fields=[
            "payload", "ok", "detail", "fetched_at", "consec_failures",
            "alert_active",
        ]
    )
    logger.info("provider %s cached %s guest(s)", provider.name, len(guests))
    return row


def _record_failure(row: ProviderSnapshot, now, detail: str) -> ProviderSnapshot:
    """One failed poll: previous payload kept, reason recorded, alert damped."""
    row.ok = False
    row.detail = detail
    row.fetched_at = now
    row.consec_failures += 1
    _alert_on_failure(row)
    row.save(
        update_fields=[
            "ok", "detail", "fetched_at", "consec_failures", "alert_active",
        ]
    )
    return row


def _alert_on_failure(row: ProviderSnapshot) -> None:
    """Damped and edge-triggered, like every other alert here (BACKLOG.6).

    One failed poll is a transient, not an incident; alert only when the streak
    reaches `ALERT_AFTER_FAILURES`, and only on the pass that crosses it —
    `alert_active` is what keeps an overnight outage at one message instead of
    one per interval. The deadman never covers this case: it watches *workers*,
    and this worker is alive and beating; it is the cluster that is not.
    """
    if row.consec_failures >= ALERT_AFTER_FAILURES and not row.alert_active:
        row.alert_active = True
        _notify(notifications.provider_failure_message(row.provider, row.consec_failures))


def _alert_on_recovery(row: ProviderSnapshot) -> None:
    """Announce a recovery exactly once, and only after a real alert."""
    if row.alert_active:
        row.alert_active = False
        _notify(notifications.provider_recovery_message(row.provider, row.consec_failures))


def _notify(message: dict) -> None:
    """Discord, if the settings table says so — read live, per pass, the same
    gate the health worker and the deadman honour."""
    from ..settings_store import get_setting

    if get_setting("notifications.discord.enabled"):
        notifications.dispatch(message)


def _infrastructure(provider: Provider, status=None) -> tuple[dict | None, str]:
    """§7d's cluster data, or `None` plus the reason — never an exception.

    **Deliberately fetched after the guests and unable to fail the poll.**
    Enrichment is the product and the panel is decoration: a cluster read that
    403s because the token lacks `Sys.Audit` must cost the nodes table and
    nothing else. Wired the other way round — one call, all-or-nothing — a
    permissions gap on a diagnostic endpoint would silently stop every service
    in the catalog getting a location.

    A provider with no cluster (`None`) and one that could not read its cluster
    (an exception) both leave `infrastructure` null, so the reason is carried
    separately rather than inferred from the absence.
    """
    reader = getattr(provider, "infrastructure", None)
    if reader is None:
        return None, "This provider does not report cluster infrastructure."
    try:
        result = reader(status=status) if status is not None else reader()
    except ProviderUnavailable as exc:
        logger.warning("provider %s infrastructure read failed: %s", provider.name, exc)
        return None, str(exc)
    except Exception:  # noqa: BLE001 - a panel surprise must not cost the guests
        # Same closed-string rule as the poll's own boundary (audit S6).
        logger.exception(
            "provider %s infrastructure read failed unexpectedly", provider.name
        )
        return None, "Provider returned an unexpected response; see the worker log."
    if result is None:
        return None, "This provider does not report cluster infrastructure."
    return result.as_payload(), ""
