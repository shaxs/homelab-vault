"""The TrueNAS worker, worker isolation, and native-alert forwarding — TN2.

Three things this chunk adds beyond the TN1 provider, each proven against what the
**worker/poll** actually does rather than what a settings API returns (standing
rule 2 — a settings screen that saves and reads back while nothing consumes the
value is a green chunk with an inert screen):

1. **The worker is scoped to `"truenas"` alone** and registers against the V1.3
   heartbeat contract exactly as V1.4's Proxmox worker did — one key, judged by
   its own declared interval.
2. **Neither worker polls the other's provider** (§7c). This is the
   operator-named highest risk of the chunk: the old `for provider in PROVIDERS`
   pass would have made the Proxmox worker poll TrueNAS under Proxmox's switch and
   beat it into the heartbeat table as `"proxmox"`. Pinned in both directions.
3. **The poll caches `report()` and forwards newly-firing native alerts to
   Discord** — edge-triggered, sanitized (TN1's forward obligation), gated.
"""

import pytest
from django.core.management import call_command

from services.heartbeat import PROXMOX_WORKER, TRUENAS_WORKER
from services.models import ProviderSnapshot, WorkerHeartbeat
from services.providers import ProxmoxProvider, TrueNASProvider
from services.providers.poll import poll_provider
from services.settings_store import write_settings

from .proxmox_fakes import FakeCluster, install as install_proxmox
from .truenas_fakes import FakeTrueNAS, install as install_truenas

pytestmark = pytest.mark.django_db


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def truenas_env(settings):
    """Credentials in the environment, mirroring the deploy-time `TRUENAS_*`."""
    settings.TRUENAS_HOST = "https://truenas.test"
    settings.TRUENAS_API_KEY = "1-secretkey"
    settings.TRUENAS_API_USER = "hvault"
    settings.TRUENAS_VERIFY_TLS = False
    settings.TRUENAS_TIMEOUT_S = 15.0


@pytest.fixture
def truenas_configured(truenas_env, monkeypatch):
    """Configured, polling enabled, and pointed at the default fake box."""
    write_settings({"truenas.polling.enabled": True})
    return install_truenas(monkeypatch, TrueNASProvider(), FakeTrueNAS())


@pytest.fixture
def truenas_provider(truenas_env):
    """A provider instance for the direct-`poll_provider` alert tests."""
    return TrueNASProvider()


@pytest.fixture
def dispatched(monkeypatch):
    sent = []
    monkeypatch.setattr("services.notifications.dispatch", sent.append)
    return sent


def _enable_discord():
    write_settings({"notifications.discord.enabled": True})


def run_truenas():
    call_command("truenas_worker", once=True)


def run_proxmox():
    call_command("proxmox_worker", once=True)


#: One active CRITICAL alert with a stable uuid, for the forwarding tests.
def _alert(uuid="a-1", level="CRITICAL", formatted="Pool tank is DEGRADED.",
           dismissed=False):
    return {
        "uuid": uuid,
        "level": level,
        "dismissed": dismissed,
        "formatted": formatted,
        "datetime": {"$date": 1_700_000_000_000},
    }


# --- The worker registers and obeys its OWN settings (§6a, §5a) ------------


def test_the_truenas_worker_registers_by_beating(truenas_configured):
    """A third worker, added exactly as V1.4 predicted: one key, no deadman edit."""
    assert not WorkerHeartbeat.objects.filter(name=TRUENAS_WORKER).exists()

    run_truenas()

    row = WorkerHeartbeat.objects.get(name=TRUENAS_WORKER)
    assert row.cycles == 1
    assert row.state == WorkerHeartbeat.State.OK


def test_a_pass_caches_the_report_on_the_snapshot(truenas_configured):
    """The producer→consumer wire TN3 renders from: `report()` reaches the
    snapshot payload, no browser fetch involved."""
    run_truenas()

    snap = ProviderSnapshot.objects.get(provider="truenas")
    assert snap.ok is True
    # The report round-tripped through the cache — pools present, a degraded one.
    assert snap.payload["report"] is not None
    assert [p["name"] for p in snap.payload["report"]["pools"]] == ["tank", "backup"]
    assert snap.payload["report_detail"] == ""


def test_polling_disabled_makes_no_call_but_still_beats(truenas_configured):
    """"Polling is off" is a configuration state, not a stalled process — the
    worker must keep beating or the deadman fires for a healthy process."""
    write_settings({"truenas.polling.enabled": False})

    run_truenas()

    assert ProviderSnapshot.objects.filter(provider="truenas").count() == 0
    assert WorkerHeartbeat.objects.filter(name=TRUENAS_WORKER).exists()


def test_the_worker_beats_with_no_truenas_configured():
    """§7d: everything else identical with no TrueNAS key set."""
    write_settings({"truenas.polling.enabled": True})

    run_truenas()

    assert WorkerHeartbeat.objects.filter(name=TRUENAS_WORKER).exists()
    assert ProviderSnapshot.objects.filter(provider="truenas").count() == 0


@pytest.mark.parametrize("interval", [30, 300, 900])
def test_the_beat_carries_the_truenas_interval(truenas_configured, interval):
    """`truenas.poll_interval_s` reaches the heartbeat, which is what makes the
    deadman's threshold track it — its own key, not Proxmox's."""
    write_settings({"truenas.poll_interval_s": interval})

    run_truenas()

    row = WorkerHeartbeat.objects.get(name=TRUENAS_WORKER)
    assert row.interval_s == interval
    assert row.stale_after_s == 3 * interval


def test_the_interval_change_is_observed_without_a_restart(truenas_configured):
    """§5a's "re-derived live", read per pass. A startup snapshot would satisfy
    the wording and still be wrong when the interval later changes."""
    write_settings({"truenas.poll_interval_s": 120})
    run_truenas()
    assert WorkerHeartbeat.objects.get(name=TRUENAS_WORKER).stale_after_s == 360

    write_settings({"truenas.poll_interval_s": 900})
    run_truenas()
    assert WorkerHeartbeat.objects.get(name=TRUENAS_WORKER).stale_after_s == 2700


# --- The coupling regression: neither worker polls the other (§7c) ---------
#
# THE highest-risk latent bug of this chunk. Both providers are configured AND
# polling-enabled in each test, so the gate cannot short-circuit the proof: the
# only thing stopping a worker from polling the other provider is the per-provider
# scoping the refactor introduced.


def test_the_proxmox_worker_polls_proxmox_and_only_proxmox(
    settings, monkeypatch, truenas_configured
):
    """TrueNAS live and enabled, Proxmox live and enabled — run the PROXMOX
    worker. The old `for provider in PROVIDERS` loop would have polled TrueNAS
    here too, under `proxmox.polling.enabled`, and beat it as `"proxmox"`."""
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    write_settings({"proxmox.polling.enabled": True})
    install_proxmox(monkeypatch, ProxmoxProvider(), FakeCluster())

    run_proxmox()

    assert ProviderSnapshot.objects.filter(provider="proxmox").exists()
    assert not ProviderSnapshot.objects.filter(provider="truenas").exists()
    assert not WorkerHeartbeat.objects.filter(name=TRUENAS_WORKER).exists()


def test_the_truenas_worker_polls_truenas_and_only_truenas(
    settings, monkeypatch, truenas_configured
):
    """The mirror image: Proxmox live and enabled, run the TRUENAS worker. It
    must write no Proxmox snapshot and no Proxmox heartbeat."""
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    write_settings({"proxmox.polling.enabled": True})
    install_proxmox(monkeypatch, ProxmoxProvider(), FakeCluster())

    run_truenas()

    assert ProviderSnapshot.objects.filter(provider="truenas").exists()
    assert not ProviderSnapshot.objects.filter(provider="proxmox").exists()
    assert not WorkerHeartbeat.objects.filter(name=PROXMOX_WORKER).exists()


# --- Native-alert forwarding (TN1's obligation, §5) ------------------------
#
# Driven through `poll_provider` directly — the seam that owns the forwarding —
# rather than the worker, so each edge case is isolated from the loop.


def test_a_new_critical_alert_dispatches_once_and_does_not_re_fire(
    truenas_provider, monkeypatch, dispatched
):
    """Edge-triggered like every alert here: fires on the pass it appears, and the
    same alert on the next poll says nothing (the notified set rides the payload)."""
    _enable_discord()
    install_truenas(monkeypatch, truenas_provider, FakeTrueNAS(alerts=[_alert()]))

    poll_provider(truenas_provider)  # newly appeared — fires
    poll_provider(truenas_provider)  # still active — silent

    assert len(dispatched) == 1
    assert "truenas" in dispatched[0]["content"]
    snap = ProviderSnapshot.objects.get(provider="truenas")
    assert snap.payload["notified_alert_keys"] == ["a-1"]


def test_a_cleared_alert_recovers_exactly_once(
    truenas_provider, monkeypatch, dispatched
):
    _enable_discord()
    box = install_truenas(monkeypatch, truenas_provider, FakeTrueNAS(alerts=[_alert()]))

    poll_provider(truenas_provider)  # fires
    box.alerts = []  # operator cleared it on the box
    poll_provider(truenas_provider)  # recovery — once
    poll_provider(truenas_provider)  # nothing active, nothing to clear — silent

    assert len(dispatched) == 2
    assert "cleared" in dispatched[1]["content"].lower()
    snap = ProviderSnapshot.objects.get(provider="truenas")
    assert snap.payload["notified_alert_keys"] == []


def test_the_alert_text_is_sanitized_before_discord(
    truenas_provider, monkeypatch, dispatched
):
    """TN1's forward obligation: the provider-authored `formatted` text is
    untrusted (names datasets a hostile actor could set), so it is `sanitize()`d —
    markdown neutralised — before it can reach Discord."""
    _enable_discord()
    hostile = _alert(uuid="h-1", formatted="@everyone **pwned** dataset")
    install_truenas(monkeypatch, truenas_provider, FakeTrueNAS(alerts=[hostile]))

    poll_provider(truenas_provider)

    value = dispatched[0]["embed"]["fields"][2]["value"]
    assert "pwned" in value  # the text itself survives, for the operator to read
    assert "**pwned**" not in value  # but the markdown is escaped (sanitize ran)


def test_nothing_dispatches_when_discord_is_off_but_the_latch_advances(
    truenas_provider, monkeypatch, dispatched
):
    """Discord off: no message — but the notified set still advances, so switching
    Discord on mid-alert does not replay every alert already standing (the
    documented provider-alert latch, applied to native alerts)."""
    # notifications.discord.enabled defaults False — deliberately NOT enabled.
    install_truenas(monkeypatch, truenas_provider, FakeTrueNAS(alerts=[_alert()]))

    poll_provider(truenas_provider)

    assert dispatched == []
    snap = ProviderSnapshot.objects.get(provider="truenas")
    assert snap.payload["notified_alert_keys"] == ["a-1"]


def test_a_sub_error_alert_is_never_forwarded(
    truenas_provider, monkeypatch, dispatched
):
    """"Critical/high" is ERROR and above. A WARNING is noteworthy, not an
    incident — it must not ping, and must not enter the notified set (or it would
    block a later CRITICAL with the same uuid from ever firing)."""
    _enable_discord()
    warn = _alert(uuid="w-1", level="WARNING", formatted="noteworthy, not critical")
    install_truenas(monkeypatch, truenas_provider, FakeTrueNAS(alerts=[warn]))

    poll_provider(truenas_provider)

    assert dispatched == []
    snap = ProviderSnapshot.objects.get(provider="truenas")
    assert snap.payload["notified_alert_keys"] == []


def test_a_provider_without_report_forwards_nothing_and_caches_null(
    settings, monkeypatch, dispatched
):
    """The reflection is additive: Proxmox defines no `report()`, so its snapshot
    carries `report: None`, an empty notified set, and never dispatches a native
    alert — the existing poll-failure path is untouched."""
    _enable_discord()
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    install_proxmox(monkeypatch, ProxmoxProvider(), FakeCluster())

    row = poll_provider(ProxmoxProvider())

    assert row.payload["report"] is None
    assert row.payload["notified_alert_keys"] == []
    assert dispatched == []
