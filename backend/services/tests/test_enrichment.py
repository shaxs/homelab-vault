"""Matching, refreshing, and manual-vs-derived location — v1 §7b, §4a.

Three behaviours carry this chunk and each has a test that fails loudly without
its fix:

1. **Refresh by foreign ID, never re-match by address** — otherwise a DHCP lease
   change silently drops a link that was already correct.
2. **A guest that migrates re-derives with no manual action** — the entire
   reason location lives in `service_links` rather than as a string on
   `services`.
3. **A pin is never overwritten** — §4a, and the strongest available form of it
   is that nothing in `enrichment.py` writes to `Service` at all.
"""

import pytest
from django.utils import timezone

from services import enrichment
from services.models import ProviderSnapshot, Service, ServiceLink
from services.providers import ProxmoxProvider
from services.providers.poll import poll_provider

from .proxmox_fakes import FakeCluster, guest, install

pytestmark = pytest.mark.django_db


@pytest.fixture
def provider(settings):
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=hlv@pve!readonly=secret-uuid"
    return ProxmoxProvider()


def make_service(slug="media", url="http://192.168.0.125:8989/", **kwargs):
    return Service.objects.create(slug=slug, name=slug, launch_url=url, **kwargs)


def refresh(resolve=False):
    return enrichment.refresh_links("proxmox", resolve=resolve)


def cache(cluster):
    """Write a snapshot without going through a provider."""
    p = ProxmoxProvider()
    ProviderSnapshot.objects.update_or_create(
        provider="proxmox",
        defaults={
            "ok": True,
            "detail": "",
            "fetched_at": timezone.now(),
            "payload": {
                "guests": [
                    {
                        "foreign_id": str(g["vmid"]),
                        "location": f"{g['node']} · "
                        f"{'CT' if g['kind'] == 'lxc' else 'VM'} {g['vmid']}",
                        "node": g["node"],
                        "kind": g["kind"],
                        "name": g["name"],
                        "addresses": list(g["addresses"]),
                        "running": g["running"],
                    }
                    for g in cluster.guests
                ]
            },
        },
    )
    return p


# --- The address index -----------------------------------------------------


def test_an_address_held_by_two_guests_is_not_usable_for_matching():
    """**Not defensive coding.** On the real cluster `172.17.0.1` — the default
    `docker0` gateway — was claimed by 29 of 37 guests, and `172.16.0.0/12` is
    inside `PROBE_ALLOWED_NETWORKS`, so it is a legal service URL. A
    last-writer-wins dict would answer "this service lives on whichever guest
    was enumerated last".
    """
    guests = [
        {"foreign_id": "1", "addresses": ["192.168.0.10", "172.17.0.1"]},
        {"foreign_id": "2", "addresses": ["192.168.0.11", "172.17.0.1"]},
    ]
    index = enrichment.build_address_index(guests)
    assert index["192.168.0.10"]["foreign_id"] == "1"
    assert index["192.168.0.11"]["foreign_id"] == "2"
    assert "172.17.0.1" not in index
    assert enrichment.ambiguous_addresses(guests) == {"172.17.0.1": 2}


def test_a_unique_address_survives_even_in_a_shared_range():
    """Tailnet (CGNAT) addresses are unique per guest and must not be collateral
    damage of the ambiguity rule."""
    guests = [{"foreign_id": "1", "addresses": ["100.65.201.97", "172.17.0.1"]}]
    assert "100.65.201.97" in enrichment.build_address_index(guests)


# --- Matching --------------------------------------------------------------


def test_a_service_is_matched_to_its_guest_and_the_vmid_is_persisted(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    svc = make_service()

    summary = refresh()

    link = ServiceLink.objects.get(service=svc)
    assert summary["matched"] == 1
    assert link.provider == "proxmox"
    assert link.ref == "105"          # the VMID, not the address (§7b)
    assert link.kind == "lxc"
    assert link.location == "pve-c · CT 105"
    assert link.node == "pve-c"
    assert link.last_seen_at is not None


def test_a_service_on_an_ambiguous_address_gets_no_link(provider, monkeypatch):
    """No location is honest where a wrong one is not."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    make_service(slug="docker", url="http://172.17.0.1:9000/")

    refresh()

    assert ServiceLink.objects.count() == 0


def test_a_service_outside_the_cluster_gets_no_link(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    make_service(slug="nas", url="http://192.168.0.250:5000/")

    refresh()

    assert ServiceLink.objects.count() == 0


def test_the_health_url_is_preferred_over_the_launch_url():
    """`probe_url` is what actually gets contacted, so it is what location should
    follow when the two differ."""
    cache(FakeCluster())
    make_service(
        slug="split",
        url="http://192.168.0.250:80/",
        health_url="http://192.168.0.108:8080/health",
    )
    refresh()
    assert ServiceLink.objects.get().ref == "117"


# --- Refresh by foreign ID (§7b) -------------------------------------------


def test_a_link_is_refreshed_by_vmid_and_survives_the_address_changing(provider, monkeypatch):
    """**The rule §7b states and the reason for it.** VMID is stable across
    migration; the address is not. Re-matching by address every poll drops links
    that were already correct the moment DHCP hands out a new lease.

    Confirmed red by making `refresh_links` re-match on address: the link is
    deleted-and-not-recreated and this assertion fails on `ServiceLink.count()`.
    """
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    svc = make_service()
    refresh()

    # Same guest, new lease. Nothing in the catalog changed.
    moved = FakeCluster(
        guests=[
            guest(105, "pve-c", "media-services", addresses=("192.168.0.199",)),
        ]
    )
    cache(moved)
    summary = refresh()

    link = ServiceLink.objects.get(service=svc)
    assert ServiceLink.objects.count() == 1
    assert link.ref == "105"
    assert summary["refreshed"] == 1
    assert link.payload["addresses"] == ["192.168.0.199"]


def test_a_guest_that_migrates_between_nodes_re_derives_with_no_manual_action(provider, monkeypatch):
    """§7b: *"When a guest moves from pve-c to pve-d, the next poll sees it
    on the new node and the link updates. No re-probe, no manual step."* This is
    the entire reason location lives in `service_links`."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    svc = make_service()
    refresh()
    assert ServiceLink.objects.get(service=svc).location == "pve-c · CT 105"

    cache(FakeCluster(guests=[guest(105, "pve-d", "media-services",
                                    addresses=("192.168.0.125",))]))
    refresh()

    link = ServiceLink.objects.get(service=svc)
    assert link.location == "pve-d · CT 105"
    assert link.node == "pve-d"
    # And nothing was written to the service itself.
    svc.refresh_from_db()
    assert svc.host_label is None


def test_a_stopped_guest_keeps_its_link(provider, monkeypatch):
    """4 of 36 containers on the real cluster were stopped and reported no
    address. A stopped guest is not a deleted one."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    svc = make_service(slug="archived", url="http://192.168.0.108/")
    refresh()
    assert ServiceLink.objects.get(service=svc).ref == "117"

    cache(FakeCluster(guests=[guest(117, "pve-c", "leantime", addresses=(),
                                    running=False)]))
    refresh()

    link = ServiceLink.objects.get(service=svc)
    assert link.ref == "117"
    assert link.payload["running"] is False
    assert link.last_seen_at is not None


def test_last_seen_at_moves_on_every_pass_even_when_nothing_changed(provider, monkeypatch):
    """This is exactly what distinguishes it from `updated_at`: a guest that
    vanishes stops being seen without anything about the row changing."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    make_service()
    refresh()
    first = ServiceLink.objects.get().last_seen_at

    refresh()
    assert ServiceLink.objects.get().last_seen_at > first


# --- A link the provider stops confirming (audit S8) ------------------------
#
# `last_seen_at` was written on every pass and read by NOTHING, so a destroyed
# guest kept its location in the anonymous catalog indefinitely — the exact
# "copied string goes stale silently" failure service_links exists to prevent,
# reintroduced one level up. Staleness is counted in successful passes, the
# same 3× philosophy as everything else here.


def _linked_service(cluster=None, slug="leantime", url="http://192.168.0.108/"):
    """A service linked to CT 117 by a real refresh pass."""
    cluster = cluster or FakeCluster(
        guests=[guest(117, "pve-c", "leantime", addresses=("192.168.0.108",))]
    )
    cache(cluster)
    service = make_service(slug=slug, url=url)
    refresh()
    return service, ServiceLink.objects.get(service=service)


def test_a_missing_guest_marks_the_link_stale_after_three_passes():
    service, link = _linked_service()
    cache(FakeCluster(guests=[]))  # the guest is gone; polls keep succeeding

    for expected_misses in (1, 2):
        refresh()
        link.refresh_from_db()
        assert link.missed_passes == expected_misses
        assert link.location_stale is False

    refresh()
    link.refresh_from_db()
    assert link.location_stale is True
    # Stale is said, not resolved: the derived value is still served (§4a's
    # philosophy applied to time).
    assert link.location == "pve-c · CT 117"


def test_the_api_reports_location_stale(client):
    service, link = _linked_service()
    ServiceLink.objects.filter(pk=link.pk).update(
        missed_passes=ServiceLink.STALE_AFTER_MISSES
    )

    body = client.get("/api/v1/services/leantime").json()

    assert body["location_stale"] is True
    assert body["location_derived"] == "pve-c · CT 117"


def test_location_stale_is_false_for_a_tracking_link(client):
    _linked_service()
    body = client.get("/api/v1/services/leantime").json()
    assert body["location_stale"] is False


def test_a_failed_poll_does_not_count_toward_staleness():
    """The counter moves only when the provider actually answered: a worker
    outage or a cluster outage says nothing about any single guest."""
    service, link = _linked_service()
    ProviderSnapshot.objects.filter(provider="proxmox").update(
        ok=False, detail="cluster down"
    )

    for _ in range(5):
        refresh()

    link.refresh_from_db()
    assert link.missed_passes == 0


def test_a_returning_guest_resets_the_miss_counter():
    service, link = _linked_service()
    cache(FakeCluster(guests=[]))
    refresh()
    link.refresh_from_db()
    assert link.missed_passes == 1

    cache(FakeCluster(
        guests=[guest(117, "pve-c", "leantime", addresses=("192.168.0.108",))]
    ))
    refresh()

    link.refresh_from_db()
    assert link.missed_passes == 0


def test_a_guest_missing_long_enough_is_unlinked():
    service, link = _linked_service()
    ServiceLink.objects.filter(pk=link.pk).update(
        missed_passes=ServiceLink.UNLINK_AFTER_MISSES - 1
    )
    cache(FakeCluster(guests=[]))

    summary = refresh()

    assert summary["unlinked"] == 1
    assert not ServiceLink.objects.filter(service=service).exists()


def test_an_unlinked_service_rejoins_the_address_match_pool():
    """After the unlink, the service is matched fresh — by address, the
    corroboration that creates links — the next time a guest answers there."""
    service, link = _linked_service()
    ServiceLink.objects.filter(pk=link.pk).update(
        missed_passes=ServiceLink.UNLINK_AFTER_MISSES - 1
    )
    cache(FakeCluster(guests=[]))
    refresh()
    assert not ServiceLink.objects.filter(service=service).exists()

    # A NEW guest (different VMID) now owns the address.
    cache(FakeCluster(
        guests=[guest(220, "pve-b", "leantime-v2", addresses=("192.168.0.108",))]
    ))
    refresh()

    fresh = ServiceLink.objects.get(service=service)
    assert fresh.ref == "220"


def test_a_reused_vmid_with_a_different_identity_does_not_reattach():
    """Proxmox reuses freed VMIDs. After a gap, a guest wearing the old ref
    but matching neither the stored name nor any stored address is an
    unrelated guest — blindly refreshing rewrote the service's node, name and
    addresses to it with no warning (audit S8, Codex's half)."""
    service, link = _linked_service()
    cache(FakeCluster(guests=[]))
    refresh()  # one miss — the gap

    imposter = guest(117, "pve-a", "unrelated-new-thing", addresses=("192.168.0.201",))
    cache(FakeCluster(guests=[imposter]))
    summary = refresh()

    assert summary["unlinked"] == 1
    # No link survives: the imposter's address does not match the service
    # either, so the service honestly has no location now.
    assert not ServiceLink.objects.filter(service=service).exists()


def test_a_rebuilt_guest_with_the_same_name_reattaches():
    """A container destroyed and recreated keeps its VMID and name but not its
    lease — that IS the same service, and the name corroborates it."""
    service, link = _linked_service()
    cache(FakeCluster(guests=[]))
    refresh()  # the gap

    rebuilt = guest(117, "pve-c", "leantime", addresses=("192.168.0.150",))
    cache(FakeCluster(guests=[rebuilt]))
    refresh()

    link.refresh_from_db()
    assert link.missed_passes == 0
    assert link.payload["addresses"] == ["192.168.0.150"]


def test_an_uninterrupted_link_still_tracks_by_ref_alone():
    """The §7b identity rule is untouched for a link with no gap: a new DHCP
    lease AND a migration in one poll still refresh by VMID, no corroboration
    demanded — corroboration is only for links the provider stopped
    confirming."""
    service, link = _linked_service()

    moved = guest(117, "pve-b", "leantime", addresses=("192.168.0.177",))
    cache(FakeCluster(guests=[moved]))
    refresh()

    link.refresh_from_db()
    assert link.payload["node"] == "pve-b"
    assert link.payload["addresses"] == ["192.168.0.177"]


# --- §4a: manual pins ------------------------------------------------------


def test_enrichment_never_overwrites_a_typed_location(provider, monkeypatch):
    """§4a: *"If a value will be overwritten, do not overwrite it — a warning is
    not a substitute for not destroying data."*"""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    svc = make_service(host_label="Synology DS923+")

    refresh()

    svc.refresh_from_db()
    assert svc.host_label == "Synology DS923+"
    assert ServiceLink.objects.get(service=svc).location == "pve-c · CT 105"


def test_enrichment_writes_nothing_to_the_service_table(provider, monkeypatch):
    """The structural form of the rule above: there is no code path that could
    destroy a pin, because nothing here saves a `Service`."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    svc = make_service(host_label="pinned", category="cat", description="d")
    before = Service.objects.filter(pk=svc.pk).values().first()

    refresh()

    assert Service.objects.filter(pk=svc.pk).values().first() == before


def test_unpinned_is_the_default_for_anything_enrichment_discovered():
    """§4a, from the prose-only sweep. A discovered service has no pin."""
    cache(FakeCluster())
    svc = make_service()
    refresh()
    svc.refresh_from_db()
    assert svc.host_label is None
    assert ServiceLink.objects.get(service=svc).location == "pve-c · CT 105"


# --- Provider unreachable never blocks (§7c) -------------------------------


def test_refresh_with_no_snapshot_is_a_no_op_not_an_error():
    make_service()
    summary = enrichment.refresh_links("proxmox", resolve=False)
    assert summary["skipped"] is True
    assert summary["matched"] == 0
    assert ServiceLink.objects.count() == 0


def test_refresh_against_a_failed_snapshot_leaves_existing_links_alone(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    make_service()
    refresh()

    ProviderSnapshot.objects.filter(provider="proxmox").update(
        ok=False, detail="cluster down"
    )
    summary = enrichment.refresh_links("proxmox", resolve=False)

    assert summary["skipped"] is True
    assert "cluster down" in summary["reason"]
    assert ServiceLink.objects.count() == 1   # not deleted, just not refreshed


def test_enrichment_makes_no_outbound_call(provider, monkeypatch):
    """§7c made structural: request paths read the cache, so there is no timeout
    to tune and no try/except for a future edit to forget."""
    cluster = install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    make_service()
    before = len(cluster.requests)

    refresh()

    assert len(cluster.requests) == before


# --- The API surface (§4a) -------------------------------------------------


def get_service(client, slug="media"):
    return client.get(f"/api/v1/services/{slug}").json()


def test_the_api_publishes_the_derived_location_and_its_provider(client):
    cache(FakeCluster())
    make_service()
    refresh()

    body = get_service(client)
    assert body["location_derived"] == "pve-c · CT 105"
    assert body["location_provider"] == "proxmox"
    assert body["host_label"] is None
    assert body["location_disagrees"] is False


def test_an_unmatched_service_reports_null_rather_than_an_empty_string(client):
    """Null is "nothing matched"; an empty string would render as a blank value
    that looks like a location we failed to display."""
    make_service(slug="nas", url="http://192.168.0.250:5000/")

    body = get_service(client, "nas")
    assert body["location_derived"] is None
    assert body["location_provider"] is None
    assert body["location_disagrees"] is False


def test_a_pin_that_agrees_with_the_derived_value_is_not_a_disagreement(client):
    """Otherwise every correctly-pinned service nags forever, and the badge
    stops meaning anything."""
    cache(FakeCluster())
    make_service(host_label="pve-c · CT 105")
    refresh()

    assert get_service(client)["location_disagrees"] is False


def test_a_pin_that_differs_from_the_derived_value_is_surfaced(client):
    """§4a: *"Disagreement is shown, not resolved."*"""
    cache(FakeCluster())
    make_service(host_label="pve-old · CT 105")
    refresh()

    body = get_service(client)
    assert body["location_disagrees"] is True
    assert body["host_label"] == "pve-old · CT 105"      # pinned, untouched
    assert body["location_derived"] == "pve-c · CT 105"  # derived, alongside


def test_a_pin_with_no_derived_value_is_not_a_disagreement(client):
    """Nothing to disagree with. A service on hardware outside the cluster is
    the normal case for this, not an error."""
    make_service(slug="nas", url="http://192.168.0.250:5000/", host_label="Synology")

    assert get_service(client, "nas")["location_disagrees"] is False


def test_unpinning_through_the_existing_patch_endpoint_lets_the_value_track(
    client, admin_headers
):
    """§4a's 2026-08-03 correction: the surviving affordance is **unpin**, and it
    needs **no new endpoint** — `PATCH /services/{slug}` already clears it.

    There is deliberately no "accept": accept writes the derived value into the
    pin, which re-pins, so the pair diverges again on the next migration and the
    operator accepts forever.
    """
    cache(FakeCluster())
    svc = make_service(host_label="pve-old · CT 105")
    refresh()
    assert get_service(client)["location_disagrees"] is True

    response = client.patch(
        f"/api/v1/services/{svc.slug}",
        data={"host_label": None},
        content_type="application/json",
        **admin_headers,
    )

    assert response.status_code == 200
    body = get_service(client)
    assert body["host_label"] is None
    assert body["location_disagrees"] is False
    assert body["location_derived"] == "pve-c · CT 105"  # now tracking


def test_the_catalog_serialises_links_without_a_query_per_service(
    client, django_assert_num_queries
):
    """The prefetch is load-bearing: three location fields resolve off
    `obj.links`, and the catalog is polled every 15 seconds."""
    cache(FakeCluster())
    for n, ip in enumerate(("192.168.0.125", "192.168.0.136", "192.168.0.108")):
        make_service(slug=f"svc{n}", url=f"http://{ip}/")
    refresh()

    with django_assert_num_queries(3):
        # services + prefetched links + one settings read for the health policy
        assert len(client.get("/api/v1/services").json()) == 3


# --- The settings screen's provider block (§7a, §6a) -----------------------


def test_the_settings_screen_reports_a_blind_token_as_unusable(
    client, admin_headers, provider, monkeypatch
):
    """The operator-facing half of the finding: "configured" alone would render
    a green tick over a provider that can see nothing."""
    install(monkeypatch, provider, FakeCluster(privileges={}))
    poll_provider(provider)

    (row,) = client.get("/api/v1/settings", **admin_headers).json()["providers"]
    assert row["configured"] is True
    assert row["usable"] is False
    assert "PVEAuditor" in row["detail"]


def test_the_settings_screen_reports_ambiguous_addresses(
    client, admin_headers, provider, monkeypatch
):
    """"Why did nothing match" is otherwise unanswerable from the UI, and the
    answer is almost always Docker bridge gateways."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)

    (row,) = client.get("/api/v1/settings", **admin_headers).json()["providers"]
    assert row["guest_count"] == 6
    assert row["ambiguous_address_count"] == 1  # 172.17.0.1, on two guests


def test_the_settings_screen_reports_the_credentials_privileges(
    client, admin_headers, provider, monkeypatch
):
    """Audit informational: `ProviderStatus.privileges` was computed and
    asserted since V1.4 and serialised nowhere. It pairs with the S5 detail —
    what the credential can actually do, on the row that says whether it
    works. Admin-scoped; the public pin lives in test_panels."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)

    (row,) = client.get("/api/v1/settings", **admin_headers).json()["providers"]

    assert "VM.Audit" in row["privileges"]
    assert "VM.GuestAgent.Audit" in row["privileges"]


def test_a_configured_provider_that_has_never_polled_says_so(
    client, admin_headers, provider
):
    """The state a fresh deployment is in for up to one poll interval. Silence
    here reads as broken."""
    (row,) = client.get("/api/v1/settings", **admin_headers).json()["providers"]
    assert row["configured"] is True
    assert row["usable"] is False
    assert row["detail"] == "Waiting for the first poll."
    assert row["last_polled_at"] is None


def test_the_settings_screen_reports_why_the_cluster_read_failed(
    client, admin_headers, provider, monkeypatch
):
    """Audit S5, the dead end closed: guests read fine, the cluster read 403s,
    `usable` stays true — and the dashboard says "see Settings for the reason".
    Until this row carried `infrastructure_detail`, Settings had nothing to
    show. The row is admin-scoped; `/api/v1/panels` keeps its closed public
    strings (pinned in test_panels)."""
    cluster = FakeCluster()
    original = cluster._route

    def route(path, query=""):
        return None if path in ("/cluster/status", "/nodes") else original(path, query)

    cluster._route = route
    install(monkeypatch, provider, cluster)
    poll_provider(provider)

    (row,) = client.get("/api/v1/settings", **admin_headers).json()["providers"]
    assert row["usable"] is True  # the poll itself succeeded
    assert row["infrastructure_detail"]  # and the reason the panel is empty is HERE
    assert row["guest_count"] == 6  # locations kept working throughout


# --- Location preview on the add form (§4a) --------------------------------


def test_the_probe_previews_the_location_a_service_would_get(
    client, admin_headers, provider, monkeypatch
):
    """The add form's Location input is the *pinned* value, so pre-filling it
    would pin every service at creation and the derived value could never track
    (§4a). Showing it beside the probe result is what lets the field stay empty
    without reading as broken."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    monkeypatch.setattr(
        "services.api.probe_url",
        lambda url: __import__("services.probe", fromlist=["ProbeResult"]).ProbeResult(
            ok=True, url=url, final_url=url, status=200
        ),
    )

    response = client.post(
        "/api/v1/probe",
        data={"url": "http://192.168.0.125:8989/"},
        content_type="application/json",
        **admin_headers,
    )

    body = response.json()
    assert body["derived_location"] == "pve-c · CT 105"
    assert body["derived_location_provider"] == "proxmox"


def test_the_preview_matches_what_the_service_actually_gets(provider, monkeypatch):
    """The preview would be worse than nothing if it disagreed with the result.

    Both go through `build_address_index`, so this asserts the two agree on the
    cases where they could plausibly differ: a hit, an ambiguous address, and an
    address with no guest.
    """
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)

    for url, expected in [
        ("http://192.168.0.125:8989/", "pve-c · CT 105"),
        ("http://172.17.0.1:9000/", None),        # ambiguous — two guests hold it
        ("http://192.168.0.250:5000/", None),    # no guest
    ]:
        preview = enrichment.preview_location(url, resolve=False)
        svc = make_service(slug=f"s{abs(hash(url))}", url=url)
        refresh()
        link = ServiceLink.objects.filter(service=svc).first()
        actual = link.location if link else None
        assert preview["location"] == expected
        assert actual == expected, f"preview and result disagree for {url}"


def test_the_preview_is_null_with_no_snapshot():
    """A fresh install, before the first poll. Null renders no row at all rather
    than an empty one that looks like a failure."""
    assert enrichment.preview_location("http://192.168.0.125/", resolve=False) == {
        "location": None,
        "provider": None,
    }


def test_the_preview_makes_no_provider_call(provider, monkeypatch):
    cluster = install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    before = len(cluster.requests)

    enrichment.preview_location("http://192.168.0.125:8989/", resolve=False)

    assert len(cluster.requests) == before


def test_guest_addresses_never_reach_an_anonymous_reader(client):
    """**The disclosure boundary V1.4 draws, pinned.**

    `GET /services` and `/export/json` are `auth=None` by design (§4c), and V1.4
    adds a location string to both. Node name and VMID are accepted as public —
    `host_label` has carried exactly that shape since v0, operator-typed, on the
    same anonymous response — but a guest's **addresses** are a different class:
    they are an internal network map of hosts the catalog does not list, and
    nothing about the catalog requires publishing them.

    Today they stay server-side only because the resolvers return `link.location`
    and nothing else. That is a property of three lines, not a boundary, so this
    is what makes it one: adding `addresses` to `ServiceOut` fails here.
    """
    cache(FakeCluster())
    make_service()  # launch_url is http://192.168.0.125:8989/ — guest 105
    refresh()

    for path in ("/api/v1/services", "/api/v1/export/json"):
        response = client.get(path)
        (service,) = response.json()
        raw = response.content.decode()

        # The location string is public, deliberately.
        assert service["location_derived"] == "pve-c · CT 105"

        # Guest 105 also holds 172.17.0.1 and is named "media-services". The
        # service references neither, so either appearing means the link payload
        # leaked rather than the service's own data being echoed.
        #
        # Note what is NOT asserted: 192.168.0.125 *is* in the response, because
        # it is this service's own `launch_url` and has been public since v0.
        # Asserting its absence would fail for the wrong reason and would have to
        # be deleted by whoever hit it — a test that cries wolf gets removed.
        assert "172.17.0.1" not in raw
        assert "media-services" not in raw
        assert "addresses" not in raw


def test_no_response_ever_carries_the_proxmox_credential(
    client, admin_headers, provider, monkeypatch
):
    """§7a: the token must never be returned by an API — admin responses included."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    make_service()
    refresh()

    for path, headers in (
        ("/api/v1/services", {}),
        ("/api/v1/export/json", {}),
        ("/api/v1/settings", admin_headers),
    ):
        body = client.get(path, **headers).content.decode()
        assert "secret-uuid" not in body
        assert "PVEAPIToken" not in body


def test_provider_status_is_admin_only(client):
    """Node names, guest counts and privilege names are a different disclosure
    class from the catalog (§6a)."""
    assert client.get("/api/v1/settings").status_code == 401
    assert "providers" not in client.get("/api/v1/settings/public").json()
