"""Token scopes, session exchange, and the CSRF split — spec §4c.

The CSRF split is the load-bearing control: CsrfViewMiddleware is absent
globally so bearer writes work, which means the *only* CSRF protection for
cookie-authenticated writes is the check inside the session branch. These tests
exercise the bypass paths, not just the happy path.
"""

import json

import pytest

from services.models import Service

from .conftest import ADMIN_TOKEN, AGENT_TOKEN

pytestmark = pytest.mark.django_db


@pytest.fixture
def service(client, agent_headers):
    client.post(
        "/api/v1/services",
        data=json.dumps({"launch_url": "https://x.lab.local"}),
        content_type="application/json",
        **agent_headers,
    )
    return Service.objects.get(slug="x")


def body(payload):
    return {"data": json.dumps(payload), "content_type": "application/json"}


# --- Scopes ---------------------------------------------------------------


def test_write_without_credentials_is_rejected(client):
    response = client.post("/api/v1/services", **body({"launch_url": "https://x.lab.local"}))
    assert response.status_code == 401


def test_agent_token_can_create_and_patch(client, agent_headers, service):
    response = client.patch(
        "/api/v1/services/x", **body({"category": "tools"}), **agent_headers
    )
    assert response.status_code == 200


def test_agent_token_cannot_delete(client, agent_headers, service):
    """"An agent that can register services must not be able to unregister the
    catalog" (§4c)."""
    response = client.delete("/api/v1/services/x", **agent_headers)
    assert response.status_code == 401
    assert Service.objects.filter(slug="x").exists()


def test_admin_token_can_delete(client, admin_headers, service):
    assert client.delete("/api/v1/services/x", **admin_headers).status_code == 200


def test_probe_is_admin_scope_only(client, agent_headers, admin_headers, monkeypatch):
    monkeypatch.setattr(
        "services.api.probe_url",
        lambda url: __import__(
            "services.probe", fromlist=["ProbeResult"]
        ).ProbeResult(ok=False, url=url, final_url=url, error="stub"),
    )
    payload = body({"url": "https://x.lab.local"})
    assert client.post("/api/v1/probe", **payload, **agent_headers).status_code == 401
    assert client.post("/api/v1/probe", **payload, **admin_headers).status_code == 200


def test_unset_token_never_authenticates(client, settings, service):
    """A deployment that forgot to set HOMELABVAULT_AGENT_TOKEN must not accept
    the empty string."""
    settings.HOMELABVAULT_AGENT_TOKEN = ""
    response = client.patch(
        "/api/v1/services/x",
        **body({"category": "x"}),
        HTTP_AUTHORIZATION="Bearer ",
    )
    assert response.status_code == 401


def test_invalid_token_is_rejected(client, service):
    response = client.patch(
        "/api/v1/services/x",
        **body({"category": "x"}),
        HTTP_AUTHORIZATION="Bearer not-the-token",
    )
    assert response.status_code == 401


def test_a_non_ascii_token_is_refused_not_a_500(client, service):
    """hmac.compare_digest raises TypeError on non-ASCII str input. Unhandled,
    an accented character in the configured token — or in what a caller sends —
    turns every authentication attempt into a 500 with a stack trace under
    DEBUG, when the honest answer is "that is not the token"."""
    response = client.patch(
        "/api/v1/services/x",
        **body({"category": "x"}),
        HTTP_AUTHORIZATION="Bearer tökén-wíth-áccents",
    )
    assert response.status_code == 401


def test_a_non_ascii_configured_token_does_not_break_valid_auth(
    client, settings, service, agent_headers
):
    settings.HOMELABVAULT_ADMIN_TOKEN = "ådmin-tøken"
    response = client.patch("/api/v1/services/x", **body({"category": "y"}), **agent_headers)
    assert response.status_code == 200


# --- Bearer requests are CSRF-exempt (§4c) --------------------------------


def test_bearer_write_needs_no_csrf_token_or_cookie(client, agent_headers):
    """Otherwise every agent write fails."""
    response = client.post(
        "/api/v1/services", **body({"launch_url": "https://agent.lab.local"}), **agent_headers
    )
    assert response.status_code == 201


# --- Session exchange -----------------------------------------------------


def test_session_exchange_requires_the_admin_token(client):
    assert client.post("/api/v1/session", **body({"token": AGENT_TOKEN})).status_code == 401
    assert client.post("/api/v1/session", **body({"token": "wrong"})).status_code == 401
    assert client.post("/api/v1/session", **body({"token": ADMIN_TOKEN})).status_code == 200


def test_session_cookie_is_httponly_and_samesite_strict(client, settings):
    response = client.post("/api/v1/session", **body({"token": ADMIN_TOKEN}))
    cookie = response.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Strict"


def test_session_response_never_echoes_the_token(client):
    response = client.post("/api/v1/session", **body({"token": ADMIN_TOKEN}))
    assert ADMIN_TOKEN not in response.content.decode()


def test_repeated_failures_are_throttled(client, settings):
    settings.SESSION_LOGIN_MAX_ATTEMPTS = 3
    from django.core.cache import cache

    cache.clear()
    for _ in range(3):
        assert client.post("/api/v1/session", **body({"token": "wrong"})).status_code == 401
    # Even the correct token is refused once the window is exhausted.
    assert client.post("/api/v1/session", **body({"token": ADMIN_TOKEN})).status_code == 429


# --- CSRF enforcement on the cookie branch --------------------------------


def test_get_issues_the_csrf_cookie(client):
    """The SPA can only send X-CSRFToken if it was issued the cookie first."""
    response = client.get("/")
    assert "csrftoken" in response.cookies


def _session_client(enforce_csrf=True):
    from django.test import Client

    return Client(enforce_csrf_checks=enforce_csrf)


def test_session_write_without_csrf_token_is_rejected(service):
    """The hole this guards: with CsrfViewMiddleware absent globally, nothing
    else protects cookie-authenticated writes."""
    csrf_client = _session_client()
    assert csrf_client.post("/api/v1/session", **body({"token": ADMIN_TOKEN})).status_code == 200

    response = csrf_client.patch("/api/v1/services/x", **body({"category": "nope"}))
    assert response.status_code == 403
    assert Service.objects.get(slug="x").category != "nope"


def test_session_write_with_a_forged_csrf_token_is_rejected(service):
    csrf_client = _session_client()
    csrf_client.post("/api/v1/session", **body({"token": ADMIN_TOKEN}))
    response = csrf_client.patch(
        "/api/v1/services/x",
        **body({"category": "nope"}),
        HTTP_X_CSRFTOKEN="clearly-not-a-valid-token",
    )
    assert response.status_code == 403


def test_session_write_with_a_valid_csrf_token_succeeds(service):
    csrf_client = _session_client()
    csrf_client.get("/")
    csrf_client.post(
        "/api/v1/session",
        **body({"token": ADMIN_TOKEN}),
        HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value,
    )
    response = csrf_client.patch(
        "/api/v1/services/x",
        **body({"category": "tools"}),
        HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value,
    )
    assert response.status_code == 200
    assert Service.objects.get(slug="x").category == "tools"


def test_junk_bearer_header_does_not_downgrade_into_the_session_branch(service):
    """The subtle bypass: if an invalid Authorization header fell through to
    session auth, an attacker could attach one to a cookie-authenticated
    cross-site request and skip the CSRF check entirely."""
    csrf_client = _session_client()
    csrf_client.get("/")
    csrf_client.post(
        "/api/v1/session",
        **body({"token": ADMIN_TOKEN}),
        HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value,
    )
    response = csrf_client.patch(
        "/api/v1/services/x",
        **body({"category": "nope"}),
        HTTP_AUTHORIZATION="Bearer garbage",
    )
    assert response.status_code == 401
    assert Service.objects.get(slug="x").category != "nope"


def test_non_bearer_authorization_header_also_does_not_downgrade(service):
    csrf_client = _session_client()
    csrf_client.get("/")
    csrf_client.post(
        "/api/v1/session",
        **body({"token": ADMIN_TOKEN}),
        HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value,
    )
    response = csrf_client.patch(
        "/api/v1/services/x",
        **body({"category": "nope"}),
        HTTP_AUTHORIZATION="Basic YWRtaW46YWRtaW4=",
    )
    assert response.status_code == 401
