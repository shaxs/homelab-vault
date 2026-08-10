"""Settings values proven to reach their consumer — v1 §6a, standing rule 2.

**Why this file exists at all.** Every test in `test_settings.py` can pass —
the screen renders, saves, reads back, refuses the wrong types, scopes correctly
— while no worker ever reads the table. That is a fully green chunk with an inert
screen, and it surfaces months later when somebody changes a poll interval in
anger and nothing happens. V1.2 built the table and named V1.3 as the consumer of
these four values; this is the consumer's half of the contract.

One test per value: **change it, then assert the thing that consumes it behaves
differently.** Never "the setting reads back as 300".

`proxmox.polling.enabled` and `proxmox.poll_interval_s` are V1.4's to prove, and
the `ui.*` keys are V1.5's.
"""

import asyncio
from datetime import timedelta

import pytest
from django.utils import timezone

from services import notifications
from services.health.transitions import apply_result, is_due
from services.management.commands import health_worker
from services.models import Service, WorkerHeartbeat
from services.settings_store import write_settings

pytestmark = pytest.mark.django_db


def make(**kwargs):
    """A service with nothing pinned — which is what a new service looks like
    since V1.3, and the only shape that can follow a global."""
    defaults = {"slug": "svc", "name": "Svc", "launch_url": "https://svc.lab.local"}
    return Service.objects.create(**{**defaults, **kwargs})


async def _ok(service):
    return True


async def _fail(service):
    return False


# --- health.default_interval_s --------------------------------------------


def test_the_scheduler_follows_the_global_interval():
    """The consumer is `is_due`: change the interval and the worker's idea of
    which services are due changes with it."""
    now = timezone.now()
    service = make(health_checked_at=now - timedelta(seconds=120))
    assert service.health_interval_s is None  # follows the global

    write_settings({"health.default_interval_s": 3600})
    assert is_due(service, now) is False

    write_settings({"health.default_interval_s": 15})
    assert is_due(service, now) is True


def test_a_pinned_interval_beats_the_global_in_both_directions():
    """Null means follow, a number means pin — the distinction the nullable
    columns exist for."""
    now = timezone.now()
    pinned = make(slug="pinned", health_interval_s=3600, health_checked_at=now - timedelta(seconds=120))
    following = make(slug="following", health_checked_at=now - timedelta(seconds=120))

    write_settings({"health.default_interval_s": 15})
    assert is_due(pinned, now) is False
    assert is_due(following, now) is True


def test_staleness_follows_the_global_interval():
    """`stale` is 3 intervals (§8), and a service without its own interval must
    use the live global rather than a value stamped at insert."""
    now = timezone.now()
    service = make(health_status="up", health_checked_at=now - timedelta(seconds=300))

    write_settings({"health.default_interval_s": 60})  # stale after 180s
    assert service.effective_status(now) == "stale"

    write_settings({"health.default_interval_s": 600})  # stale after 1800s
    assert service.effective_status(now) == "up"


def test_the_api_publishes_the_effective_interval(client):
    """The catalog re-derives `stale` client-side (`frontend/src/status.js`).

    With only the raw nullable column in the response the browser would fall
    back to a hardcoded 60 and disagree with the server on every deployment that
    changed the interval — a status that differs depending on who you ask.
    """
    make()
    write_settings({"health.default_interval_s": 45})

    row = client.get("/api/v1/services").json()[0]
    assert row["health_interval_s"] is None
    assert row["health_interval_effective_s"] == 45


# --- health.fail_threshold -------------------------------------------------


def test_the_transition_rule_follows_the_global_fail_threshold():
    service = make(health_status="up")
    assert service.health_fail_threshold is None

    write_settings({"health.fail_threshold": 1})
    apply_result(service, ok=False)
    assert service.health_status == "down"


def test_raising_the_global_fail_threshold_holds_the_status():
    service = make(health_status="up")
    write_settings({"health.fail_threshold": 5})

    for _ in range(4):
        apply_result(service, ok=False)
    assert service.health_status == "up"

    apply_result(service, ok=False)
    assert service.health_status == "down"


# --- health.recovery_threshold ---------------------------------------------


def test_recovery_damping_follows_the_global_setting():
    service = make(health_status="down")
    write_settings({"health.recovery_threshold": 3})

    for _ in range(2):
        apply_result(service, ok=True)
    assert service.health_status == "down"

    apply_result(service, ok=True)
    assert service.health_status == "up"


def test_a_recovery_threshold_of_one_restores_immediately():
    """v0 §8's behaviour is still reachable — it is now a setting rather than
    the rule."""
    service = make(health_status="down")
    write_settings({"health.recovery_threshold": 1})
    apply_result(service, ok=True)
    assert service.health_status == "up"


# --- notifications.discord.enabled ----------------------------------------


@pytest.mark.django_db(transaction=True)
def test_the_worker_sends_nothing_while_discord_is_off(monkeypatch):
    dispatched = []
    monkeypatch.setattr(notifications, "dispatch", dispatched.append)
    monkeypatch.setattr(health_worker, "run_check_bounded", _fail)
    write_settings({"health.fail_threshold": 1})
    make(health_status="up")

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))

    assert Service.objects.get(slug="svc").health_status == "down"  # still checked
    assert dispatched == []


@pytest.mark.django_db(transaction=True)
def test_turning_discord_on_makes_the_worker_announce(monkeypatch):
    """The wire, end to end: a settings row, read by the worker, changing what
    reaches Discord."""
    dispatched = []
    monkeypatch.setattr(notifications, "dispatch", dispatched.append)
    monkeypatch.setattr(health_worker, "run_check_bounded", _fail)
    write_settings({"health.fail_threshold": 1, "notifications.discord.enabled": True})
    make(health_status="up", name="Karakeep")

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))

    assert len(dispatched) == 1
    assert "Karakeep" in dispatched[0]["content"]
    assert "down" in dispatched[0]["content"]


@pytest.mark.django_db(transaction=True)
def test_the_worker_announces_a_recovery(monkeypatch):
    dispatched = []
    monkeypatch.setattr(notifications, "dispatch", dispatched.append)
    monkeypatch.setattr(health_worker, "run_check_bounded", _ok)
    write_settings(
        {"health.recovery_threshold": 1, "notifications.discord.enabled": True}
    )
    make(health_status="down", name="Karakeep")

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))

    assert len(dispatched) == 1
    assert "back up" in dispatched[0]["content"]


@pytest.mark.django_db(transaction=True)
def test_a_notify_disabled_service_is_checked_by_the_worker_and_stays_silent(
    monkeypatch,
):
    """§4's separation, proven through the real worker rather than the helper."""
    dispatched = []
    monkeypatch.setattr(notifications, "dispatch", dispatched.append)
    monkeypatch.setattr(health_worker, "run_check_bounded", _fail)
    write_settings({"health.fail_threshold": 1, "notifications.discord.enabled": True})
    make(health_status="up", notify_enabled=False)

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))

    assert Service.objects.get(slug="svc").health_status == "down"
    assert dispatched == []


# --- The worker registers at all ------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_the_worker_registers_on_its_first_pass(monkeypatch):
    """There is no separate registration step — a worker registers by beating,
    which is what lets V1.4 add one without touching the deadman."""
    monkeypatch.setattr(health_worker, "run_check_bounded", _ok)
    make()

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))

    row = WorkerHeartbeat.objects.get(name="health")
    assert row.cycles == 1
    assert row.interval_s == 60  # the live global, written by the worker
    assert row.state == "ok"


@pytest.mark.django_db(transaction=True)
def test_a_restarted_worker_starts_its_cycle_count_again(monkeypatch):
    """Each `_run` is a process. `cycles` counts within one, so a counter that
    stopped moving means *this* process stopped working — which is the reading
    that makes the number worth storing."""
    monkeypatch.setattr(health_worker, "run_check_bounded", _ok)
    make()

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))
    first = WorkerHeartbeat.objects.get(name="health")

    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))
    second = WorkerHeartbeat.objects.get(name="health")

    assert second.cycles == 1
    assert second.started_at > first.started_at


@pytest.mark.django_db(transaction=True)
def test_the_worker_beats_even_with_an_empty_catalog(monkeypatch):
    """A worker with nothing to check is alive, not stalled. Beating only when
    services were checked would alert on an empty catalog — the state a fresh
    install is in."""
    monkeypatch.setattr(health_worker, "run_check_bounded", _ok)
    asyncio.run(health_worker._run(tick=0, concurrency=1, once=True))
    assert WorkerHeartbeat.objects.filter(name="health").exists()
