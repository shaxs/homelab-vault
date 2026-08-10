"""Health status model — spec §8."""

from datetime import timedelta

import pytest
from django.utils import timezone

from services.models import STATUS_OFF, STATUS_STALE, STATUS_UNKNOWN, Service

pytestmark = pytest.mark.django_db


def make(**kwargs):
    defaults = {
        "slug": "svc",
        "name": "Svc",
        "launch_url": "https://svc.lab.local",
    }
    return Service.objects.create(**{**defaults, **kwargs})


def test_never_checked_is_unknown():
    assert make().effective_status() == STATUS_UNKNOWN


def test_recent_check_reports_the_stored_status():
    service = make(health_status="up", health_checked_at=timezone.now())
    assert service.effective_status() == "up"


def test_stale_is_derived_after_three_intervals():
    """"`stale` is derived on read, never stored" (§8) — the condition that
    produces it is the health worker being dead, and a dead worker cannot write
    `stale` to the database."""
    service = make(
        health_status="up",
        health_interval_s=60,
        health_checked_at=timezone.now() - timedelta(seconds=181),
    )
    assert service.effective_status() == STATUS_STALE
    # Nothing was written — the stored value is untouched.
    assert service.health_status == "up"


def test_just_inside_the_stale_window_is_not_stale():
    service = make(
        health_status="up",
        health_interval_s=60,
        health_checked_at=timezone.now() - timedelta(seconds=179),
    )
    assert service.effective_status() == "up"


def test_stale_honours_per_service_interval():
    service = make(
        health_status="up",
        health_interval_s=600,
        health_checked_at=timezone.now() - timedelta(seconds=400),
    )
    assert service.effective_status() == "up"


def test_a_down_service_still_goes_stale_when_the_worker_dies():
    service = make(
        health_status="down",
        health_interval_s=60,
        health_checked_at=timezone.now() - timedelta(hours=1),
    )
    assert service.effective_status() == STATUS_STALE


def test_checks_disabled_reports_off():
    service = make(health_enabled=False, health_checked_at=timezone.now())
    assert service.effective_status() == STATUS_OFF


def test_api_serialises_the_derived_status(client):
    make(
        health_status="up",
        health_interval_s=60,
        health_checked_at=timezone.now() - timedelta(hours=1),
    )
    payload = client.get("/api/v1/services/svc").json()
    assert payload["status"] == STATUS_STALE
    assert payload["health_status"] == "up"


def test_check_type_registry_key_comes_from_the_scheme():
    assert make(slug="a", launch_url="https://a.lab.local").check_type == "http"
    assert (
        make(slug="b", launch_url="https://b.lab.local", health_url="tcp://b.lab:22").check_type
        == "tcp"
    )


@pytest.mark.parametrize(
    "stored,expected",
    [
        (-5000, Service.MIN_TIMEOUT_MS),
        (5, Service.MIN_TIMEOUT_MS),
        (2147483647, Service.MAX_TIMEOUT_MS),
        (5000, 5000),
        (None, Service.MIN_TIMEOUT_MS),
    ],
)
def test_clamped_timeout_never_leaves_the_bounds(stored, expected):
    """`clean()` bounds the API write path, but a row written around it (manual
    psql, a migration) must not hand the worker an unbounded httpx.Timeout. The
    checkers honour `clamped_timeout_ms`, which fails closed to the bounds — the
    same two-layer shape as the auth-mode coercion (audit S1)."""
    svc = Service(
        slug="s", name="S", launch_url="https://s.lab.local",
        health_timeout_ms=stored,
    )
    assert svc.clamped_timeout_ms == expected
