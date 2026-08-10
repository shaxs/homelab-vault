"""On-demand health re-check — waiting out an interval to confirm a fix is
tedious, but a manual check must not behave differently from the worker's."""

import pytest
from django.utils import timezone

from services.models import Service

pytestmark = pytest.mark.django_db


@pytest.fixture
def service():
    return Service.objects.create(
        slug="svc", name="Svc", launch_url="https://svc.lab.local"
    )


@pytest.fixture
def check_returns(monkeypatch):
    def _install(ok):
        async def fake(_service):
            return ok

        # The recheck endpoint calls run_check_bounded since audit S1.
        monkeypatch.setattr("services.api.run_check_bounded", fake)

    return _install


def test_a_successful_check_records_up(client, agent_headers, service, check_returns):
    check_returns(True)
    response = client.post("/api/v1/health/svc/check", **agent_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "up"
    assert body["health_checked_at"] is not None

    service.refresh_from_db()
    assert service.health_status == "up"


def test_one_failure_does_not_force_down(client, agent_headers, service, check_returns):
    """The threshold exists to prevent flapping (§8); a manual check must respect
    it or the button becomes a way to mark anything down instantly."""
    check_returns(False)
    response = client.post("/api/v1/health/svc/check", **agent_headers)

    assert response.status_code == 200
    service.refresh_from_db()
    assert service.health_consec_failures == 1
    assert service.health_status == "unknown"


def test_repeated_failures_reach_the_threshold(
    client, agent_headers, service, check_returns
):
    check_returns(False)
    for _ in range(3):
        client.post("/api/v1/health/svc/check", **agent_headers)

    service.refresh_from_db()
    assert service.health_status == "down"


def test_a_success_after_failures_resets(client, agent_headers, service, check_returns):
    """The failure counter resets on the first success; the *status* does not.

    Recovery is damped by `health.recovery_threshold` as of v1 §5, and a manual
    recheck deliberately obeys the same rules as the worker — a button that
    could produce a status the worker disagrees with is worse than no button.
    """
    check_returns(False)
    for _ in range(3):
        client.post("/api/v1/health/svc/check", **agent_headers)
    check_returns(True)
    client.post("/api/v1/health/svc/check", **agent_headers)

    service.refresh_from_db()
    assert service.health_status == "down"
    assert service.health_consec_failures == 0
    assert service.health_consec_successes == 1

    client.post("/api/v1/health/svc/check", **agent_headers)
    service.refresh_from_db()
    assert service.health_status == "up"


def test_rechecking_clears_a_stale_reading(
    client, agent_headers, service, check_returns
):
    """The most common reason to press it: the row says stale and you want to
    know whether the service or the worker is the problem."""
    from datetime import timedelta

    service.health_status = "up"
    service.health_checked_at = timezone.now() - timedelta(hours=2)
    service.save()
    assert service.effective_status() == "stale"

    check_returns(True)
    assert client.post("/api/v1/health/svc/check", **agent_headers).json()["status"] == "up"


def test_recheck_requires_authentication(client, service):
    """It makes the server issue an outbound request; unauthenticated reads must
    not be able to drive network traffic."""
    assert client.post("/api/v1/health/svc/check").status_code == 401


def test_recheck_is_refused_when_checks_are_disabled(client, agent_headers, service):
    service.health_enabled = False
    service.save()
    assert client.post("/api/v1/health/svc/check", **agent_headers).status_code == 409


def test_recheck_of_an_unknown_service_is_404(client, agent_headers):
    assert client.post("/api/v1/health/nope/check", **agent_headers).status_code == 404
