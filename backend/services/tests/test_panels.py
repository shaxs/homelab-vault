"""The dashboard's one endpoint — v1 §8, §7d.

Three groups of tests, and the middle one is the reason this file is long.

1. **The endpoint assembles panels server-side from cache.** §8 calls that the
   one v1 decision v2 depends on, and warns that a panel fetching for itself
   gets rebuilt. Proven by counting outbound requests, not by reading the code.
2. **It is public, and public has a boundary.** The operator ruled on 2026-08-06
   that cluster data ships unauthenticated (AS_BUILT §7). That ruling covers
   node names, versions and capacity — and nothing else. Guest names and
   addresses, the reason a poll failed, and the certificate fingerprint
   `/nodes` hands out all stay behind it, so each has an assertion here.
3. **Panel visibility actually reaches the panel** (§6a, standing rule 2). V1.2
   built the switches and named V1.5 as their consumer; a settings test that
   only proves the value reads back would leave a green chunk with an inert
   screen.
"""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from services.models import ProviderSnapshot, Service
from services.settings_store import write_settings

from .proxmox_fakes import FakeCluster, install

pytestmark = pytest.mark.django_db

PANELS = "/api/v1/panels"


@pytest.fixture
def proxmox_configured(settings):
    """Credentials present, so §7d's "not configured means not rendered" does
    not hide everything. No transport: nothing here may call a cluster."""
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=hlv@pve!readonly=secret-uuid"
    write_settings({"proxmox.polling.enabled": True})


def snapshot(payload=None, ok=True, detail="", age_s=0):
    """A cached poll, as the worker would have written it."""
    from services.providers import ProxmoxProvider
    from services.providers.poll import poll_provider

    row, _ = ProviderSnapshot.objects.get_or_create(
        provider="proxmox", defaults={"fetched_at": timezone.now()}
    )
    row.payload = payload if payload is not None else {}
    row.ok = ok
    row.detail = detail
    row.fetched_at = timezone.now() - timedelta(seconds=age_s)
    row.save()
    return row


def real_snapshot(monkeypatch, **kwargs):
    """A snapshot written by an actual poll of the fake cluster.

    Hand-built payloads drift from what the worker really writes, and this
    endpoint reads that payload — so the fixture that matters is the real one.
    """
    from services.providers import ProxmoxProvider
    from services.providers.poll import poll_provider

    provider = ProxmoxProvider()
    install(monkeypatch, provider, FakeCluster(**kwargs))
    return poll_provider(provider)


def make(slug, **kwargs):
    defaults = {"name": slug.title(), "launch_url": f"https://{slug}.lab.local"}
    return Service.objects.create(slug=slug, **{**defaults, **kwargs})


# --- Assembled server-side, from cache only --------------------------------


def test_the_dashboard_is_one_public_read(client):
    """No interstitial, and no second endpoint per panel."""
    response = client.get(PANELS)

    assert response.status_code == 200
    body = response.json()
    assert body["landing_page"] == "catalog"
    assert "status_summary" in body


def test_a_panel_read_makes_no_outbound_call(client, proxmox_configured, monkeypatch):
    """§7c made structural, not defensive.

    There is no timeout to tune and no try/except to forget, because there is no
    outbound call on this path at all. Counting requests is the only assertion
    that stays true when somebody adds one.
    """
    real_snapshot(monkeypatch)
    calls = []

    import httpx

    original = httpx.Client.request

    def counted(self, method, url, *args, **kwargs):
        calls.append(f"{method} {url}")
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", counted)

    assert client.get(PANELS).status_code == 200
    assert calls == []


def test_the_panel_serves_what_the_worker_cached(client, proxmox_configured, monkeypatch):
    real_snapshot(monkeypatch)

    body = client.get(PANELS).json()

    assert body["proxmox"]["available"] is True
    assert body["proxmox"]["cluster"]["name"] == "demo-lab"
    assert len(body["proxmox"]["nodes"]) == 4


def test_the_cluster_card_aggregates_only_online_nodes(client, proxmox_configured, monkeypatch):
    """An offline node contributes no capacity. Counting its cores reports a
    cluster with more CPU than it can actually run anything on."""
    from .proxmox_fakes import DEFAULT_NODES, node

    nodes = [node("pve-live", maxcpu=16), node("pve-dead", online=False, maxcpu=64)]
    real_snapshot(monkeypatch, nodes=nodes)

    cluster = client.get(PANELS).json()["proxmox"]["cluster"]

    assert cluster["nodes_online"] == 1
    assert cluster["nodes_total"] == 2
    assert cluster["cpu_count"] == 16


def test_cluster_cpu_is_weighted_by_core_count(client, proxmox_configured, monkeypatch):
    """A 4-core node at 90% and a 20-core node at 5% is not a cluster at 47.5%."""
    from .proxmox_fakes import node

    real_snapshot(
        monkeypatch,
        nodes=[node("small", maxcpu=4, cpu=0.90), node("big", maxcpu=20, cpu=0.05)],
    )

    cluster = client.get(PANELS).json()["proxmox"]["cluster"]

    # (90*4 + 5*20) / 24 = 19.2, not (90+5)/2 = 47.5.
    assert cluster["cpu_pct"] == 19.2


def test_guest_counts_come_from_the_cached_guest_list(client, proxmox_configured, monkeypatch):
    real_snapshot(monkeypatch)

    cluster = client.get(PANELS).json()["proxmox"]["cluster"]

    assert cluster["lxc_count"] == 5
    assert cluster["vm_count"] == 1


# --- Public, with a boundary -----------------------------------------------


def test_the_panel_never_names_a_guest(client, proxmox_configured, monkeypatch):
    """The ruling covers node inventory. A guest's name and addresses are an
    internal map of hosts the catalog does not list — V1.4 pinned that boundary
    on the catalog endpoints and this one must hold it too."""
    real_snapshot(monkeypatch)

    body = client.get(PANELS).content.decode()

    assert "media-services" not in body
    assert "192.168.0.125" not in body
    assert "172.17.0.1" not in body


def test_a_failed_poll_gives_a_reason_that_names_nothing(client, proxmox_configured):
    """The snapshot's own `detail` is written for the admin-scoped settings
    screen and can name an environment variable or quote a `pveum` command
    carrying the token's `user@realm`. The public panel gets a closed set of
    strings instead."""
    snapshot(
        ok=False,
        detail=(
            "The API token holds no privileges. Run:\n"
            "  pveum acl modify / --users 'hlv@pve' --roles PVEAuditor"
        ),
    )

    body = client.get(PANELS).json()["proxmox"]

    assert body["available"] is False
    assert body["detail"] == "Cluster state is unavailable — see Settings for the reason."
    assert "pveum" not in json.dumps(body)
    assert "hlv@pve" not in json.dumps(body)


def test_an_unparsed_cluster_read_is_also_coarsened(client, proxmox_configured, monkeypatch):
    """A poll that succeeded for guests but failed for the cluster records its
    reason in `infrastructure_detail`. That string is a provider message too."""
    from .test_providers import _cluster_failing_on

    from services.providers import ProxmoxProvider
    from services.providers.poll import poll_provider

    provider = ProxmoxProvider()
    install(monkeypatch, provider, _cluster_failing_on(("/cluster/status", "/nodes")))
    poll_provider(provider)

    body = client.get(PANELS).json()["proxmox"]

    assert body["available"] is False
    assert body["detail"] == "Cluster state is unavailable — see Settings for the reason."
    assert "HTTP 500" not in json.dumps(body)


def test_the_panel_never_carries_the_credential(client, proxmox_configured, monkeypatch):
    real_snapshot(monkeypatch)

    body = client.get(PANELS).content.decode()

    assert "PVEAPIToken" not in body
    assert "secret-uuid" not in body


def test_the_panel_never_carries_a_node_certificate_fingerprint(
    client, proxmox_configured, monkeypatch
):
    real_snapshot(monkeypatch)

    body = client.get(PANELS).content.decode()

    assert "ssl_fingerprint" not in body
    assert "AA:BB:CC:DD" not in body


def test_the_panel_never_carries_the_credentials_privileges(
    client, proxmox_configured, monkeypatch
):
    """The snapshot's cached status now carries privilege names for the
    admin settings row (audit informational); this endpoint is public and must
    not forward them — same boundary as the fingerprint above."""
    real_snapshot(monkeypatch)

    body = client.get(PANELS).content.decode()

    assert "privileges" not in body
    assert "VM.GuestAgent.Audit" not in body


def test_node_addresses_are_public_on_purpose(client, proxmox_configured, monkeypatch):
    """**This is a deliberate disclosure, not a leak — do not "fix" it.**

    Operator ruling 2026-08-07: the drawer links to each node's own web UI, which
    needs the address, and it ships on the public endpoint. That extends the
    2026-08-06 cluster-data ruling from names and capacity to addresses; both are
    recorded in AS_BUILT §7.

    Pinned in the positive direction precisely because every neighbouring test in
    this file asserts something *out* of this payload. Without this, the obvious
    reading of the file is that addresses were an oversight.
    """
    real_snapshot(monkeypatch)

    nodes = client.get(PANELS).json()["proxmox"]["nodes"]

    assert all(node["ip"] for node in nodes)
    # Guest addresses remain server-side — a different question, still closed.
    body = client.get(PANELS).content.decode()
    assert "192.168.0.125" not in body


# --- Not configured means not rendered (§7d) -------------------------------


def test_no_proxmox_configured_means_no_proxmox_panel(client):
    """The catalog was always the product; location is decoration. A dashboard
    with no cluster is still a dashboard."""
    body = client.get(PANELS).json()

    assert body["proxmox"] is None
    assert body["status_summary"] is not None
    assert body["panels"] == [
        "status_summary",
        "proxmox_cluster",
        "proxmox_nodes",
    ]


def test_polling_switched_off_says_so_rather_than_freezing(client, settings, monkeypatch):
    """A panel whose numbers stopped moving with nothing on screen saying why is
    worse than an honest empty one."""
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=hlv@pve!readonly=secret-uuid"
    write_settings({"proxmox.polling.enabled": True})
    real_snapshot(monkeypatch)
    write_settings({"proxmox.polling.enabled": False})

    body = client.get(PANELS).json()["proxmox"]

    assert body["available"] is False
    assert body["detail"] == "Proxmox polling is switched off in Settings."
    assert body["cluster"] is None
    assert body["nodes"] is None


def test_a_configured_provider_awaiting_its_first_poll_says_so(client, proxmox_configured):
    body = client.get(PANELS).json()["proxmox"]

    assert body["available"] is False
    assert body["detail"] == "Waiting for the first poll."


def test_a_snapshot_older_than_three_intervals_is_flagged_stale(
    client, proxmox_configured, monkeypatch
):
    """3x the interval, matching the deadman's rule (§5a) rather than inventing
    a second definition of "too old to trust"."""
    write_settings({"proxmox.poll_interval_s": 60})
    real_snapshot(monkeypatch)
    ProviderSnapshot.objects.filter(provider="proxmox").update(
        fetched_at=timezone.now() - timedelta(seconds=200)
    )

    assert client.get(PANELS).json()["proxmox"]["stale"] is True


def test_the_staleness_threshold_follows_the_live_interval(
    client, proxmox_configured, monkeypatch
):
    """Derived, never a constant. A 900s interval and a 60s one are judged by
    their own numbers, with nothing restarted."""
    real_snapshot(monkeypatch)
    ProviderSnapshot.objects.filter(provider="proxmox").update(
        fetched_at=timezone.now() - timedelta(seconds=200)
    )

    write_settings({"proxmox.poll_interval_s": 60})
    assert client.get(PANELS).json()["proxmox"]["stale"] is True

    write_settings({"proxmox.poll_interval_s": 900})
    assert client.get(PANELS).json()["proxmox"]["stale"] is False


# --- Settings reach the panel (§6a, standing rule 2) -----------------------


def test_switching_off_the_status_panel_removes_it_from_the_response(client):
    """Omitted server-side, not hidden client-side. A hidden panel that is still
    in the payload discloses everything it was hidden for."""
    write_settings({"ui.panels.status_summary": False})

    body = client.get(PANELS).json()

    assert "status_summary" not in body["panels"]
    assert body["status_summary"] is None


def test_switching_off_the_cluster_panel_removes_only_the_cluster(
    client, proxmox_configured, monkeypatch
):
    real_snapshot(monkeypatch)
    write_settings({"ui.panels.proxmox_cluster": False})

    body = client.get(PANELS).json()

    assert body["panels"] == ["status_summary", "proxmox_nodes"]
    assert body["proxmox"]["cluster"] is None
    assert body["proxmox"]["nodes"] is not None


def test_switching_off_the_nodes_panel_removes_only_the_nodes(
    client, proxmox_configured, monkeypatch
):
    real_snapshot(monkeypatch)
    write_settings({"ui.panels.proxmox_nodes": False})

    body = client.get(PANELS).json()

    assert body["proxmox"]["nodes"] is None
    assert body["proxmox"]["cluster"] is not None


def test_switching_off_both_proxmox_panels_drops_the_block(
    client, proxmox_configured, monkeypatch
):
    real_snapshot(monkeypatch)
    write_settings(
        {"ui.panels.proxmox_cluster": False, "ui.panels.proxmox_nodes": False}
    )

    body = client.get(PANELS).json()

    assert body["proxmox"] is None
    assert body["panels"] == ["status_summary"]


def test_the_landing_page_setting_is_served_to_the_dashboard(client):
    """§8: which page loads at `/` is a setting. The frontend cannot honour it
    without being told, and this is where it is told."""
    write_settings({"ui.landing_page": "dashboard"})

    assert client.get(PANELS).json()["landing_page"] == "dashboard"


# --- Status summary ---------------------------------------------------------


def test_the_counts_use_the_same_derivation_as_the_catalog(client):
    """`stale` and `off` are never stored, so counting the `health_status`
    column would be simpler, faster, and wrong."""
    make("up-one", health_status="up", health_checked_at=timezone.now())
    make("down-one", health_status="down", health_checked_at=timezone.now())
    make("never-checked")
    make("no-check", health_enabled=False)
    # Checked, but so long ago that the worker must be dead: `stale`, and the
    # stored column still says `up`.
    make(
        "stale-one",
        health_status="up",
        health_checked_at=timezone.now() - timedelta(hours=6),
    )

    summary = client.get(PANELS).json()["status_summary"]

    assert summary == {
        "total": 5,
        "up": 1,
        "down": 1,
        "stale": 1,
        "unknown": 1,
        "off": 1,
        "attention": summary["attention"],
        "attention_total": 3,
    }


def test_the_attention_list_is_worst_first_and_excludes_disabled_checks(client):
    """Checks being switched off is a decision somebody made, not a fault."""
    make("quiet", health_enabled=False)
    make("broken", health_status="down", health_checked_at=timezone.now())
    make("never")
    make(
        "frozen",
        health_status="up",
        health_checked_at=timezone.now() - timedelta(hours=6),
    )

    attention = client.get(PANELS).json()["status_summary"]["attention"]

    assert [row["status"] for row in attention] == ["down", "stale", "unknown"]
    assert "quiet" not in [row["slug"] for row in attention]


def test_the_attention_list_is_capped_and_says_how_many_it_left_out(client):
    """A panel is a glance; a list that can be 46 rows long is a second
    catalog."""
    for index in range(11):
        make(f"down-{index:02d}", health_status="down", health_checked_at=timezone.now())

    summary = client.get(PANELS).json()["status_summary"]

    # Five, by operator ruling of 2026-08-07 after the V1.5 validation pass —
    # eight rows already dominated the panel on a real catalog.
    assert len(summary["attention"]) == 5
    assert summary["attention_total"] == 11
