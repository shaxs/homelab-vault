"""Regressions for findings from the step 1.4 / 1.5 security review.

Each test here corresponds to a defect that was found and fixed; they exist so
the fix cannot silently regress.
"""

import json

import pytest
from django.utils import timezone

from services import probe as probe_module
from services.models import Service

pytestmark = pytest.mark.django_db


# SCR-001 — model validation escaped as a 500 instead of a 422.
def test_invalid_choice_returns_422_naming_the_field(client, agent_headers):
    """Pydantic catches types and unknown keys; a right-typed wrong *choice*
    only fails at full_clean(), where Django raises its own ValidationError.
    Unhandled, Ninja re-raised it as a 500 — spec §4c requires a 422 naming the
    field."""
    response = client.post(
        "/api/v1/services",
        data=json.dumps(
            {"launch_url": "https://x.lab.local", "launch_target": "popup_window"}
        ),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422
    assert "launch_target" in response.content.decode()
    assert not Service.objects.exists()


def test_valid_choice_is_still_accepted(client, agent_headers):
    response = client.post(
        "/api/v1/services",
        data=json.dumps(
            {"launch_url": "https://x.lab.local", "launch_target": "same_tab"}
        ),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 201


# SCR-002 — stored XSS via an SVG favicon served from our own origin.
#
# SVG is now supported, but the defence is unchanged in substance: no SVG is
# ever written under MEDIA_ROOT, so none can be navigated to and executed in our
# origin. It is held as a data URI, which only renders inside an <img>.
def test_an_svg_icon_is_never_written_to_disk(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"

    stored = probe_module._store_icon(svg, "image/svg+xml", "http://x/i.svg")

    assert stored.startswith("data:image/svg+xml;base64,")
    # Nothing servable was created — that is the property that matters.
    assert not list(tmp_path.rglob("*.svg"))
    assert not list(tmp_path.rglob("icons/*"))


def test_svg_is_absent_from_the_file_backed_formats():
    """The file-writing path must never gain an .svg branch."""
    assert ".svg" not in probe_module._ICON_EXTENSIONS.values()
    assert ".svg" not in probe_module._ICON_SUFFIXES


def test_an_oversized_svg_is_refused(settings):
    settings.ICON_MAX_INLINE_BYTES = 128
    big = b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"<rect/>" * 200 + b"</svg>"
    assert probe_module._store_icon(big, "image/svg+xml", "http://x/i.svg") is None


def test_raster_favicon_is_stored(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    # A full PNG signature: storage now verifies the bytes, not the headers.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    stored = probe_module._store_icon(png, "image/png", "http://x/i.png")
    assert stored is not None and stored.endswith(".png")


# SCR-003 — TLS verification is an explicit, reviewable setting — and it must
# actually REACH each client. The old test asserted
# `hasattr(settings, "PROBE_VERIFY_TLS")`, which stayed green while neither
# client passed the value to httpx (audit T1). Now the value is captured at
# the construction each code path actually performs.


@pytest.mark.parametrize("verify", [True, False])
def test_the_tls_setting_reaches_the_probe_client(settings, monkeypatch, verify):
    import httpx

    settings.PROBE_VERIFY_TLS = verify
    captured = {}
    real_client = httpx.Client

    def recording(*args, **kwargs):
        captured["verify"] = kwargs.get("verify", "MISSING")
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<title>ok</title>")
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(probe_module.httpx, "Client", recording)
    monkeypatch.setattr(probe_module, "validate_url", lambda url: url)

    probe_module.probe_url("http://svc.lab.local/")

    assert captured["verify"] is verify


@pytest.mark.parametrize("verify", [True, False])
def test_the_tls_setting_reaches_the_owned_icon_client(
    settings, monkeypatch, verify
):
    """`fetch_and_store_icon` constructs its own client when not handed one —
    the second construction site T1 named."""
    import httpx

    settings.PROBE_VERIFY_TLS = verify
    captured = {}
    real_client = httpx.Client

    def recording(*args, **kwargs):
        captured["verify"] = kwargs.get("verify", "MISSING")
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(404)
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(probe_module.httpx, "Client", recording)
    monkeypatch.setattr(probe_module, "validate_url", lambda url: url)

    probe_module.fetch_and_store_icon("http://svc.lab.local/favicon.ico")

    assert captured["verify"] is verify


@pytest.mark.asyncio
@pytest.mark.parametrize("verify", [True, False])
async def test_the_tls_setting_reaches_the_health_checker(
    settings, monkeypatch, verify
):
    import httpx

    from services.health import http as health_http
    from services.models import Service

    settings.PROBE_VERIFY_TLS = verify
    captured = {}
    real_client = httpx.AsyncClient

    def recording(*args, **kwargs):
        captured["verify"] = kwargs.get("verify", "MISSING")
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200)
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(health_http.httpx, "AsyncClient", recording)
    monkeypatch.setattr(health_http, "validate_url", lambda url: url)
    service = Service(slug="s", name="S", launch_url="http://svc.lab.local/")

    assert await health_http.check_http(service) is True
    assert captured["verify"] is verify


# SCR-004 — the health checker must not follow redirects. The old test grepped
# `inspect.getsource` for the string "follow_redirects=False", which a comment
# satisfies and an unused call satisfies (audit T2). The behaviour: a 302 is
# an HTTP response — alive under §8 — and its Location, which can name a
# denied address, is never fetched.


@pytest.mark.asyncio
async def test_the_health_checker_does_not_follow_redirects(monkeypatch):
    import httpx

    from services.health import http as health_http
    from services.models import Service

    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    real_client = httpx.AsyncClient

    def recording(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(health_http.httpx, "AsyncClient", recording)
    monkeypatch.setattr(health_http, "validate_url", lambda url: url)
    service = Service(slug="s", name="S", launch_url="http://svc.lab.local/")

    assert await health_http.check_http(service) is True  # a 302 is alive (§8)
    assert requested == ["http://svc.lab.local/"]  # the hop was never fetched


@pytest.mark.asyncio
async def test_the_health_checker_validates_before_any_request(monkeypatch):
    """An agent-scoped token can write health_url; without validation the
    published health status becomes an internal port-scan oracle. Behavioural
    form of the old getsource check (audit T2): a rejected URL produces zero
    requests, not a request that happens to fail."""
    import httpx

    from services.health import http as health_http
    from services.models import Service
    from services.probe import ProbeRejected

    requested = []
    real_client = httpx.AsyncClient

    def recording(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: requested.append(str(request.url)) or httpx.Response(200)
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(health_http.httpx, "AsyncClient", recording)

    def rejecting(url):
        raise ProbeRejected("denied by policy")

    monkeypatch.setattr(health_http, "validate_url", rejecting)
    service = Service(slug="s", name="S", launch_url="http://10.0.0.9/")

    assert await health_http.check_http(service) is False
    assert requested == []


# --- V1.3's api-security gate (2026-08-05) --------------------------------


@pytest.mark.django_db
def test_a_service_cannot_pin_a_poll_interval_below_the_floor(client, agent_headers):
    """§6a's 15s floor covered the settings write path, not this one.

    Found by V1.3's `api-security` gate. Pinning `health_interval_s: 1` makes the
    worker probe that service every second — a sustained 60x amplification of
    §4c's deliberately-sanctioned "an agent write makes the server fetch a URL",
    and anonymous under `open-writes`. The settings floor never saw this path
    because it belongs to a different endpoint.
    """
    response = client.post(
        "/api/v1/services",
        data={"launch_url": "https://svc.lab.local", "health_interval_s": 1},
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 422
    assert "health_interval_s" in response.content.decode()
    assert not Service.objects.exists()


@pytest.mark.django_db
def test_the_floor_holds_on_patch_too(client, agent_headers):
    """The create path and the update path are two doors to the same field."""
    Service.objects.create(
        slug="svc", name="Svc", launch_url="https://svc.lab.local"
    )
    response = client.patch(
        "/api/v1/services/svc",
        data={"health_interval_s": 2},
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 422
    assert Service.objects.get(slug="svc").health_interval_s is None


@pytest.mark.django_db
def test_the_floor_value_itself_is_accepted(client, agent_headers):
    response = client.post(
        "/api/v1/services",
        data={"launch_url": "https://svc.lab.local", "health_interval_s": 15},
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code in (200, 201)


@pytest.mark.django_db
def test_unpinning_an_interval_is_still_allowed(client, agent_headers):
    """The floor must not block the null that means "follow the global" — which
    is the state the nullable column exists to express."""
    Service.objects.create(
        slug="svc",
        name="Svc",
        launch_url="https://svc.lab.local",
        health_interval_s=600,
    )
    response = client.patch(
        "/api/v1/services/svc",
        data={"health_interval_s": None},
        content_type="application/json",
        **agent_headers,
    )

    assert response.status_code == 200
    assert Service.objects.get(slug="svc").health_interval_s is None


@pytest.mark.django_db
def test_a_fail_threshold_of_zero_is_refused(client, agent_headers):
    """Zero would mark a service down before any check failed."""
    response = client.post(
        "/api/v1/services",
        data={"launch_url": "https://svc.lab.local", "health_fail_threshold": 0},
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 422


@pytest.mark.django_db
def test_healthz_detail_needs_admin_not_merely_a_token(client, agent_headers):
    """An agent token is a credential, not an admin one.

    `/healthz` shows thresholds only to admins, and the check that decides is
    `admin_only_auth` — which also ignores the auth mode, so an open deployment
    does not promote every anonymous caller to the detailed view.
    """
    from services.models import WorkerHeartbeat

    now = timezone.now()
    WorkerHeartbeat.objects.create(
        name="health",
        interval_s=60,
        started_at=now,
        last_beat_at=now,
        cycles=1,
    )

    worker = client.get("/api/v1/healthz", **agent_headers).json()["workers"][0]
    assert worker["interval_s"] is None


@pytest.mark.django_db
def test_healthz_detail_stays_admin_only_under_an_open_mode(client, settings):
    settings.HOMELABVAULT_AUTH_MODE = "open"
    from services.models import WorkerHeartbeat

    now = timezone.now()
    WorkerHeartbeat.objects.create(
        name="health",
        interval_s=60,
        started_at=now,
        last_beat_at=now,
        cycles=1,
    )

    body = client.get("/api/v1/healthz").json()
    assert body["workers"][0]["stale_after_s"] is None
    assert body["notifications_configured"] is None
