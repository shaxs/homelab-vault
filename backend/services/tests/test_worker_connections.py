"""Connection hygiene on the threads this app starts itself.

Two bugs live here, both found the first time the suite ran against a real
PostgreSQL server and both structurally invisible on SQLite (its test database
is in-memory, and that backend's `_close()` refuses to close an in-memory
database, so every close request was silently discarded).

The second one is *intermittent by construction*: a leaked session does not fail
the run that leaks it. It fails the DROP DATABASE at the end of that run, which
orphans `test_homelabvault_dev`, which breaks the **next** run with "database
already exists". Asserting on the observable end state — the session count —
turns that into a deterministic failure in the run that causes it.
"""

import threading
import time

import pytest
from django.db import connection, transaction

from services import export_static
from services.models import Service

pytestmark = pytest.mark.django_db


@pytest.fixture
def export_path(settings, tmp_path):
    settings.STATIC_EXPORT_ENABLED = True
    path = tmp_path / "share" / "index.html"
    settings.STATIC_EXPORT_PATH = str(path)
    settings.STATIC_EXPORT_STALE_MARKER = str(tmp_path / "local" / "export.stale")
    return path


def _open_sessions() -> int:
    """Sessions on this database other than the one asking."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        return cursor.fetchone()[0]


def _settled_sessions(timeout: float = 10.0) -> int:
    """`_open_sessions()` once the value stops moving.

    PostgreSQL reaps a backend a moment after its client closes the socket, so a
    single read taken straight after a close is a race in the *test* rather than
    a finding. Two equal reads in a row is enough, and keeps a passing run fast.
    """
    deadline = time.monotonic() + timeout
    previous = _open_sessions()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        current = _open_sessions()
        if current == previous:
            return current
        previous = current
    return previous


def _drained_baseline() -> int:
    """Session count with the shared worker thread's connection already handed
    back.

    `sync_to_async` reuses one executor thread for the whole session, so an
    earlier test can leave a connection on it. Measuring a baseline that already
    includes that connection would let a genuine leak land on the same number
    and the assertion would pass against unfixed code — the exact way this bug
    hid in the first place. Drain first, so the baseline means something.
    """
    _release_worker_thread_connections()
    return _settled_sessions()


def _release_worker_thread_connections() -> None:
    from asgiref.sync import SyncToAsync

    from services.db import close_this_threads_connections

    executor = getattr(SyncToAsync, "single_thread_executor", None)
    if executor is not None:
        executor.submit(close_this_threads_connections).result(timeout=10)


# --- The export must not tear down a transaction it does not own ----------


def test_the_export_can_be_called_inside_a_transaction_it_does_not_own(export_path):
    """`run_static_export()` used to open with `close_old_connections()`.

    Called synchronously inside an atomic block, `close_if_unusable_or_obsolete()`
    compares `get_autocommit()` (False, in-atomic) against the configured
    AUTOCOMMIT (True), decides the connection is unusable, and closes it — and
    `close()` inside an atomic block sets `closed_in_transaction` while leaving
    `self.connection` pointing at the dead object, so `ensure_connection()` never
    reconnects. Every subsequent query then raises "the connection is closed".

    The assertion that matters is the query *after* the call, not the export.
    """
    Service.objects.create(
        slug="jellyfin", name="Jellyfin", launch_url="https://jellyfin.lab.local"
    )

    with transaction.atomic():
        assert export_static.run_static_export() is True
        # Same connection, still inside the same transaction.
        assert Service.objects.count() == 1

    assert Service.objects.get(slug="jellyfin").name == "Jellyfin"


def test_a_failed_export_also_leaves_the_transaction_usable(settings, tmp_path):
    """The failure path ran `close_old_connections()` in a `finally`, so it
    broke the caller's connection even when the export itself was handled."""
    Service.objects.create(
        slug="jellyfin", name="Jellyfin", launch_url="https://jellyfin.lab.local"
    )
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    settings.STATIC_EXPORT_PATH = str(blocker / "sub" / "index.html")
    settings.STATIC_EXPORT_STALE_MARKER = str(tmp_path / "local" / "export.stale")

    with transaction.atomic():
        assert export_static.run_static_export() is False
        assert Service.objects.count() == 1


# --- Threads we start must hand their sessions back -----------------------


@pytest.mark.django_db(transaction=True)
def test_the_export_thread_releases_its_database_session(export_path):
    """The production write path: on_commit -> daemon thread -> ORM.

    That thread gets its own connection and nothing closes it, so before the fix
    every catalog mutation leaked a PostgreSQL session for the life of the
    process.
    """
    baseline = _drained_baseline()

    thread = export_static._dispatch()
    thread.join(timeout=15)
    assert not thread.is_alive(), "export thread did not finish"

    assert _settled_sessions() == baseline


@pytest.mark.django_db(transaction=True)
def test_a_scheduled_export_releases_its_session(export_path):
    """Same thing through the real entry point, so the wiring is covered too."""
    baseline = _drained_baseline()

    with transaction.atomic():
        Service.objects.create(
            slug="karakeep", name="Karakeep", launch_url="https://karakeep.lab.local"
        )
        export_static.schedule_static_export()

    _join_export_threads()
    assert _settled_sessions() == baseline


@pytest.mark.django_db(transaction=True)
def test_a_worker_pass_releases_its_database_sessions(monkeypatch):
    """The health worker reaches the ORM through `sync_to_async`, which runs it
    on a thread-pool thread with its own connection. This is the leak that
    actually orphaned `test_homelabvault_dev`.

    Deliberately a *synchronous* test driving the loop with `asyncio.run`. Doing
    the counting through `sync_to_async` would run it on the very thread being
    measured, and `pg_backend_pid()` excludes the asking connection — so the
    leaked session would be the one row the query cannot see, and the test would
    pass against the unfixed code.
    """
    import asyncio

    from services.management.commands import health_worker

    _make_service()
    monkeypatch.setattr(health_worker, "run_check_bounded", _always_ok)

    baseline = _drained_baseline()

    async def one_pass():
        return await health_worker.run_pass(asyncio.Semaphore(2))

    asyncio.run(one_pass())

    assert _settled_sessions() == baseline


def _join_export_threads(timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if thread.name != "hlv-static-export":
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        assert not thread.is_alive(), "export thread did not finish"


def _make_service():
    return Service.objects.create(
        slug="svc", name="Svc", launch_url="https://svc.lab.local"
    )


async def _always_ok(service):
    return True


# --- The notification thread must not hold a connection either ------------


@pytest.mark.django_db(transaction=True)
def test_a_dispatched_notification_leaves_no_connection_behind(settings, monkeypatch):
    """V1.3's notification thread, held to the same rule as the export thread.

    This one was written into `notifications.py` as a design claim — "the thread
    does no ORM work" — before it was true: `send()` reached the settings table
    to find out which environment variable holds the webhook, so every dispatched
    notification opened a connection on a thread with nothing to close it.

    Nothing failed. That is the whole problem with this class of bug: the leak
    does not fail the run that leaks it, it fails that run's DROP DATABASE and
    breaks the **next** one. The suite found it only because the orphaned
    database was noticed by hand. Asserting the session count returns to a
    drained baseline makes it fail here instead.
    """
    from services import notifications

    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = "https://discord.example/hook"
    monkeypatch.setattr(
        notifications.httpx, "post", lambda url, **kw: _FakeResponse(204)
    )

    baseline = _drained_baseline()

    for _ in range(3):
        notifications.dispatch("hello").join(timeout=10)

    assert _settled_sessions() == baseline


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
