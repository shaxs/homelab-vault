import pytest
from django.test import Client

from services.probe import ProbeResult

ADMIN_TOKEN = "test-admin-token"
AGENT_TOKEN = "test-agent-token"


@pytest.fixture(scope="session", autouse=True)
def _release_worker_thread_connections(django_db_setup):
    """Hand back connections held by `sync_to_async` worker threads.

    A test that reaches the ORM through `sync_to_async` runs it on an asgiref
    thread-pool thread, which opens its own database connection and has nothing
    to close it. That session is still open when pytest-django tries to DROP the
    test database, which fails with *"database is being accessed by other
    users"* and leaves `test_homelabvault_dev` behind — breaking the **next**
    run rather than this one, which is what made it look intermittent.

    Production code owns its own thread boundaries (see services/db.py); this is
    the net under the test suite, so a newly-added `sync_to_async` call cannot
    orphan the database for whoever runs the suite next. Depending on
    `django_db_setup` is what orders this: pytest tears fixtures down in reverse
    setup order, so this runs *before* the database is dropped.
    """
    yield

    from asgiref.sync import SyncToAsync

    from services.db import close_this_threads_connections

    executor = getattr(SyncToAsync, "single_thread_executor", None)
    if executor is None:  # pragma: no cover - asgiref internals moved
        return
    try:
        executor.submit(close_this_threads_connections).result(timeout=10)
    except Exception:  # pragma: no cover - never fail teardown over cleanup
        pass


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.HOMELABVAULT_ADMIN_TOKEN = ADMIN_TOKEN
    settings.HOMELABVAULT_AGENT_TOKEN = AGENT_TOKEN
    return settings


@pytest.fixture(autouse=True)
def _isolated_cache(settings):
    """Production uses a file-based cache so the login throttle is shared across
    gunicorn workers; tests use locmem so they neither touch disk nor leak
    throttle counters into each other."""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test",
        }
    }
    from django.core.cache import cache

    cache.clear()


@pytest.fixture(autouse=True)
def _no_static_export(settings, tmp_path):
    """Keep the recovery export out of the repo during tests, and off the
    network path entirely."""
    settings.STATIC_EXPORT_ENABLED = False
    settings.STATIC_EXPORT_PATH = str(tmp_path / "export" / "index.html")
    settings.MEDIA_ROOT = str(tmp_path / "media")


@pytest.fixture(autouse=True)
def _no_autofill_probe(monkeypatch):
    """POST /api/v1/services probes internally on create (§4c). Tests must not hit
    the network for that; opt back in by monkeypatching probe_url yourself."""
    monkeypatch.setattr(
        "services.api.probe_url",
        lambda url: ProbeResult(ok=False, url=url, final_url=url, error="probe disabled"),
    )
    monkeypatch.setattr("services.api.validate_url", lambda url: url)


@pytest.fixture(autouse=True)
def _no_real_webhook(settings, monkeypatch):
    """No test may reach a real Discord webhook.

    Notifications are off by default in the settings registry, so nothing
    dispatches unless a test turns them on — but the dev box's `.env` may hold a
    real URL, and a test that enables notifications and forgets to stub the
    sender would post to the operator's actual channel. Clearing it here makes
    that impossible rather than unlikely: `send()` short-circuits when no URL
    resolves. Tests that exercise the transport set their own.
    """
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = ""
    monkeypatch.delenv("HOMELABVAULT_DISCORD_WEBHOOK_URL", raising=False)


@pytest.fixture(autouse=True)
def _no_real_proxmox(settings):
    """No test may reach the operator's real cluster (§7a).

    Exactly the reasoning behind `_no_real_webhook` above, and added the moment
    V1.4 put working credentials in the dev `.env`: a test that exercises the
    provider and forgets to stub the transport would otherwise poll the live
    Proxmox cluster — slowly, and differently on every machine, with results
    that depend on which containers happen to be running.

    Clearing both makes `is_configured()` false, so the provider short-circuits
    before constructing a client. Tests that need a configured provider set
    these themselves **and** stub the transport; `configured_proxmox` does both.
    """
    settings.PROXMOX_HOST = ""
    settings.PROXMOX_API_TOKEN = ""


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def agent_headers():
    return {"HTTP_AUTHORIZATION": f"Bearer {AGENT_TOKEN}"}


@pytest.fixture
def admin_headers():
    return {"HTTP_AUTHORIZATION": f"Bearer {ADMIN_TOKEN}"}
