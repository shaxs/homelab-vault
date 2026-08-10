"""Matching services to provider guests — v1 §7b, §4a.

**Everything here reads the cached snapshot; nothing here calls a provider.**
That is what makes §7c's *"Proxmox being unreachable must never block a catalog
write or a probe"* a structural property rather than a defensive one: there is
no outbound call on the request path to time out, and therefore no timeout to
tune and no `try/except` for a future edit to forget. The worker owns the only
network call; see `management/commands/proxmox_worker.py`.

**The identity rule (§7b): match by address once, then refresh by foreign ID.**
A service with no link is matched against guest addresses. From then on the link
is refreshed by its `ref` (the VMID), which is stable across migration, and the
address is never consulted for that service again. Re-matching by address every
poll would drop links that were already correct the moment a guest took a new
DHCP lease — the exact failure §7b names.

**The pin rule (§4a): `host_label` is the operator's, and enrichment never
writes it.** Derived location lives on the link; the pinned value lives on the
service; the API returns both and the UI shows the disagreement. Nothing in this
module writes to `Service`, which is the strongest form of "a warning is not a
substitute for not destroying data" — there is no code path that could.
"""

from __future__ import annotations

import ipaddress
import logging
from collections import defaultdict
from urllib.parse import urlsplit

from django.utils import timezone

from .models import ProviderSnapshot, Service, ServiceLink

logger = logging.getLogger("services.enrichment")


# --- Address index ---------------------------------------------------------


def build_address_index(guests: list[dict]) -> dict[str, dict]:
    """Map address -> guest, **dropping every address more than one guest has.**

    This is not defensive coding; it is the difference between enrichment and a
    random-guest generator. Observed on the reference cluster 2026-08-06: every
    container running Docker reports `172.17.0.1` — the default `docker0`
    gateway — so eleven guests claimed the same address. `172.16.0.0/12` is
    inside `PROBE_ALLOWED_NETWORKS`, so such an address is a legal service URL,
    and a last-writer-wins dict would have answered "this service lives on
    whichever guest happened to be enumerated last."

    Dropping the ambiguous address is right rather than merely safe: an address
    that identifies eleven guests identifies none of them, and no location at
    all is honest where a wrong one is not. Tailnet (CGNAT) addresses survive
    because they *are* unique per guest.
    """
    holders: dict[str, list[dict]] = defaultdict(list)
    for guest in guests:
        for address in guest.get("addresses") or []:
            holders[address].append(guest)

    index: dict[str, dict] = {}
    for address, owners in holders.items():
        if len(owners) == 1:
            index[address] = owners[0]
        else:
            logger.debug(
                "address %s claimed by %s guests — not usable for matching",
                address,
                len(owners),
            )
    return index


def ambiguous_addresses(guests: list[dict]) -> dict[str, int]:
    """Addresses shared by more than one guest, and how many claim each.

    Exposed for the settings/diagnostics surface: "why did nothing match" is
    otherwise unanswerable, and this is usually the answer.
    """
    holders: dict[str, int] = defaultdict(int)
    for guest in guests:
        for address in guest.get("addresses") or []:
            holders[address] += 1
    return {a: n for a, n in holders.items() if n > 1}


# --- Service addresses -----------------------------------------------------


def service_addresses(service: Service, resolve: bool = True) -> list[str]:
    """Candidate addresses for a service, best first.

    §7b matches "the resolved address", so a hostname is looked up — that is
    what lets a tailnet-named service match the guest it runs on. An IP literal
    needs no lookup and never gets one. Resolution failure is not an error here:
    a service whose name does not resolve simply gets no location.
    """
    candidates: list[str] = []
    for url in (service.probe_url, service.launch_url):
        host = urlsplit(url or "").hostname
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if resolve:
                candidates.extend(_resolve_quietly(host))
            continue
        if host not in candidates:
            candidates.append(host)
    return candidates


def preview_location(url: str, *, resolve: bool = True) -> dict:
    """What location a service at `url` **would** get, without creating one.

    For the add form: the Location input is the *pinned* value, so pre-filling it
    would pin every service at creation and the derived value would never track
    again — the treadmill §4a's correction removes. Showing the answer beside the
    probe result instead means the field can stay empty and still not look
    broken, which is the actual complaint.

    Same shape as `/slug-preview`: let the UI show what the server will derive
    rather than reimplementing the rule. Reads the cached snapshot only — no
    provider call, so this stays on the request path safely (§7c).

    Returns `location: None` for every honest "no location" case — nothing
    cached, no guest at that address, or an address held by more than one guest
    — because each of those is exactly what the service will show after saving.
    """
    snapshot = ProviderSnapshot.objects.filter(provider="proxmox").first()
    if snapshot is None or not snapshot.ok:
        return {"location": None, "provider": None}

    guests = snapshot.guests
    index = build_address_index(guests)
    for address in _url_addresses(url, resolve=resolve):
        guest = index.get(address)
        if guest is not None:
            return {"location": guest.get("location") or None, "provider": "proxmox"}
    return {"location": None, "provider": None}


def _url_addresses(url: str, *, resolve: bool) -> list[str]:
    """Candidate addresses for a bare URL — `service_addresses` without a row.

    Deliberately mirrors that function rather than sharing a branch with it: this
    one runs on a request path and takes a string, that one runs in the worker
    and takes a `Service`. Keeping them separate is what stops a later edit to
    the worker's matching quietly changing what the add form promises.
    """
    host = urlsplit(url or "").hostname
    if not host:
        return []
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return _resolve_quietly(host) if resolve else []
    return [host]


def _resolve_quietly(hostname: str) -> list[str]:
    from .probe import ProbeRejected, resolve_host

    try:
        return resolve_host(hostname)
    except ProbeRejected:
        return []
    except Exception:  # noqa: BLE001 - DNS must never break an enrichment pass
        logger.debug("could not resolve %s", hostname, exc_info=True)
        return []


# --- The pass --------------------------------------------------------------


def refresh_links(provider_name: str, *, resolve: bool = True) -> dict:
    """Refresh every link for one provider from its cached snapshot.

    Returns a summary the worker logs and the validation checklist reads.
    Never raises for provider reasons — an absent or failed snapshot means
    "nothing to do", not an error, because the catalog must keep working with
    Proxmox switched off entirely (§7d).
    """
    snapshot = ProviderSnapshot.objects.filter(provider=provider_name).first()
    if snapshot is None or not snapshot.ok:
        return {
            "provider": provider_name,
            "skipped": True,
            "reason": (snapshot.detail if snapshot else "no snapshot yet"),
            "linked": 0,
            "refreshed": 0,
            "matched": 0,
        }

    guests = snapshot.guests
    by_ref = {str(g.get("foreign_id")): g for g in guests}
    index = build_address_index(guests)
    now = timezone.now()

    existing = {
        link.service_id: link
        for link in ServiceLink.objects.filter(provider=provider_name)
    }

    refreshed = matched = unlinked = 0
    for service in Service.objects.all():
        link = existing.get(service.id)
        if link is not None:
            # Refresh by foreign ID. The address is never reconsulted for a
            # service that already has a link (§7b).
            guest = by_ref.get(str(link.ref))
            if guest is None:
                # The provider answered and this guest was not in the answer
                # (audit S8). Counted per successful pass, never per failed
                # one; at the bound the link is dropped — the provider itself
                # kept saying the guest no longer exists.
                if _miss(link):
                    unlinked += 1
                continue
            if link.missed_passes > 0 and not _same_guest(link, guest):
                # The VMID came back wearing a different identity: Proxmox
                # reuses freed IDs, so after a gap the ref alone is not proof
                # (audit S8). Drop the link and fall through to address
                # matching — the corroboration that creates links in the
                # first place. Blindly reattaching here rewrote the service's
                # node, name and addresses to an unrelated guest.
                link.delete()
                unlinked += 1
            else:
                if _apply(link, guest, now):
                    refreshed += 1
                continue

        guest = _first_match(service, index, resolve=resolve)
        if guest is None:
            continue
        link = ServiceLink(
            service=service, provider=provider_name, kind=guest.get("kind") or "",
            ref=str(guest.get("foreign_id")),
        )
        _apply(link, guest, now)
        matched += 1

    return {
        "provider": provider_name,
        "skipped": False,
        "reason": "",
        "linked": ServiceLink.objects.filter(provider=provider_name).count(),
        "refreshed": refreshed,
        "matched": matched,
        "unlinked": unlinked,
    }


def _first_match(service: Service, index: dict[str, dict], *, resolve: bool):
    for address in service_addresses(service, resolve=resolve):
        guest = index.get(address)
        if guest is not None:
            return guest
    return None


def _miss(link: ServiceLink) -> bool:
    """One successful pass without this guest. True when the link was dropped.

    `last_seen_at` is deliberately untouched — it answers "when was the guest
    last confirmed", and a miss is not a confirmation of anything.
    """
    link.missed_passes += 1
    if link.missed_passes >= ServiceLink.UNLINK_AFTER_MISSES:
        link.delete()
        return True
    link.save(update_fields=["missed_passes", "updated_at"])
    return False


def _same_guest(link: ServiceLink, guest: dict) -> bool:
    """Is the guest wearing this link's ref still the guest the link meant?

    Only consulted after a gap (`missed_passes > 0`) — an uninterrupted link
    tracks by ref alone, which is what survives DHCP churn (§7b). After a
    gap, name equality or any shared address is accepted as corroboration: a
    rebuilt container keeps its name, a re-addressed one keeps its name, a
    migrated one keeps both — while a VMID recycled onto an unrelated guest
    matches neither, which is exactly the case that must not reattach.
    """
    stored = link.payload or {}
    stored_name = (stored.get("name") or "").strip()
    new_name = (guest.get("name") or "").strip()
    if stored_name and new_name and stored_name == new_name:
        return True
    stored_addresses = set(stored.get("addresses") or [])
    new_addresses = set(guest.get("addresses") or [])
    return bool(stored_addresses & new_addresses)


def _apply(link: ServiceLink, guest: dict, now) -> bool:
    """Write the guest's current state onto the link. Always stamps `last_seen_at`.

    `last_seen_at` moves on every pass that saw the guest, even when nothing
    else changed — that is precisely what distinguishes it from `updated_at`,
    and what lets a link that has gone quiet be told from one that is simply
    stable.
    """
    payload = dict(link.payload or {})
    before = dict(payload)
    payload.update(
        {
            "node": guest.get("node") or "",
            "name": guest.get("name") or "",
            "kind": guest.get("kind") or "",
            "addresses": list(guest.get("addresses") or []),
            "running": bool(guest.get("running", True)),
            "location": guest.get("location") or "",
        }
    )
    changed = payload != before or link.kind != (guest.get("kind") or "")
    link.payload = payload
    link.kind = guest.get("kind") or link.kind
    link.last_seen_at = now
    # Seen means seen: any run of misses ends here (audit S8).
    link.missed_passes = 0
    link.save()
    return changed
