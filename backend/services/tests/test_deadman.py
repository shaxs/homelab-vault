"""The deadman, the heartbeat contract, and `/api/v1/healthz` — v1 §5a.

The failure this whole file exists for: the health worker dies at 02:00, the web
app keeps serving, every status freezes at its last value — almost certainly
`up` — and nothing is ever said, because notifications fire on transitions and
there are no transitions without checks.

**Isolation is proven here with two fixture heartbeats, not two processes.** The
registry is rows keyed by worker name, so the logic needs no second process to
test; §12's *"proven by stopping each real worker independently"* needs one and
belongs to V1.4, whose Proxmox worker is the second one (§5a's 2026-08-03 note,
standing rule 2).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from services import deadman, heartbeat
from services.models import Service, WorkerHeartbeat
from services.settings_store import write_settings

pytestmark = pytest.mark.django_db


def collector():
    sent = []
    return sent, sent.append


def register(name, interval_s=60, age_s=0, state="ok"):
    now = timezone.now()
    return WorkerHeartbeat.objects.create(
        name=name,
        interval_s=interval_s,
        started_at=now - timedelta(seconds=age_s + 1),
        last_beat_at=now - timedelta(seconds=age_s),
        cycles=5,
        state=state,
    )


# --- The contract V1.4 consumes -------------------------------------------


def test_a_worker_registers_by_beating():
    row = heartbeat.beat("health", interval_s=60)
    assert row.cycles == 1
    assert WorkerHeartbeat.objects.count() == 1


def test_beating_again_advances_the_cycle_counter_without_a_second_row():
    started = timezone.now()
    heartbeat.beat("health", interval_s=60, started_at=started)
    row = heartbeat.beat("health", interval_s=60, started_at=started)
    assert row.cycles == 2
    assert WorkerHeartbeat.objects.count() == 1


def test_a_restart_resets_the_counter():
    """`cycles` means "since this process started", which is what makes a
    counter that stopped moving readable."""
    heartbeat.beat("health", interval_s=60, started_at=timezone.now())
    row = heartbeat.beat(
        "health", interval_s=60, started_at=timezone.now() + timedelta(seconds=1)
    )
    assert row.cycles == 1


def test_workers_are_separate_rows():
    """V1.4 registers `proxmox` against this unchanged."""
    heartbeat.beat("health", interval_s=60)
    heartbeat.beat("proxmox", interval_s=120, started_at=timezone.now())
    assert set(WorkerHeartbeat.objects.values_list("name", flat=True)) == {
        "health",
        "proxmox",
    }


@pytest.mark.django_db(transaction=True)
def test_a_heartbeat_written_by_the_worker_is_readable_by_the_web_process():
    """§5a: web and worker are separate containers.

    A heartbeat on the worker's local filesystem is invisible to an evaluator on
    the web request path — the identical bug that stopped v0's export retry from
    ever firing (doc-drift 1g). The proof available in-process is the same one
    that matters: the beat is written on the worker's `sync_to_async` thread,
    through that thread's *own* database connection, and read back from the main
    thread's connection. Shared state that survives that is in Postgres.
    """
    import asyncio

    from asgiref.sync import sync_to_async

    from services.db import releases_connections

    async def worker_side():
        await sync_to_async(releases_connections(heartbeat.beat), thread_sensitive=False)(
            "health", interval_s=45
        )

    asyncio.run(worker_side())

    row = WorkerHeartbeat.objects.get(name="health")
    assert row.interval_s == 45


# --- Firing ----------------------------------------------------------------


def test_a_fresh_worker_does_not_alert():
    register("health", interval_s=60, age_s=10)
    sent, send = collector()
    assert deadman.evaluate(notify=send) == []
    assert sent == []


def test_a_stalled_worker_alerts():
    register("health", interval_s=60, age_s=200)  # threshold is 180
    sent, send = collector()

    moved = deadman.evaluate(notify=send)

    assert [m["transition"] for m in moved] == ["stalled"]
    assert len(sent) == 1
    assert "health" in sent[0]["content"]
    assert WorkerHeartbeat.objects.get(name="health").state == "stalled"


def test_the_threshold_is_three_intervals_not_a_hardcoded_number():
    """Hardcoding 180s goes quietly wrong the day somebody changes the interval
    — so check a worker whose interval is not 60."""
    register("slow", interval_s=600, age_s=1000)  # under 3x600
    register("fast", interval_s=10, age_s=40)  # over 3x10
    sent, send = collector()

    moved = deadman.evaluate(notify=send)

    assert [m["worker"] for m in moved] == ["fast"]


# --- Per worker, not global (§5a) -----------------------------------------


def test_one_stalled_worker_alerts_and_the_live_one_stays_quiet():
    """§12's unit-level deadman line. One dead worker must never be masked by
    another being alive, and must never drag the other into the alert."""
    register("health", interval_s=60, age_s=10)
    register("proxmox", interval_s=60, age_s=500)
    sent, send = collector()

    moved = deadman.evaluate(notify=send)

    assert [m["worker"] for m in moved] == ["proxmox"]
    assert len(sent) == 1
    assert "proxmox" in sent[0]["content"]
    # Nowhere in the whole message — content OR embed.
    assert "health" not in str(sent[0])
    assert WorkerHeartbeat.objects.get(name="health").state == "ok"
    assert WorkerHeartbeat.objects.get(name="proxmox").state == "stalled"


# --- Edge-triggered --------------------------------------------------------


def test_a_stalled_worker_does_not_re_alert_every_cycle():
    """Otherwise an overnight failure posts several hundred times before anyone
    wakes up, which is indistinguishable from not alerting at all."""
    register("health", interval_s=60, age_s=200)
    sent, send = collector()

    for _ in range(10):
        deadman.evaluate(notify=send)

    assert len(sent) == 1


def test_recovery_alerts_once_and_then_goes_quiet():
    register("health", interval_s=60, age_s=200)
    sent, send = collector()
    deadman.evaluate(notify=send)

    heartbeat.beat("health", interval_s=60)
    for _ in range(5):
        deadman.evaluate(notify=send)

    assert len(sent) == 2
    assert "reporting again" in sent[1]["content"]
    assert WorkerHeartbeat.objects.get(name="health").state == "ok"


def test_a_second_stall_after_a_recovery_alerts_again():
    """Edge-triggered, not once-per-lifetime."""
    register("health", interval_s=60, age_s=200)
    sent, send = collector()
    deadman.evaluate(notify=send)
    heartbeat.beat("health", interval_s=60)
    deadman.evaluate(notify=send)

    WorkerHeartbeat.objects.filter(name="health").update(
        last_beat_at=timezone.now() - timedelta(seconds=200)
    )
    deadman.evaluate(notify=send)

    assert [
        ("stall" in m["content"] or "stopped" in m["content"]) for m in sent
    ] == [True, False, True]


# --- Derived live, not snapshotted (§5a, 2026-08-03) ----------------------


def test_the_threshold_follows_the_settings_change_without_a_restart():
    """A value read once at process start satisfies §12's wording and is still
    wrong: move the interval 60 -> 300 with the deadman still on 180 and it
    fires *continuously*. Nothing is re-imported or restarted in this test."""
    heartbeat.beat("health", interval_s=60)
    WorkerHeartbeat.objects.filter(name="health").update(
        last_beat_at=timezone.now() - timedelta(seconds=200)
    )
    sent, send = collector()
    assert len(deadman.evaluate(notify=send)) == 1  # 200s > 3x60

    # The operator raises the interval; the worker's next beat carries it.
    write_settings({"health.default_interval_s": 300})
    heartbeat.beat("health", interval_s=300)
    WorkerHeartbeat.objects.filter(name="health").update(
        state="ok",
        last_beat_at=timezone.now() - timedelta(seconds=200),
    )

    sent2, send2 = collector()
    assert deadman.evaluate(notify=send2) == []  # 200s < 3x300
    assert WorkerHeartbeat.objects.get(name="health").stale_after_s == 900


# transaction=True for the same reason as in test_health_worker.py: the worker
# reaches the ORM through `sync_to_async`, whose writes land on a thread-pool
# connection outside this test's transaction and are therefore never rolled back.
@pytest.mark.django_db(transaction=True)
def test_the_worker_writes_the_live_interval_on_every_beat():
    """The wire that makes the test above true: the worker re-reads the setting
    each pass rather than holding the value it started with."""
    import asyncio

    from services.management.commands import health_worker

    write_settings({"health.default_interval_s": 45})
    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))
    assert WorkerHeartbeat.objects.get(name="health").interval_s == 45

    write_settings({"health.default_interval_s": 90})
    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))
    assert WorkerHeartbeat.objects.get(name="health").interval_s == 90


# --- The real worker, started and stopped ---------------------------------


@pytest.mark.django_db(transaction=True)
def test_stopping_the_real_health_worker_fires_its_deadman(monkeypatch):
    """§12: "the health worker stopped for real and its deadman fires".

    Not a fixture row — the actual `health_worker` command registers, then stops
    (the process exits after `--once`), and the evaluator notices on its own.
    Time is moved rather than waited for; the alternative is a test that sleeps
    for three minutes.
    """
    from services.management.commands import health_worker

    async def always_ok(service):
        return True

    monkeypatch.setattr(health_worker, "run_check_bounded", always_ok)
    Service.objects.create(
        slug="svc", name="Svc", launch_url="https://svc.lab.local"
    )

    import asyncio

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))
    sent, send = collector()
    assert deadman.evaluate(notify=send) == []  # running: silent

    # The worker is gone. Nothing writes a heartbeat from here on.
    WorkerHeartbeat.objects.filter(name="health").update(
        last_beat_at=timezone.now() - timedelta(seconds=3 * 60 + 1)
    )

    moved = deadman.evaluate(notify=send)
    assert [m["worker"] for m in moved] == ["health"]
    assert "stopped reporting" in sent[0]["content"]


# --- Discord on/off is a setting (standing rule 2) ------------------------


def test_the_stall_alert_obeys_the_discord_setting(monkeypatch):
    register("health", interval_s=60, age_s=200)
    dispatched = []
    monkeypatch.setattr(deadman.notifications, "dispatch", dispatched.append)

    deadman.evaluate()
    assert dispatched == []  # default is off

    WorkerHeartbeat.objects.filter(name="health").update(state="ok")
    write_settings({"notifications.discord.enabled": True})
    deadman.evaluate()
    assert len(dispatched) == 1


# --- /api/v1/healthz -------------------------------------------------------


def test_healthz_reports_per_worker_freshness(client):
    register("health", interval_s=60, age_s=10)
    response = client.get("/api/v1/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert [w["name"] for w in body["workers"]] == ["health"]
    assert body["workers"][0]["stale"] is False


def test_healthz_serves_503_when_a_worker_is_stalled(client):
    """So a watcher can alert on the status line without parsing the body."""
    register("health", interval_s=60, age_s=200)
    response = client.get("/api/v1/healthz")

    assert response.status_code == 503
    assert response.json()["ok"] is False


def test_healthz_withholds_the_thresholds_from_anonymous_callers(client):
    """`health.default_interval_s` is deliberately not in the public settings
    whitelist (§6a); publishing 3x it here would hand back what the whitelist
    withholds."""
    register("health", interval_s=60, age_s=10)
    worker = client.get("/api/v1/healthz").json()["workers"][0]

    assert worker["interval_s"] is None
    assert worker["stale_after_s"] is None
    assert worker["cycles"] is None
    assert worker["started_at"] is None
    # Liveness itself is public: that is the point of the endpoint.
    assert worker["state"] == "ok"
    assert worker["last_beat_at"] is not None


def test_healthz_gives_an_admin_the_numbers(client, admin_headers):
    register("health", interval_s=60, age_s=10)
    body = client.get("/api/v1/healthz", **admin_headers).json()

    assert body["workers"][0]["interval_s"] == 60
    assert body["workers"][0]["stale_after_s"] == 180
    assert body["workers"][0]["cycles"] == 5
    assert body["notifications_configured"] is False


def test_healthz_names_no_service(client):
    """Different disclosure class: the catalog is public, but this endpoint is
    about infrastructure and has no reason to enumerate it."""
    Service.objects.create(
        slug="secret-service", name="Secret Service", launch_url="https://x.lab.local"
    )
    register("health", interval_s=60, age_s=10)

    body = client.get("/api/v1/healthz").content.decode()
    assert "secret-service" not in body
    assert "Secret Service" not in body


def test_healthz_evaluates_so_the_watcher_drives_the_internal_layer(monkeypatch, client):
    """§5a's two layers meet here: the thing that watches for a dead container
    also runs the check for a dead worker, so the internal layer does not depend
    on a cron entry somebody has to remember."""
    register("health", interval_s=60, age_s=200)
    write_settings({"notifications.discord.enabled": True})
    dispatched = []
    monkeypatch.setattr(deadman.notifications, "dispatch", dispatched.append)

    client.get("/api/v1/healthz")
    assert len(dispatched) == 1

    # Polled every ten seconds, it still says it once.
    for _ in range(5):
        client.get("/api/v1/healthz")
    assert len(dispatched) == 1


def test_healthz_is_ok_when_no_worker_has_ever_registered(client):
    """A worker nobody deployed is not a failure — inventing one would make the
    endpoint useless as a watcher target on a fresh install."""
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "workers": [],
        "checked_at": response.json()["checked_at"],
        "notifications_configured": None,
    }
