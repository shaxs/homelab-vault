"""Health worker transitions and scheduling — spec §8."""

from datetime import timedelta

import pytest
from django.utils import timezone

from services.health.registry import CHECKERS
from services.health.transitions import apply_result, is_due
from services.models import Service

pytestmark = pytest.mark.django_db


def make(**kwargs):
    defaults = {"slug": "svc", "name": "Svc", "launch_url": "https://svc.lab.local"}
    return Service.objects.create(**{**defaults, **kwargs})


# --- Stored transitions ---------------------------------------------------


def test_failures_below_threshold_do_not_mark_down():
    """The threshold is retained because it prevents flapping (§8)."""
    service = make(health_status="up", health_fail_threshold=3)
    for _ in range(2):
        apply_result(service, ok=False)
    assert service.health_status == "up"
    assert service.health_consec_failures == 2


def test_threshold_reached_marks_down():
    service = make(health_status="up", health_fail_threshold=3)
    for _ in range(3):
        apply_result(service, ok=False)
    assert service.health_status == "down"


def test_recovery_is_damped_and_the_failure_counter_resets_at_once():
    """v0 §8's "any success -> up immediately" is amended by v1 §5.

    The counter still resets on the first success — that part was never about
    flapping — but the *status* waits for `health.recovery_threshold` successes,
    or one service alternating down/up floods Discord until somebody mutes the
    channel, taking the real alerts with it.
    """
    service = make(health_status="down", health_consec_failures=9)
    apply_result(service, ok=True)  # default recovery threshold is 2
    assert service.health_status == "down"
    assert service.health_consec_failures == 0
    assert service.health_consec_successes == 1

    apply_result(service, ok=True)
    assert service.health_status == "up"


def test_first_contact_is_not_damped():
    """A service that was never down has no flap to damp — it goes up on its
    first success, as it always did. Otherwise a bulk import sits at `unknown`
    for two cycles and reads as broken."""
    service = make(health_status="unknown")
    apply_result(service, ok=True)
    assert service.health_status == "up"


def test_health_changed_at_moves_only_on_transition():
    """"`health_changed_at` gives 'down for 3 days' in the UI without a history
    table" (§8) — so it must not be bumped by every check."""
    # A new service starts at `unknown`, so the first success is a transition.
    service = make(health_fail_threshold=1)
    apply_result(service, ok=True)
    assert service.health_status == "up"
    assert service.health_changed_at is not None

    first_change = service.health_changed_at
    apply_result(service, ok=True)
    assert service.health_changed_at == first_change

    apply_result(service, ok=False)
    assert service.health_status == "down"
    assert service.health_changed_at > first_change


def test_apply_result_reports_changed_fields_only():
    service = make(health_status="up")
    applied = apply_result(service, ok=True)
    assert set(applied.changed) <= {
        "health_checked_at",
        "health_consec_failures",
        "health_consec_successes",
        "health_status",
        "health_changed_at",
    }
    assert applied.transition is None


# --- Scheduling -----------------------------------------------------------


def test_never_checked_is_due():
    assert is_due(make()) is True


def test_disabled_service_is_never_due():
    assert is_due(make(health_enabled=False)) is False


def test_per_service_interval_is_honoured():
    """The worker must respect health_interval_s rather than sweeping
    everything on a fixed 60s cycle (§8)."""
    now = timezone.now()
    slow = make(slug="slow", health_interval_s=600, health_checked_at=now - timedelta(seconds=120))
    fast = make(slug="fast", health_interval_s=30, health_checked_at=now - timedelta(seconds=120))
    assert is_due(slow, now) is False
    assert is_due(fast, now) is True


# --- Registry -------------------------------------------------------------


def test_registry_ships_http_and_tcp():
    assert set(CHECKERS) == {"http", "tcp"}


# transaction=True is required, not stylistic: the worker reaches the ORM through
# sync_to_async, which runs on a thread-pool executor with its own database
# connection. Those writes are invisible to the surrounding test transaction and
# are never rolled back, so with the default django_db they leak into every test
# that runs afterwards.
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_worker_pass_records_a_result(monkeypatch):
    import asyncio

    from services.management.commands import health_worker

    service = await _acreate()
    # The worker calls run_check_bounded, not run_check, since audit S1.
    monkeypatch.setattr(health_worker, "run_check_bounded", _always_ok)

    await health_worker.run_pass(asyncio.Semaphore(2))

    refreshed = await _aget(service.pk)
    assert refreshed.health_status == "up"
    assert refreshed.health_checked_at is not None


async def _always_ok(service):
    return True


# --- Timeout bounds and off-loop DNS (audit S1, S2) -----------------------


@pytest.mark.asyncio
async def test_a_stalled_check_cannot_hold_the_pass(monkeypatch):
    """One check that never returns must not stall the gathered pass — no later
    pass or beat would run and the deadman would fire on the worker itself
    (audit S1). `run_check_bounded` caps it. The outer wait_for is the test's
    own safety net so a regression hangs the test rather than the suite."""
    import asyncio

    from services.health import registry

    async def never_returns(service):
        await asyncio.sleep(3600)

    monkeypatch.setitem(registry.CHECKERS, "http", never_returns)
    service = Service(slug="s", name="S", launch_url="https://s.lab.local")

    result = await asyncio.wait_for(
        registry.run_check_bounded(service, ceiling_s=0.05), timeout=5
    )
    assert result is False


@pytest.mark.asyncio
async def test_http_check_resolves_dns_off_the_event_loop(monkeypatch):
    """`validate_url` resolves DNS with a blocking `getaddrinfo`; run on the
    loop, one slow resolve serialises every concurrent check and can trip the
    deadman (audit S2). It must run on a worker thread.

    Asserted by recording WHICH thread runs it — the backlog-window lesson:
    when the property is *where* code executes, a timing or count proxy passes
    in both worlds, so the execution context itself is what to record."""
    import threading

    from services.health import http as health_http
    from services.probe import ProbeRejected

    loop_thread = threading.current_thread()
    ran_on = {}

    def recording_validate(url):
        ran_on["thread"] = threading.current_thread()
        raise ProbeRejected("stop before any network call")

    monkeypatch.setattr(health_http, "validate_url", recording_validate)
    service = Service(slug="s", name="S", launch_url="https://s.lab.local")

    assert await health_http.check_http(service) is False
    assert ran_on["thread"] is not loop_thread


@pytest.mark.asyncio
async def test_tcp_check_resolves_dns_off_the_event_loop(monkeypatch):
    """Same property for the tcp checker (audit S2)."""
    import threading

    from services.health import tcp as health_tcp
    from services.probe import ProbeRejected

    loop_thread = threading.current_thread()
    ran_on = {}

    def recording_resolve(host):
        ran_on["thread"] = threading.current_thread()
        raise ProbeRejected("stop before any connection")

    monkeypatch.setattr(health_tcp, "resolve_host", recording_resolve)
    service = Service(
        slug="s", name="S", launch_url="https://s.lab.local",
        health_url="tcp://s.lab.local:22",
    )

    assert await health_tcp.check_tcp(service) is False
    assert ran_on["thread"] is not loop_thread


@pytest.mark.asyncio
async def test_a_tcp_url_with_an_out_of_range_port_fails_the_check_not_the_worker():
    """`urlsplit(...).port` raises ValueError on `:99999`, health_url is
    agent-writable, and an exception out of a checker is not a failed check —
    it is a broken pass (audit S9's class, at the tcp seam)."""
    from services.health import tcp as health_tcp

    service = Service(
        slug="s", name="S", launch_url="https://s.lab.local",
        health_url="tcp://s.lab.local:99999",
    )

    assert await health_tcp.check_tcp(service) is False


async def _acreate():
    from asgiref.sync import sync_to_async

    return await sync_to_async(make)()


async def _aget(pk):
    from asgiref.sync import sync_to_async

    return await sync_to_async(Service.objects.get)(pk=pk)
