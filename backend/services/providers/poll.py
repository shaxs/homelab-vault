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
from .base import CRITICAL_ALERT_LEVELS, Provider, ProviderUnavailable

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
    # Captured before the wholesale payload overwrite below: the edge-triggered
    # alert forwarder diffs this poll's active alerts against the set the
    # *previous* snapshot already notified. Reading it off the row we are about to
    # rewrite is what lets the notified set live in the payload with no migration.
    old_payload = row.payload or {}

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
    report, report_detail = _report(provider, status)

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
        # The optional `report()` capability (TrueNAS pools/disks/system/alerts),
        # cached for the adapter section TN3 renders. `None` for a provider that
        # has no report (Proxmox) or one that could not read it — the reason rides
        # `report_detail`, the same shape as `infrastructure`/`infrastructure_detail`.
        "report": report.as_payload() if report is not None else None,
        "report_detail": report_detail,
        # The edge-trigger state for native provider alerts — the set already
        # forwarded to Discord, so next poll announces only what newly appeared.
        # In the payload, not a column: no migration (done-when).
        "notified_alert_keys": _forward_provider_alerts(provider, report, old_payload),
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


def _report(provider: Provider, status=None):
    """A provider's optional `report()` payload, or `None` plus the reason.

    Mirrors `_infrastructure()` exactly — same reflection (`getattr`), same
    failure discipline, same "a decoration surprise never fails the poll" rule.
    Returns the report **object** (not its payload) so the caller can both
    serialise it and diff its `alerts` for forwarding; the object carries
    `as_payload()`.

    Discovered by reflection rather than the `Provider` Protocol on purpose:
    `report` is not a Protocol method (that would force every provider to define
    it and break the guest-only ones — see `base.py`), so a provider without one
    is `None` here and nothing else.
    """
    reader = getattr(provider, "report", None)
    if reader is None:
        return None, "This provider does not report a status section."
    try:
        result = reader(status=status) if status is not None else reader()
    except ProviderUnavailable as exc:
        logger.warning("provider %s report read failed: %s", provider.name, exc)
        return None, str(exc)
    except Exception:  # noqa: BLE001 - a report surprise must not cost the poll
        logger.exception(
            "provider %s report read failed unexpectedly", provider.name
        )
        return None, "Provider returned an unexpected response; see the worker log."
    if result is None:
        return None, "This provider does not report a status section."
    return result, ""


def _forward_provider_alerts(provider: Provider, report, old_payload: dict) -> list[str]:
    """Edge-triggered Discord forwarding of a provider's own native alerts (TN2).

    The same edge-trigger philosophy as every alert here (BACKLOG.6, the deadman):
    diff this poll's active critical/high alerts against the set the previous
    snapshot already notified, announce only what **newly** appeared, and announce
    once that something cleared. The notified set is carried in the payload — no
    migration.

    Two boundaries this function holds, both from TN1's forward obligation:

    - **The alert text is untrusted.** `formatted` names pools and datasets a
      hostile actor with write access to the box could influence, so it is
      `sanitize()`d inside `provider_alert_message()` before it can reach Discord,
      exactly like a probed page title. `allowed_mentions.parse=[]` holds in
      `build_payload`, so text can never trigger a ping.
    - **The notified set advances on every successful poll, gate or no gate.**
      Whether or not Discord is enabled, the returned set becomes "already
      notified", so switching Discord on mid-alert does not replay every alert
      already standing — it announces the eventual *recovery* of a fall it never
      announced. This is the documented provider-alert latch behaviour (§5,
      AS_BUILT), applied to native alerts.
    """
    already = set(old_payload.get("notified_alert_keys") or [])

    if report is None:
        # A provider with no report() (Proxmox) has no native alerts; its poll
        # failures ride the separate `consec_failures` path. Preserve any prior
        # set rather than clearing it, so the field is inert for such providers.
        return sorted(already)

    critical = [a for a in report.alerts if a.level in CRITICAL_ALERT_LEVELS]
    current_keys = {a.key for a in critical}

    for alert in critical:
        if alert.key not in already:
            _notify(notifications.provider_alert_message(provider.name, alert))
    if already - current_keys:
        _notify(
            notifications.provider_alert_cleared_message(
                provider.name, len(already - current_keys)
            )
        )
    return sorted(current_keys)
