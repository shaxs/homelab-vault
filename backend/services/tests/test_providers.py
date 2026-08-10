"""The provider seam and the Proxmox implementation — v1 §7a, §7b, §7c.

The tests that matter most here are the ones about **states that look like
success**: a token with no privileges, and a DHCP container whose config reads
cleanly and contains no address. Both were found against the real cluster before
this code existed, and both would otherwise ship as "enrichment silently does
nothing".
"""

import json

import httpx
import pytest

from services.models import ProviderSnapshot
from services.providers import ProxmoxProvider, configured_providers
from services.providers.base import Guest, ProviderStatus, ProviderUnavailable
from services.providers.poll import poll_provider

from .proxmox_fakes import (
    DEFAULT_GUESTS,
    HA_OFF,
    HA_ON,
    TIB,
    FakeCluster,
    guest,
    install,
    install_failure,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def provider(settings):
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=hlv@pve!readonly=secret-uuid"
    return ProxmoxProvider()


# --- Configuration ---------------------------------------------------------


def test_a_provider_with_no_credentials_is_not_configured():
    """§7d: no token -> provider disabled -> no panel, no enrichment, no worker."""
    assert ProxmoxProvider().is_configured() is False
    assert configured_providers() == []


def test_is_configured_needs_both_host_and_token(settings):
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = ""
    assert ProxmoxProvider().is_configured() is False
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    settings.PROXMOX_HOST = ""
    assert ProxmoxProvider().is_configured() is False


def test_a_host_without_a_scheme_is_refused_by_name(settings):
    """A bare `host:port` raises httpx.UnsupportedProtocol on every single call.

    Naming it once beats failing per-poll with an exception whose message says
    nothing about which variable is wrong.
    """
    settings.PROXMOX_HOST = "192.168.0.101:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    status = ProxmoxProvider().status()
    assert status.usable is False
    assert "PROXMOX_HOST" in status.detail and "https://" in status.detail


def test_a_scheme_with_no_address_is_told_that_and_not_the_opposite(settings):
    """Found by the operator, 2026-08-06: `PROXMOX_HOST=https://` was reported as
    *"must include the scheme"* — pointing at the one part of the line that was
    right, and sending them to re-check what they had just done correctly.

    An error that names the wrong half is worse than a generic one.
    """
    settings.PROXMOX_HOST = "https://"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"

    detail = ProxmoxProvider().status().detail

    assert "no address" in detail
    assert "must start with https" not in detail  # the wrong half


@pytest.mark.parametrize(
    "value, expected",
    [
        ("192.168.0.101:8006", "https://"),   # no scheme at all
        ("ftp://192.168.0.101", "https://"),  # wrong scheme
        ("https://", "no address"),            # scheme only
        ("http://", "no address"),
    ],
)
def test_each_malformed_host_names_its_own_fault(settings, value, expected):
    settings.PROXMOX_HOST = value
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    assert expected in ProxmoxProvider().status().detail


def test_a_well_formed_host_is_accepted(settings, monkeypatch):
    """Shape validation passes and the call proceeds to the transport.

    **`.invalid` deliberately, never a real-looking LAN address.** The first
    draft used a real LAN address — at the time, the operator's actual cluster —
    and the file's runtime went from 0.9s to 4.3s because the test was dialling
    it for real. That is precisely what `_no_real_proxmox` exists to prevent, and
    hardcoding an address defeats it. `.invalid` is reserved by RFC 2606 and
    cannot resolve, so this stays offline and fast whatever machine runs it.
    """
    settings.PROXMOX_HOST = "https://pve.invalid:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    install_failure(monkeypatch, ProxmoxProvider(), exc=httpx.ConnectError("offline"))

    detail = ProxmoxProvider().status().detail

    # Refused at the transport, not on shape — which is the point.
    assert "PROXMOX_HOST" not in detail
    assert "offline" in detail


# --- The blind token -------------------------------------------------------


def test_a_token_with_no_privileges_is_reported_unusable(provider, monkeypatch):
    """**The finding this whole design exists for.**

    Proxmox filters collections by permission and answers `200 []` rather than
    403, so a token with no ACL enumerates zero guests through a completely
    healthy-looking call. Reported as "no guests", enrichment would write no
    links forever while every request returned 200.
    """
    install(monkeypatch, provider, FakeCluster(privileges={}))
    status = provider.status()
    assert status.configured is True
    assert status.reachable is True
    assert status.permitted is False
    assert status.usable is False
    assert "PVEAuditor" in status.detail


def test_a_blind_token_raises_rather_than_returning_an_empty_guest_list(
    provider, monkeypatch
):
    """`[]` must mean "this cluster has no guests" and nothing else.

    Confirmed red by making `guests()` return `[]` on a blind token: the assert
    below is the only thing standing between that and a silent no-op.
    """
    install(monkeypatch, provider, FakeCluster(privileges={}, guests=[]))
    with pytest.raises(ProviderUnavailable):
        provider.guests()


def test_the_blind_token_message_names_both_grants(provider, monkeypatch):
    """With privilege separation the effective rights are the INTERSECTION of the
    user's ACL and the token's, so granting only one leaves the token blind —
    the exact mistake made while provisioning the real credential."""
    install(monkeypatch, provider, FakeCluster(privileges={}))
    detail = provider.status().detail
    assert "--users" in detail and "--tokens" in detail


def test_a_permitted_token_reports_its_privileges(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())
    status = provider.status()
    assert status.usable is True
    assert "VM.Audit" in status.privileges
    # PVE 8+ moved read-only agent queries here from VM.Monitor, which is why
    # PVEAuditor alone can read a VM's addresses and §7a's "read-only by
    # construction" survives intact.
    assert "VM.GuestAgent.Audit" in status.privileges
    assert "VM.Monitor" not in status.privileges


# --- Addresses -------------------------------------------------------------


def test_lxc_addresses_come_from_the_running_interfaces_not_the_config(
    provider, monkeypatch
):
    """§7b says "LXC config carries them directly". It does not, for DHCP —
    which was 36 of 36 containers on the real cluster. The config is intent;
    only the running container knows its lease."""
    cluster = install(monkeypatch, provider, FakeCluster())
    guests = {g.foreign_id: g for g in provider.guests()}
    assert "192.168.0.125" in guests["105"].addresses
    assert "/nodes/pve-c/lxc/105/interfaces" in cluster.requests
    # The config is not even consulted when interfaces answered.
    assert "/nodes/pve-c/lxc/105/config" not in cluster.requests


def test_a_statically_addressed_container_falls_back_to_its_config(
    provider, monkeypatch
):
    """The rarer pinned case still has to work, or enrichment regresses for
    anyone who does not use DHCP."""
    cluster = install(
        monkeypatch,
        provider,
        FakeCluster(
            guests=[guest(150, "pve-c", "static", addresses=("192.168.0.200",))],
            static_ip_guests=[150],
        ),
    )
    (found,) = provider.guests()
    assert found.addresses == ("192.168.0.200",)
    assert "/nodes/pve-c/lxc/150/config" in cluster.requests


def test_vm_addresses_come_from_the_guest_agent(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())
    vm = next(g for g in provider.guests() if g.kind == "qemu")
    assert vm.addresses == ("192.168.0.88",)


def test_loopback_and_ipv6_are_dropped(provider, monkeypatch):
    """Every guest has 127.0.0.1. Keeping it would match every service against
    whichever guest was enumerated first — worse than matching nothing."""
    install(monkeypatch, provider, FakeCluster())
    for found in provider.guests():
        assert not any(a.startswith("127.") for a in found.addresses)
        assert not any(":" in a for a in found.addresses)


def test_a_stopped_guest_is_returned_with_no_addresses(provider, monkeypatch):
    """A stopped guest is not a deleted guest — it keeps its link (§7b)."""
    install(monkeypatch, provider, FakeCluster())
    stopped = next(g for g in provider.guests() if g.foreign_id == "140")
    assert stopped.running is False
    assert stopped.addresses == ()


def test_one_unreadable_guest_does_not_lose_the_others(provider, monkeypatch):
    """A VM without the guest agent answers 500. That is §7b's "expect partial
    coverage", and it must cost exactly one guest's address."""
    cluster = FakeCluster()

    original = cluster.handler

    def flaky(request):
        if "agent/network-get-interfaces" in request.url.path:
            return httpx.Response(500, json={"data": None})
        return original(request)

    cluster.handler = flaky
    install(monkeypatch, provider, cluster)

    guests = provider.guests()
    assert len(guests) == len(cluster.guests)
    vm = next(g for g in guests if g.kind == "qemu")
    assert vm.addresses == ()
    assert vm.node == "pve-a"  # still linked, still located


# --- Failure modes ---------------------------------------------------------


def test_an_unreachable_cluster_raises_provider_unavailable(provider, monkeypatch):
    install_failure(monkeypatch, provider, exc=httpx.ConnectError("no route"))
    with pytest.raises(ProviderUnavailable):
        provider.guests()
    assert provider.status().reachable is False


def test_a_401_names_the_credential_format_without_echoing_it(provider, monkeypatch):
    """The realistic mistake is storing only the UUID. The message has to say so
    without ever containing the value."""
    install_failure(monkeypatch, provider, status_code=401)
    status = provider.status()
    assert "PVEAPIToken=" in status.detail
    assert "secret-uuid" not in status.detail


# --- Response-shape validation (audit S6) ----------------------------------
#
# A 200 whose body is not the {"data": ...} envelope is a reverse proxy's
# error page or a changed endpoint, not the PVE API. Before these checks it
# surfaced as an AttributeError that escaped `poll_provider`'s
# ProviderUnavailable-only catch, so the worker's broad handler logged it AND
# STILL BEAT — snapshot ok=True with stale data, deadman happy, Settings
# showing no failure. The realistic trigger needs no attacker.


def _install_body(monkeypatch, provider, *, json_body="__unset__", text=None):
    """Every endpoint answers 200 with this body — the reverse-proxy shape."""

    def handler(request):
        if text is not None:
            return httpx.Response(200, text=text)
        return httpx.Response(200, json=json_body)

    def _client(self=None):
        return httpx.Client(
            base_url="https://pve.test/api2/json",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "PVEAPIToken=t@pve!k=secret"},
        )

    monkeypatch.setattr(type(provider), "_client", _client)


@pytest.mark.parametrize(
    "body",
    [
        "[1, 2, 3]",    # a bare list where the envelope should be
        '"a string"',   # a bare string
        "null",         # a literal null body — httpx's json= treats None as
                        # unset, so this must go over the wire as text
    ],
    ids=["list-body", "string-body", "null-body"],
)
def test_a_200_without_the_data_envelope_is_a_failed_poll_not_a_crash(
    provider, monkeypatch, body
):
    _install_body(monkeypatch, provider, text=body)

    row = poll_provider(provider)

    assert row.ok is False
    assert "unexpected response shape" in row.detail


def test_an_html_error_page_is_a_failed_poll_and_is_never_quoted(
    provider, monkeypatch
):
    """The detail reaches the admin settings screen; an error page can carry
    anything, so the body is never echoed into it."""
    _install_body(
        monkeypatch, provider, text="<html><body>BadGateway-XYZZY</body></html>"
    )

    row = poll_provider(provider)

    assert row.ok is False
    assert "XYZZY" not in row.detail
    assert "non-JSON" in row.detail


#: A status that says "go ahead" — handed in so the tests below exercise the
#: collection read itself. Left to ask for itself, a malformed body also fails
#: `status()` and `guests()` raises on the blind-token path, which would keep
#: these green with the shape check deleted.
_USABLE = ProviderStatus(configured=True, reachable=True, permitted=True)


def test_a_data_of_the_wrong_type_is_unavailable_not_a_type_error(
    provider, monkeypatch
):
    """`{"data": 42}` where a collection belongs. Deliberately a NON-iterable:
    the first version used a dict, and the list-of-objects check below the
    type check catches an iterable by accident — so the test stayed green
    with the type check deleted (found by this window's own red-check). A
    guard must be tested with an input only IT can refuse."""
    _install_body(monkeypatch, provider, json_body={"data": 42})

    with pytest.raises(ProviderUnavailable):
        provider.guests(status=_USABLE)


def test_a_collection_of_non_objects_is_unavailable(provider, monkeypatch):
    _install_body(monkeypatch, provider, json_body={"data": ["x", "y"]})
    with pytest.raises(ProviderUnavailable):
        provider.guests(status=_USABLE)


def test_an_unexpected_provider_error_is_recorded_at_the_poll_boundary(
    provider, monkeypatch
):
    """poll_provider's docstring says "Never raises", and this is the line that
    makes it true rather than a claim (audit S6 — blind spot 5)."""
    install(monkeypatch, provider, FakeCluster())

    def explode(status=None):
        raise RuntimeError("secret-response-content")

    monkeypatch.setattr(provider, "guests", explode)

    row = poll_provider(provider)  # must not raise

    assert row.ok is False
    assert row.consec_failures == 1
    # A closed string, never the exception text: the detail reaches the admin
    # settings screen and the exception can quote whatever the remote sent.
    assert "secret-response-content" not in row.detail
    assert row.detail == (
        "Provider returned an unexpected response; see the worker log."
    )


def test_an_unexpected_failure_still_counts_toward_the_provider_alert(
    provider, monkeypatch
):
    """The BACKLOG.6 damping must not distinguish how a poll failed."""
    install(monkeypatch, provider, FakeCluster())
    monkeypatch.setattr(
        provider, "guests", lambda status=None: (_ for _ in ()).throw(RuntimeError())
    )

    for _ in range(3):
        row = poll_provider(provider)

    assert row.consec_failures == 3
    assert row.alert_active is True


def test_an_infrastructure_surprise_costs_the_panel_not_the_guests(
    provider, monkeypatch
):
    """Same ordering rule as a clean ProviderUnavailable (§7d): the cluster
    read failing — however it fails — must not stop the catalog getting
    locations."""
    install(monkeypatch, provider, FakeCluster())
    monkeypatch.setattr(
        provider,
        "infrastructure",
        lambda status=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    row = poll_provider(provider)

    assert row.ok is True
    assert len(row.guests) == 6
    assert row.payload["infrastructure"] is None
    assert "boom" not in row.payload["infrastructure_detail"]
    assert "unexpected" in row.payload["infrastructure_detail"]


# --- The healthy empty cluster (audit T5) -----------------------------------


def test_a_permitted_token_against_an_empty_cluster_is_no_guests_and_ok(
    provider, monkeypatch
):
    """The blind-token contract pinned from the other side: `[]` with full
    privileges means "this cluster genuinely has no guests" and must stay a
    SUCCESSFUL answer. A future "hardening" that raises on an empty list would
    break every fresh install — nothing asserted this direction until now."""
    install(monkeypatch, provider, FakeCluster(guests=[]))

    assert provider.guests() == []

    row = poll_provider(provider)
    assert row.ok is True
    assert row.guests == []
    assert row.detail == ""


# --- One poll, one permissions read (audit informational) -------------------


def test_one_poll_asks_access_permissions_once(provider, monkeypatch):
    """status(), guests() and infrastructure() each used to ask
    `/access/permissions` through their own client — three TLS handshakes and
    three round trips per poll where one answers all three."""
    cluster = install(monkeypatch, provider, FakeCluster())

    poll_provider(provider)

    permission_reads = [
        p for p in cluster.requests if p == "/access/permissions"
    ]
    assert len(permission_reads) == 1


# --- Location strings ------------------------------------------------------


@pytest.mark.parametrize(
    "kind, expected",
    [("lxc", "pve-c · CT 105"), ("qemu", "pve-c · VM 105")],
)
def test_location_string_matches_the_established_host_label_format(kind, expected):
    found = Guest(foreign_id="105", node="pve-c", kind=kind, name="x")
    assert ProxmoxProvider().location_for(found) == expected


# --- Snapshot caching ------------------------------------------------------


def test_a_successful_poll_caches_every_guest(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())
    row = poll_provider(provider)
    assert row.ok is True
    assert len(row.guests) == 6
    assert {g["location"] for g in row.guests} >= {"pve-c · CT 105", "pve-a · VM 124"}


def test_a_failed_poll_keeps_the_previous_payload_and_records_why(
    provider, monkeypatch
):
    """Stale data plus a stated staleness beats no data: an operator asking "why
    is there no location" needs the reason, and a snapshot that merely stops
    updating cannot give one."""
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)

    install_failure(monkeypatch, provider, exc=httpx.ConnectError("cluster down"))
    row = poll_provider(provider)

    assert row.ok is False
    assert "cluster down" in row.detail
    assert len(row.guests) == 6  # previous payload intact


def test_polling_an_unconfigured_provider_is_recorded_not_raised(settings):
    settings.PROXMOX_HOST = ""
    settings.PROXMOX_API_TOKEN = ""
    row = poll_provider(ProxmoxProvider())
    assert row.ok is False
    assert row.detail == "Not configured."
    assert ProviderSnapshot.objects.count() == 1


def test_a_poll_never_writes_the_credential_into_the_snapshot(provider, monkeypatch):
    """§7a: the token must never be returned by an API, and the snapshot is
    served to the settings screen."""
    install(monkeypatch, provider, FakeCluster())
    row = poll_provider(provider)
    import json as _json

    assert "secret-uuid" not in _json.dumps(row.payload)
    assert "PVEAPIToken" not in _json.dumps(row.payload)


# --- The seam itself -------------------------------------------------------


# --- TLS (secure-code-review gate, 2026-08-06) -----------------------------


def test_unverified_tls_is_announced_because_a_credential_rides_on_it(
    provider, settings, caplog
):
    """`PROXMOX_VERIFY_TLS=False` is not the same trade-off as
    `PROBE_VERIFY_TLS=False`, and the symmetry between the two settings is what
    makes that easy to miss: the probe carries no credential, this client
    carries one on every request. Off is still the default — Proxmox ships a
    self-signed certificate — but it must not be silent."""
    import services.providers.proxmox as mod

    mod._tls_warning_said = False
    settings.PROXMOX_VERIFY_TLS = False

    with caplog.at_level("WARNING"):
        provider._client().close()

    assert any("PROXMOX_VERIFY_TLS is off" in r.message for r in caplog.records)
    # And the warning names no credential.
    assert all("secret-uuid" not in r.getMessage() for r in caplog.records)


def test_the_tls_warning_is_said_once_not_once_per_request(provider, settings, caplog):
    """A per-request warning on a worker polling every 30s is noise that trains
    an operator to ignore the log."""
    import services.providers.proxmox as mod

    mod._tls_warning_said = False
    settings.PROXMOX_VERIFY_TLS = False

    with caplog.at_level("WARNING"):
        for _ in range(3):
            provider._client().close()

    said = [r for r in caplog.records if "PROXMOX_VERIFY_TLS is off" in r.message]
    assert len(said) == 1


def test_no_warning_when_tls_is_verified(provider, settings, caplog):
    import services.providers.proxmox as mod

    mod._tls_warning_said = False
    settings.PROXMOX_VERIFY_TLS = True

    with caplog.at_level("WARNING"):
        provider._client().close()

    assert not [r for r in caplog.records if "PROXMOX_VERIFY_TLS" in r.message]


def test_the_client_never_follows_redirects(provider, settings):
    """httpx replays the `Authorization` header at the redirect target. The probe
    can afford hops because it sends no credential; this client cannot."""
    settings.PROXMOX_VERIFY_TLS = True
    client = provider._client()
    try:
        assert client.follow_redirects is False
    finally:
        client.close()


def test_provider_status_usable_requires_all_three():
    assert ProviderStatus(configured=True, reachable=True, permitted=True).usable
    assert not ProviderStatus(configured=True, reachable=True, permitted=False).usable
    assert not ProviderStatus(configured=True, reachable=False, permitted=True).usable
    assert not ProviderStatus(configured=False, reachable=True, permitted=True).usable


def test_the_proxmox_provider_satisfies_the_protocol():
    """The seam is the deliverable (§7a); a second provider must be able to land
    with zero changes to the enrichment path."""
    from services.providers.base import Provider

    assert isinstance(ProxmoxProvider(), Provider)


# --- Infrastructure: §7d's summary row and nodes table ---------------------
#
# Every shape asserted below came from the live cluster on 2026-08-06, and three
# of these tests exist because building from the spec's wording alone would have
# produced working code with wrong numbers in it.


def test_infrastructure_reads_the_cluster_its_nodes_and_its_storage(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())

    infra = provider.infrastructure()

    assert infra.cluster_name == "demo-lab"
    assert infra.quorate is True
    assert [n.name for n in infra.nodes] == ["pve-a", "pve-b", "pve-c", "pve-d"]
    assert all(n.online for n in infra.nodes)


def test_a_shared_storage_is_counted_once_not_once_per_node(provider, monkeypatch):
    """The finding that makes this method worth testing at all.

    `/cluster/resources?type=storage` returns one row per storage *per node that
    can see it*. The fake has one 40 TiB NFS mount visible from four nodes plus
    a local disk each. Summed as they arrive that is 165 TiB; deduplicated it is
    45 TiB. On the real cluster the same mistake reported 1331 TiB against 336.

    Nothing about the wrong version looks wrong: every read succeeds and every
    row is genuine.
    """
    install(monkeypatch, provider, FakeCluster())

    infra = provider.infrastructure()

    # 40 (shared, counted once) + 1 + 2 + 1 + 1 (local, per node) = 45 TiB.
    assert infra.storage_total_bytes == 45 * TIB
    assert infra.storages_total == 5
    assert infra.storages_online == 5


def test_local_and_shared_storage_of_the_same_name_are_different_disks(provider, monkeypatch):
    """`local` exists on every node and is four disks; `shared-nas` is one NAS
    seen four times. The dedupe key has to tell them apart."""
    install(monkeypatch, provider, FakeCluster())

    by_name = {n.name: n for n in provider.infrastructure().nodes}

    # Per-node storage is LOCAL only — otherwise the same 40 TiB share prints on
    # all four rows and implies four of them.
    assert by_name["pve-c"].storage_total_bytes == 2 * TIB
    assert by_name["pve-d"].storage_total_bytes == 1 * TIB


def test_the_version_column_is_the_pve_version_not_the_corosync_one(provider, monkeypatch):
    """`/cluster/status`'s cluster entry carries a `version` and it is the
    corosync config version — `8` on a cluster running 9.1. Rendering it in
    §7d's Version column would have been wrong by a whole major release, on
    every row, with every read succeeding."""
    install(monkeypatch, provider, FakeCluster())

    versions = {n.version for n in provider.infrastructure().nodes}

    assert versions == {"9.1.1", "9.1.5"}
    assert "8" not in versions


def test_a_half_upgraded_cluster_reports_a_version_per_node(provider, monkeypatch):
    """Observed live: two nodes on 9.1.1 and two on 9.1.5. One cluster-level
    version would have been wrong for half the cluster."""
    install(monkeypatch, provider, FakeCluster())

    versions = {n.name: n.version for n in provider.infrastructure().nodes}

    assert versions["pve-d"] == "9.1.1"
    assert versions["pve-b"] == "9.1.5"


def test_load_average_is_the_one_minute_figure_as_a_number(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())

    by_name = {n.name: n for n in provider.infrastructure().nodes}

    assert by_name["pve-b"].load_avg == 2.09
    # 0.00 is a number, not a missing value: `or None` would erase it.
    assert by_name["pve-a"].load_avg == 0.0


def test_cpu_is_reported_as_a_percentage_not_a_fraction(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())

    by_name = {n.name: n for n in provider.infrastructure().nodes}

    assert by_name["pve-b"].cpu_pct == 11.8


def test_a_node_whose_status_call_fails_still_appears_in_the_table(provider, monkeypatch):
    """One node dropping out mid-poll costs its own two columns, not the table.

    Same rule as `_one_guest`: a node vanishing from the list reads as a node
    that no longer exists.
    """
    install(monkeypatch, provider, FakeCluster(unreadable_nodes=["pve-c"]))

    by_name = {n.name: n for n in provider.infrastructure().nodes}

    assert set(by_name) == {"pve-d", "pve-c", "pve-b", "pve-a"}
    assert by_name["pve-c"].load_avg is None
    assert by_name["pve-c"].version == ""
    # What `/nodes` already gave us is still there.
    assert by_name["pve-c"].uptime_s == 1189913
    assert by_name["pve-d"].version == "9.1.1"


def test_ha_is_not_configured_when_only_a_quorum_entry_comes_back(provider, monkeypatch):
    """`/cluster/ha/status/current` answers 200 with one quorum entry on a
    cluster with no HA at all, so "the call worked" is not "HA is on"."""
    install(monkeypatch, provider, FakeCluster(ha=HA_OFF))

    assert provider.infrastructure().ha == "not-configured"


def test_ha_is_enabled_when_a_managed_service_is_present(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster(ha=HA_ON))

    assert provider.infrastructure().ha == "enabled"


def test_an_unreadable_ha_endpoint_reports_unknown_not_off(provider, monkeypatch):
    """This read needs `Sys.Audit`. A token without it must not make an
    unmonitored cluster look deliberately unmonitored."""
    cluster = _cluster_failing_on(("/cluster/ha/status/current",))
    install(monkeypatch, provider, cluster)

    assert provider.infrastructure().ha == "unknown"


def test_a_standalone_host_has_no_cluster_and_no_quorum_verdict(provider, monkeypatch):
    """An unclustered PVE host returns no `type=cluster` entry. `quorate` is
    None there, not False: one host cannot lose a vote it never had, and False
    would paint a healthy lab red."""
    install(monkeypatch, provider, FakeCluster(cluster_name=""))

    infra = provider.infrastructure()

    assert infra.cluster_name == ""
    assert infra.quorate is None


def test_node_addresses_come_from_cluster_status_not_nodes(provider, monkeypatch):
    """`/nodes` does not carry an address; `/cluster/status`'s node entries do.

    Built from `/nodes` alone the field would be silently empty and the drawer's
    Open action would never appear, with nothing failing.
    """
    install(monkeypatch, provider, FakeCluster())

    by_name = {n.name: n for n in provider.infrastructure().nodes}

    assert all(node.ip for node in by_name.values())
    assert by_name["pve-d"].ip.startswith("192.0.2.")


def test_a_node_missing_from_cluster_status_still_gets_a_row(provider, monkeypatch):
    """A node `/nodes` lists but `/cluster/status` omits keeps its row with an
    empty address, rather than disappearing or raising a KeyError."""
    cluster = FakeCluster()
    original = cluster._route

    def route(path, query=""):
        rows = original(path, query)
        if path == "/cluster/status":
            return [r for r in rows if r.get("name") != "pve-c"]
        return rows

    cluster._route = route
    install(monkeypatch, provider, cluster)

    by_name = {n.name: n for n in provider.infrastructure().nodes}

    assert set(by_name) == {"pve-d", "pve-c", "pve-b", "pve-a"}
    assert by_name["pve-c"].ip == ""
    assert by_name["pve-d"].ip


def test_the_node_payload_never_carries_the_ssl_fingerprint(provider, monkeypatch):
    """`/nodes` returns one per node and `/api/v1/panels` is public. The seam has
    no such field, so the omission is structural rather than a filter somebody
    has to remember to keep applying."""
    install(monkeypatch, provider, FakeCluster())

    payload = json.dumps(provider.infrastructure().as_payload())

    assert "AA:BB:CC:DD" not in payload
    assert "ssl_fingerprint" not in payload


def test_infrastructure_raises_rather_than_returning_an_empty_cluster(provider, monkeypatch):
    """Same rule as `guests()`: empty means "there is nothing", and a provider
    that cannot see must never say that."""
    install_failure(monkeypatch, provider, status_code=403)

    with pytest.raises(ProviderUnavailable):
        provider.infrastructure()


# --- The poll caches it, and cannot lose the guests doing so ---------------


def test_a_poll_caches_the_cluster_alongside_the_guests(provider, monkeypatch):
    install(monkeypatch, provider, FakeCluster())

    row = poll_provider(provider)

    assert row.ok is True
    assert row.payload["infrastructure"]["cluster_name"] == "demo-lab"
    assert len(row.payload["infrastructure"]["nodes"]) == 4
    assert len(row.guests) == len(DEFAULT_GUESTS)


def test_a_failed_cluster_read_never_costs_the_guests(provider, monkeypatch):
    """Enrichment is the product; the panel is decoration.

    Wired as one all-or-nothing call, a 403 on a diagnostic endpoint would stop
    every service in the catalog getting a location. This assertion is what
    keeps the two apart.
    """
    install(monkeypatch, provider, _cluster_failing_on(("/cluster/status", "/nodes")))

    row = poll_provider(provider)

    assert row.ok is True
    assert len(row.guests) == len(DEFAULT_GUESTS)
    assert row.payload["infrastructure"] is None
    assert row.payload["infrastructure_detail"]


def test_a_provider_with_no_cluster_is_not_an_error():
    """A Docker or TrueNAS provider enumerates guests and has no nodes table.
    `None` is the honest answer and must not be recorded as a failed poll."""

    class GuestOnlyProvider:
        name = "guests-only"

        def is_configured(self):
            return True

        def status(self):
            return ProviderStatus(configured=True, reachable=True, permitted=True)

        def guests(self, status=None):
            return [Guest(foreign_id="1", node="host", kind="lxc", name="one")]

        def location_for(self, guest):
            return "host"

    row = poll_provider(GuestOnlyProvider())

    assert row.ok is True
    assert row.payload["infrastructure"] is None
    assert len(row.guests) == 1


def _cluster_failing_on(paths):
    """A cluster where the named endpoints answer 500 and the rest work."""
    cluster = FakeCluster()
    original = cluster._route

    def route(path, query=""):
        return None if path in paths else original(path, query)

    cluster._route = route
    return cluster


# --- A provider that keeps failing is announced (BACKLOG.6) -----------------
#
# The gap the operator found by breaking Proxmox on purpose: a failed poll was
# recorded and shown on the settings screen, and nothing else happened. The
# worker keeps beating, so the deadman stays silent — it watches workers, and
# the worker is fine; it is the cluster that is not.


@pytest.fixture
def dispatched(monkeypatch):
    sent = []
    monkeypatch.setattr("services.notifications.dispatch", sent.append)
    return sent


def _enable_discord():
    from services.settings_store import write_settings

    write_settings({"notifications.discord.enabled": True})


def test_a_provider_alert_fires_on_the_third_consecutive_failure_and_only_once(
    provider, monkeypatch, dispatched
):
    """Damped like `fail_threshold`, edge-triggered like the deadman: an
    overnight outage is one message, not one per poll interval."""
    _enable_discord()
    install_failure(monkeypatch, provider, exc=httpx.ConnectError("cluster down"))

    for _ in range(5):
        poll_provider(provider)

    assert len(dispatched) == 1
    assert "proxmox" in dispatched[0]["content"]
    assert "3" in dispatched[0]["content"]


def test_one_or_two_failed_polls_say_nothing(provider, monkeypatch, dispatched):
    _enable_discord()
    install_failure(monkeypatch, provider, exc=httpx.ConnectError("blip"))
    poll_provider(provider)
    poll_provider(provider)
    assert dispatched == []


def test_a_bounce_resets_the_streak(provider, monkeypatch, dispatched):
    """fail, fail, success, fail, fail — never three in a row, never a message."""
    _enable_discord()
    for outcome in [False, False, True, False, False]:
        if outcome:
            install(monkeypatch, provider, FakeCluster())
        else:
            install_failure(monkeypatch, provider, exc=httpx.ConnectError("blip"))
        poll_provider(provider)
    assert dispatched == []


def test_a_recovery_is_announced_once_and_the_alert_rearms(
    provider, monkeypatch, dispatched
):
    _enable_discord()
    install_failure(monkeypatch, provider, exc=httpx.ConnectError("down"))
    for _ in range(3):
        poll_provider(provider)

    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)  # recovery — announced
    poll_provider(provider)  # still fine — silent

    install_failure(monkeypatch, provider, exc=httpx.ConnectError("down again"))
    for _ in range(3):
        poll_provider(provider)  # a second incident alerts again

    assert len(dispatched) == 3
    assert "failing" in dispatched[0]["content"]
    assert "recovered" in dispatched[1]["content"]
    assert "failing" in dispatched[2]["content"]


def test_a_recovery_with_no_alert_active_is_silent(provider, monkeypatch, dispatched):
    """The catalog-recovery rule (§5) applied here: "recovered" is a lie about a
    provider that was never announced as failing."""
    _enable_discord()
    install(monkeypatch, provider, FakeCluster())
    poll_provider(provider)
    assert dispatched == []


def test_provider_alerts_obey_the_discord_setting(provider, monkeypatch, dispatched):
    """Standing rule 2: the settings value reaches this consumer too. The alert
    STATE still latches — switching Discord on later must not replay history."""
    install_failure(monkeypatch, provider, exc=httpx.ConnectError("down"))
    for _ in range(4):
        poll_provider(provider)

    assert dispatched == []
    assert ProviderSnapshot.objects.get(provider="proxmox").alert_active is True


def test_the_provider_alert_never_carries_the_snapshot_detail(
    provider, monkeypatch, dispatched
):
    """Discord is not the settings screen (the same boundary `/api/v1/panels`
    draws). The snapshot's `detail` is written for the admin screen and can
    name an environment variable or quote a `pveum` command carrying the
    token's `user@realm` — none of which belongs in a chat channel.
    """
    _enable_discord()
    install_failure(
        monkeypatch,
        provider,
        exc=httpx.ConnectError("pveum aclmod / --users hlv@pve secret-diagnostic"),
    )
    for _ in range(3):
        poll_provider(provider)

    (message,) = dispatched
    blob = json.dumps(message)
    assert "secret-diagnostic" not in blob
    assert "hlv@pve" not in blob
    # But the operator IS pointed at where the real reason lives.
    assert "Settings" in blob


def test_an_unconfigured_provider_never_counts_toward_the_alert(settings, dispatched):
    """§7d: not configured is a configuration state, not an incident — and the
    worker never polls an unconfigured provider anyway. Belt and braces for
    direct calls."""
    _enable_discord()
    settings.PROXMOX_HOST = ""
    settings.PROXMOX_API_TOKEN = ""

    for _ in range(4):
        poll_provider(ProxmoxProvider())

    assert dispatched == []
    row = ProviderSnapshot.objects.get(provider="proxmox")
    assert row.consec_failures == 0
    assert row.alert_active is False
