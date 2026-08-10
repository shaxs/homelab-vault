"""Session lifetime, token binding, and the destructive-op re-prompt — v1 §6b.

Three controls, each with a failure mode that is invisible from the outside:

* **Fixed-window expiry.** An infinite session looks exactly like a working one.
  Nothing in the product surfaces the difference, so a later "session UX fix"
  that enables `SESSION_SAVE_EVERY_REQUEST` would land unnoticed — and with the
  catalog polling every 15 seconds, any open tab would then renew forever.
* **Token binding.** Rotating `HOMELABVAULT_ADMIN_TOKEN` previously locked out
  Hermes and left every browser session live, which is backwards for a
  credential rotation.
* **Re-prompt on delete.** The window is the whole control; a stale tab that
  still carries it protects nothing.

The expiry tests assert the *derived behaviour* rather than the flag, because a
flag assertion still passes while middleware ordering or a later override
changes the outcome.
"""

import json
import time

import pytest
from django.contrib.sessions.models import Session
from django.test import Client

from services.auth import SESSION_REAUTH_AT_KEY, SESSION_TOKEN_FP_KEY
from services.models import SavedView, Service

from .conftest import ADMIN_TOKEN

pytestmark = pytest.mark.django_db


def body(payload):
    return {"data": json.dumps(payload), "content_type": "application/json"}


def signed_in_client():
    """A CSRF-enforcing client holding a real admin session.

    `enforce_csrf_checks=True` matters: the session branch is the only one with
    CSRF exposure, so testing it with checks disabled would exercise a path no
    browser takes.
    """
    client = Client(enforce_csrf_checks=True)
    client.get("/")
    response = client.post(
        "/api/v1/session",
        **body({"token": ADMIN_TOKEN}),
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 200
    return client


def csrf(client):
    return {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}


def session_row(client):
    return Session.objects.get(session_key=client.cookies["hlv_session"].value)


def age_the_reauth_stamp(client, seconds):
    """Move this session's last-token-entry backwards in time.

    Cheaper and more honest than shrinking the window to zero: it simulates the
    thing the control exists for — a tab that has been open for a while.
    """
    session = client.session
    session[SESSION_REAUTH_AT_KEY] = time.time() - seconds
    session.save()


@pytest.fixture
def service(client, agent_headers):
    client.post(
        "/api/v1/services",
        **body({"launch_url": "https://x.lab.local", "tags": ["media"], "category": "tools"}),
        **agent_headers,
    )
    return Service.objects.get(slug="x")


# --- Fixed-window expiry (§6b item 3) --------------------------------------


def test_session_expiry_does_not_move_when_a_request_is_served(service):
    """The highest-value assertion in this chunk.

    With `SESSION_SAVE_EVERY_REQUEST=True` the session is re-saved on every
    response and `expire_date` is recomputed as now + `SESSION_COOKIE_AGE`, so
    this comparison fails at microsecond resolution. That is the point: the
    difference between a two-week session and an eternal one is not visible
    anywhere else in the product.

    The catalog polls every 15 seconds, so under sliding expiry a single tab
    left open renews the session forever — an infinite session wearing a
    two-week label.
    """
    client = signed_in_client()
    original = session_row(client).expire_date

    assert client.get("/api/v1/services").status_code == 200
    # A write, so the session branch itself is exercised and not just a
    # public read that never touches authentication.
    assert client.patch(
        "/api/v1/services/x", **body({"category": "served"}), **csrf(client)
    ).status_code == 200

    assert session_row(client).expire_date == original


def test_session_expiry_is_a_fixed_window_from_login(settings):
    """Two weeks measured from the token exchange, not from the last request."""
    from django.utils import timezone

    client = signed_in_client()
    remaining = (session_row(client).expire_date - timezone.now()).total_seconds()
    assert abs(remaining - settings.SESSION_COOKIE_AGE) < 30


def test_the_configured_lifetime_is_the_chosen_fourteen_days(settings):
    """`SESSION_COOKIE_AGE` is a decision now, not an inherited default (§6b)."""
    assert settings.SESSION_COOKIE_AGE == 1209600


# --- Token rotation invalidates sessions (§6b item 2) ----------------------


def test_rotating_the_admin_token_kills_a_live_session(settings, service):
    client = signed_in_client()
    assert client.patch(
        "/api/v1/services/x", **body({"category": "before"}), **csrf(client)
    ).status_code == 200

    settings.HOMELABVAULT_ADMIN_TOKEN = "rotated-admin-token"

    response = client.patch(
        "/api/v1/services/x", **body({"category": "after"}), **csrf(client)
    )
    assert response.status_code == 401
    assert Service.objects.get(slug="x").category == "before"


def test_rotation_invalidates_but_does_not_erase(settings):
    """The distinction that matters when reading the sessions table.

    Django cannot reach into the store to flush a session it is not currently
    serving, so a browser that never comes back leaves its row until
    `SESSION_COOKIE_AGE` expires it. The row being present is NOT evidence the
    session still works — assert both halves, or the next reader assumes the
    wrong one. Real erasure is `clearsessions` on a schedule.
    """
    absent_browser = signed_in_client()
    key = absent_browser.cookies["hlv_session"].value

    settings.HOMELABVAULT_ADMIN_TOKEN = "rotated-admin-token"

    # Nothing served that session, so nothing could have cleaned it up.
    assert Session.objects.filter(session_key=key).exists()

    # It is nonetheless dead: the first request it makes is refused...
    assert absent_browser.get("/api/v1/services").status_code == 200  # reads are public
    assert absent_browser.delete("/api/v1/views/none", **csrf(absent_browser)).status_code == 401
    # ...and flushed on that encounter, which is the only moment we can.
    assert not Session.objects.filter(session_key=key).exists()


def test_a_session_with_no_fingerprint_is_refused(service):
    """Fails closed for sessions minted before this mechanism existed."""
    client = signed_in_client()
    session = client.session
    del session[SESSION_TOKEN_FP_KEY]
    session.save()

    response = client.patch(
        "/api/v1/services/x", **body({"category": "nope"}), **csrf(client)
    )
    assert response.status_code == 401
    assert Service.objects.get(slug="x").category != "nope"


def test_an_unset_admin_token_matches_no_session(settings, service):
    """An empty configured token must not authenticate an empty fingerprint."""
    client = signed_in_client()
    settings.HOMELABVAULT_ADMIN_TOKEN = ""
    assert client.patch(
        "/api/v1/services/x", **body({"category": "nope"}), **csrf(client)
    ).status_code == 401


def test_the_session_stores_a_fingerprint_and_never_the_token():
    """A leaked session row must not leak HOMELABVAULT_ADMIN_TOKEN."""
    client = signed_in_client()
    stored = client.session[SESSION_TOKEN_FP_KEY]
    assert ADMIN_TOKEN not in stored
    assert stored != ADMIN_TOKEN
    # And nothing in the serialised row carries it either.
    assert ADMIN_TOKEN not in session_row(client).session_data


# --- Re-prompt on destructive operations (§6b, promoted from prose) --------


def test_delete_inside_the_window_needs_no_re_prompt(service):
    client = signed_in_client()
    assert client.delete("/api/v1/services/x", **csrf(client)).status_code == 200


def test_delete_outside_the_window_demands_the_token_again(service):
    client = signed_in_client()
    age_the_reauth_stamp(client, 600)

    response = client.delete("/api/v1/services/x", **csrf(client))
    assert response.status_code == 403
    assert response.json()["code"] == "reauth_required"
    assert Service.objects.filter(slug="x").exists()


def test_re_entering_the_token_clears_the_gate_again(service):
    client = signed_in_client()
    age_the_reauth_stamp(client, 600)
    assert client.delete("/api/v1/services/x", **csrf(client)).status_code == 403

    client.post("/api/v1/session", **body({"token": ADMIN_TOKEN}), **csrf(client))
    assert client.delete("/api/v1/services/x", **csrf(client)).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/services/x",
        "/api/v1/views/things",
        "/api/v1/tags/media",
        "/api/v1/categories/tools",
    ],
)
def test_every_destructive_endpoint_is_gated(service, path):
    SavedView.objects.create(name="things", query="state:up")
    client = signed_in_client()
    age_the_reauth_stamp(client, 600)

    response = client.delete(path, **csrf(client))
    assert response.status_code == 403, path
    assert response.json()["code"] == "reauth_required"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("patch", "/api/v1/services/x", {"category": "edited"}),
        ("patch", "/api/v1/tags/media", {"name": "video"}),
        ("patch", "/api/v1/categories/tools", {"name": "utilities"}),
    ],
)
def test_bulk_edits_are_deliberately_not_gated(service, method, path, payload):
    """Batch tag add and batch category set are recoverable (§6b).

    Gating them makes the prompt routine, and a prompt people click through
    reflexively protects nothing. This test exists so that "add it everywhere"
    reads as a decision being reversed rather than a gap being filled.
    """
    client = signed_in_client()
    age_the_reauth_stamp(client, 600)

    response = getattr(client, method)(path, **body(payload), **csrf(client))
    assert response.status_code == 200


def test_a_bearer_admin_is_never_re_prompted(service, admin_headers, settings):
    """The credential is on the request itself, so it is already being
    re-presented. Gating it would break Hermes and every script."""
    settings.ADMIN_REAUTH_WINDOW_S = 0
    response = Client().delete("/api/v1/services/x", **admin_headers)
    assert response.status_code == 200


def test_a_backwards_clock_does_not_grant_an_unbounded_window(service):
    """A stamp in the future would otherwise pass `now - stamped <= window`
    forever once the clock corrected itself."""
    client = signed_in_client()
    session = client.session
    session[SESSION_REAUTH_AT_KEY] = time.time() + 86400
    session.save()

    assert client.delete("/api/v1/services/x", **csrf(client)).status_code == 403
