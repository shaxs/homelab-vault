"""The Proxmox worker — v1 §7c, §6a, §5a.

**This file closes V1.3's other half** (standing rule 2: the consumer proves the
wire). V1.3 proved deadman isolation with two *fixture* heartbeats and said the
integration proof — two real processes, stopped independently — belongs here,
because the second worker is this chunk's. It is `test_each_real_worker_*` below.

It also owns the Proxmox pair of settings values (§6a). Every one of those tests
asserts what the **worker** does, never what the settings API returns: a settings
screen that saves and reads back while no worker ever reads the table is a fully
green chunk with an inert screen.
"""

import pytest
from django.core.management import call_command
from django.utils import timezone

from services import deadman
from services.heartbeat import HEALTH_WORKER, PROXMOX_WORKER, beat
from services.management.commands import proxmox_worker
from services.models import ProviderSnapshot, Service, ServiceLink, WorkerHeartbeat
from services.providers import ProxmoxProvider
from services.settings_store import write_settings

from .proxmox_fakes import FakeCluster, install, install_failure

pytestmark = pytest.mark.django_db


@pytest.fixture
def configured(settings, monkeypatch):
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=hlv@pve!readonly=secret-uuid"
    write_settings({"proxmox.polling.enabled": True})
    return install(monkeypatch, ProxmoxProvider(), FakeCluster())


def run_once():
    call_command("proxmox_worker", once=True)


# --- Settings drive the worker (§6a, standing rule 2) ----------------------


def test_polling_disabled_means_the_worker_makes_no_call(settings, monkeypatch):
    """`proxmox.polling.enabled` is the switch, and the worker is what must obey
    it — not the settings API."""
    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    cluster = install(monkeypatch, ProxmoxProvider(), FakeCluster())
    write_settings({"proxmox.polling.enabled": False})

    run_once()

    assert cluster.requests == []
    assert ProviderSnapshot.objects.count() == 0


def test_enabling_polling_takes_effect_on_the_next_cycle_with_no_restart(configured):
    """Read live, per pass. A value captured above the loop satisfies "the worker
    reads the setting" and is still wrong."""
    write_settings({"proxmox.polling.enabled": False})
    run_once()
    assert ProviderSnapshot.objects.count() == 0

    write_settings({"proxmox.polling.enabled": True})
    run_once()
    assert ProviderSnapshot.objects.get(provider="proxmox").ok is True


@pytest.mark.parametrize("interval", [30, 300, 900])
def test_the_beat_carries_the_interval_from_settings(configured, interval):
    """`proxmox.poll_interval_s` reaches the heartbeat, which is what makes the
    deadman's threshold track it."""
    write_settings({"proxmox.poll_interval_s": interval})

    run_once()

    row = WorkerHeartbeat.objects.get(name=PROXMOX_WORKER)
    assert row.interval_s == interval
    assert row.stale_after_s == 3 * interval


def test_the_stall_threshold_moves_when_the_interval_changes_without_a_restart(configured):
    """§5a's "re-derived live". A startup snapshot satisfies §12's wording and is
    still wrong: 120s -> 900s with the deadman still on 360s fires continuously."""
    write_settings({"proxmox.poll_interval_s": 120})
    run_once()
    assert WorkerHeartbeat.objects.get(name=PROXMOX_WORKER).stale_after_s == 360

    write_settings({"proxmox.poll_interval_s": 900})
    run_once()
    assert WorkerHeartbeat.objects.get(name=PROXMOX_WORKER).stale_after_s == 2700


# --- Registration against V1.3's contract ----------------------------------


def test_the_worker_registers_by_beating(configured):
    """One call, no deadman edit, no new table — the contract kept rather than
    asserted."""
    assert not WorkerHeartbeat.objects.filter(name=PROXMOX_WORKER).exists()

    run_once()

    row = WorkerHeartbeat.objects.get(name=PROXMOX_WORKER)
    assert row.cycles == 1
    assert row.state == WorkerHeartbeat.State.OK


def test_the_worker_beats_even_when_polling_is_disabled(settings):
    """"Polling is off" is a configuration state, not a stalled process. A worker
    that stopped beating because a setting was switched off would fire the
    deadman for a process that is running perfectly."""
    write_settings({"proxmox.polling.enabled": False})

    run_once()

    assert WorkerHeartbeat.objects.filter(name=PROXMOX_WORKER).exists()


def test_the_worker_beats_even_with_no_provider_configured():
    """§7d: everything else identical with no Proxmox token."""
    run_once()
    assert WorkerHeartbeat.objects.filter(name=PROXMOX_WORKER).exists()


def test_a_failing_provider_does_not_kill_the_worker(settings, monkeypatch):
    import httpx

    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    write_settings({"proxmox.polling.enabled": True})
    install_failure(monkeypatch, ProxmoxProvider(), exc=httpx.ConnectError("down"))

    run_once()  # must not raise

    assert WorkerHeartbeat.objects.filter(name=PROXMOX_WORKER).exists()
    assert ProviderSnapshot.objects.get(provider="proxmox").ok is False


def test_a_pass_writes_links(configured):
    Service.objects.create(
        slug="media", name="media", launch_url="http://192.168.0.125:8989/"
    )
    run_once()
    assert ServiceLink.objects.get().ref == "105"


# --- The deadman, with two REAL workers (§5a, §12) -------------------------
#
# V1.3 proved the isolation logic with two fixture heartbeats and explicitly
# deferred this half: "the integration proof, stopping two real processes,
# belongs to V1.4 because the second worker is V1.4's."


# `transaction=True` on every test below that runs the REAL health worker: it
# reaches the ORM through `sync_to_async`, which runs on a thread-pool thread
# with its own connection, and that connection cannot see a test transaction's
# uncommitted rows. Without it the worker reads registry defaults instead of the
# settings the test just wrote — and the failure is silent and confusing, because
# the worker looks like it is ignoring the setting. The Proxmox worker is
# synchronous and needs none of this.


@pytest.mark.django_db(transaction=True)
def test_both_real_workers_register_independently(configured):
    call_command("health_worker", once=True)
    run_once()

    names = set(WorkerHeartbeat.objects.values_list("name", flat=True))
    assert names == {HEALTH_WORKER, PROXMOX_WORKER}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "stalled, alive", [(PROXMOX_WORKER, HEALTH_WORKER), (HEALTH_WORKER, PROXMOX_WORKER)]
)
def test_each_real_worker_stopped_independently_fires_only_its_own_deadman(
    configured, stalled, alive
):
    """§12's end-to-end line, and it could not be met before a second worker
    existed. Asserted in **both** directions and for **both** workers: the
    stopped one alerts, and the running one is proven silent.

    "Stopping" a worker in a test is not running its next cycle — which is
    exactly what a dead process does.
    """
    call_command("health_worker", once=True)
    run_once()

    # Age the stalled worker past its own threshold; leave the other current.
    row = WorkerHeartbeat.objects.get(name=stalled)
    row.last_beat_at = timezone.now() - timezone.timedelta(
        seconds=row.stale_after_s + 60
    )
    row.save(update_fields=["last_beat_at"])

    sent = []
    transitions = deadman.evaluate(notify=sent.append)

    assert [t["worker"] for t in transitions] == [stalled]
    assert WorkerHeartbeat.objects.get(name=stalled).state == "stalled"
    assert WorkerHeartbeat.objects.get(name=alive).state == "ok"
    assert len(sent) == 1
    assert stalled in sent[0]["content"]
    assert alive not in str(sent[0])


@pytest.mark.django_db(transaction=True)
def test_each_worker_is_judged_by_its_own_interval(configured):
    """A worker on 900s and one on 15s must not share a threshold — the reason
    the interval rides on the beat rather than living in the evaluator."""
    write_settings({"proxmox.poll_interval_s": 900, "health.default_interval_s": 15})
    call_command("health_worker", once=True)
    run_once()

    stale_after = dict(
        WorkerHeartbeat.objects.values_list("name", "interval_s")
    )
    assert stale_after[PROXMOX_WORKER] == 900
    assert stale_after[HEALTH_WORKER] == 15

    # 300s of silence: well past health's 45s threshold, nowhere near proxmox's 2700s.
    old = timezone.now() - timezone.timedelta(seconds=300)
    WorkerHeartbeat.objects.all().update(last_beat_at=old)

    sent = []
    transitions = deadman.evaluate(notify=sent.append)

    assert [t["worker"] for t in transitions] == [HEALTH_WORKER]


@pytest.mark.django_db(transaction=True)
def test_the_deadman_needed_no_change_to_support_a_second_worker():
    """The contract V1.3 wrote down, verified as behaviour rather than by
    grepping the source (audit T6 — the old assertion failed on an innocent
    comment and passed on a differently-named branch): a worker the evaluator
    has never heard of registers by beating and is judged by its own declared
    interval, with no registry, no name list, and no edit anywhere."""
    from services.heartbeat import beat

    name = "a-worker-invented-by-this-test"
    beat(name, interval_s=30)
    WorkerHeartbeat.objects.filter(pk=name).update(
        last_beat_at=timezone.now() - timezone.timedelta(seconds=120)
    )

    sent = []
    transitions = deadman.evaluate(notify=sent.append)

    assert [t["worker"] for t in transitions] == [name]


# --- Provider unreachable never blocks a write or a probe (§7c) ------------


def test_a_catalog_write_succeeds_while_the_provider_is_unreachable(
    client, agent_headers, settings, monkeypatch
):
    import httpx

    settings.PROXMOX_HOST = "https://pve.test:8006"
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=x@pve!y=z"
    write_settings({"proxmox.polling.enabled": True})
    install_failure(monkeypatch, ProxmoxProvider(), exc=httpx.ConnectError("down"))

    response = client.post(
        "/api/v1/services",
        data={"launch_url": "https://newthing.lab.local"},
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 201


def test_the_catalog_read_never_calls_the_provider(client, configured):
    Service.objects.create(
        slug="media", name="media", launch_url="http://192.168.0.125:8989/"
    )
    run_once()
    before = len(configured.requests)

    assert client.get("/api/v1/services").status_code == 200

    assert len(configured.requests) == before


def test_the_settings_screen_never_calls_the_provider(client, admin_headers, configured):
    """A settings screen that polls the cluster is the shape §8 warns gets
    rebuilt — and it would hang the admin page whenever Proxmox is down."""
    run_once()
    before = len(configured.requests)

    response = client.get("/api/v1/settings", **admin_headers)

    assert response.status_code == 200
    assert len(configured.requests) == before


# --- The worker's own loop -------------------------------------------------


def test_run_pass_returns_a_summary_per_configured_provider(configured):
    summaries = proxmox_worker.run_pass()
    assert [s["provider"] for s in summaries] == ["proxmox"]
    assert summaries[0]["ok"] is True
