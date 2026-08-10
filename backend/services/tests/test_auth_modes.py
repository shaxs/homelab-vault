"""`HOMELABVAULT_AUTH_MODE` — v1 §6c.

Three modes, and the whole design rests on two properties:

* **It fails closed.** Unset, misspelled, or garbage resolves to `full`. This is
  the most consequential environment variable in the project, and a typo in the
  *variable name itself* must not silently open the app — which is exactly what
  a "default to whatever is set" implementation would do.
* **Probe and icon are admin-only in every mode.** The real cost of an open mode
  is SSRF, not catalog vandalism: those four endpoints make the server fetch a
  URL of the caller's choosing.

The mode is an env var rather than a row in the settings table on purpose: a
control governing whether sessions matter cannot live where a session can reach
it.
"""

import json

import pytest
from django.test import Client

from services.models import SavedView, Service
from services.probe import ProbeResult

from .conftest import ADMIN_TOKEN

pytestmark = pytest.mark.django_db


def body(payload):
    return {"data": json.dumps(payload), "content_type": "application/json"}


@pytest.fixture
def service(client, agent_headers):
    client.post(
        "/api/v1/services",
        **body({"launch_url": "https://x.lab.local", "tags": ["media"], "category": "tools"}),
        **agent_headers,
    )
    return Service.objects.get(slug="x")


@pytest.fixture
def stub_probe(monkeypatch):
    monkeypatch.setattr(
        "services.api.probe_url",
        lambda url: ProbeResult(ok=True, url=url, final_url=url, icon_url="/media/i.png"),
    )


# --- Failing closed --------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "  ",
        "opne",  # transposed
        "open_writes",  # underscore instead of hyphen
        "openwrites",
        "yes",
        "true",
        "1",
        "full; open",
        "OPEN-WRITES-PLEASE",
    ],
)
def test_an_unrecognised_mode_falls_back_to_full(settings, client, raw):
    """Three lines of implementation, and the one item here worth building
    regardless of any other scope decision."""
    settings.HOMELABVAULT_AUTH_MODE = raw
    response = client.post("/api/v1/services", **body({"launch_url": "https://n.lab.local"}))
    assert response.status_code == 401
    assert not Service.objects.exists()


@pytest.mark.parametrize("raw", ["OPEN", " open ", "Open-Writes"])
def test_case_and_whitespace_are_normalised(settings, client, raw):
    """An operator who types OPEN in a .env file means `open`. Normalising is
    not a weakening of fail-closed — the intent is unambiguous either way."""
    settings.HOMELABVAULT_AUTH_MODE = raw
    response = client.post("/api/v1/services", **body({"launch_url": "https://n.lab.local"}))
    assert response.status_code == 201


@pytest.mark.parametrize("raw", ["", "opne", "yes", "open_writes", "full; open"])
def test_the_coercion_itself_is_pinned_not_only_its_effect(settings, raw):
    """Pins `current_auth_mode()` directly, because the behavioural tests above
    cannot.

    Garbage is refused twice: the coercion here, and `_OPEN_MODE_SCOPES.get()`
    returning None for an unknown key. Remove the coercion and every
    behavioural test still passes — the second layer catches it — so nothing
    would notice the first one going away. It still matters, for two reasons:
    the settings screen would report `opne` as the *active* mode while the app
    behaved as `full`, and a future mode added to the dict would inherit no
    validation at all.
    """
    from services.auth import current_auth_mode

    settings.HOMELABVAULT_AUTH_MODE = raw
    assert current_auth_mode() == "full"


def test_the_default_is_full(settings, client):
    assert settings.HOMELABVAULT_AUTH_MODE == "full"
    assert client.post(
        "/api/v1/services", **body({"launch_url": "https://n.lab.local"})
    ).status_code == 401


# --- full ------------------------------------------------------------------


def test_full_mode_is_v0_behaviour(settings, client, service):
    settings.HOMELABVAULT_AUTH_MODE = "full"
    assert client.post(
        "/api/v1/services", **body({"launch_url": "https://n.lab.local"})
    ).status_code == 401
    assert client.patch(
        "/api/v1/services/x", **body({"category": "nope"})
    ).status_code == 401
    assert client.delete("/api/v1/services/x").status_code == 401


# --- open-writes -----------------------------------------------------------


def test_open_writes_allows_anonymous_create_and_edit(settings, client, service):
    settings.HOMELABVAULT_AUTH_MODE = "open-writes"
    assert client.post(
        "/api/v1/services", **body({"launch_url": "https://n.lab.local"})
    ).status_code == 201
    assert client.patch(
        "/api/v1/services/x", **body({"category": "edited"})
    ).status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/v1/services/x", "/api/v1/views/things", "/api/v1/tags/media",
             "/api/v1/categories/tools"]
)
def test_open_writes_still_demands_admin_for_destructive_operations(
    settings, client, service, path
):
    """"A stale session on a lost phone still cannot strip a tag from forty-six
    services on one misclick" — §6c's argument for shipping this mode at all."""
    SavedView.objects.create(name="things", query="state:up")
    settings.HOMELABVAULT_AUTH_MODE = "open-writes"

    assert client.delete(path).status_code == 401
    assert Service.objects.filter(slug="x").exists()
    assert SavedView.objects.filter(name="things").exists()
    assert Service.objects.get(slug="x").tags == ["media"]


# --- open ------------------------------------------------------------------


def test_open_allows_anonymous_destructive_operations(settings, client, service):
    settings.HOMELABVAULT_AUTH_MODE = "open"
    assert client.delete("/api/v1/services/x").status_code == 200
    assert not Service.objects.filter(slug="x").exists()


def test_open_does_not_re_prompt_for_a_token_it_does_not_require(
    settings, client, service
):
    """The delete re-prompt (§6b) must not contradict the mode: under `open` the
    operator has declared these endpoints need no token, so demanding one here
    would be theatre rather than hardening."""
    settings.HOMELABVAULT_AUTH_MODE = "open"
    settings.ADMIN_REAUTH_WINDOW_S = 0
    assert client.delete("/api/v1/services/x").status_code == 200


@pytest.mark.parametrize("mode", ["full", "open-writes", "open"])
def test_probe_is_admin_only_in_every_mode(settings, client, admin_headers, mode, stub_probe):
    settings.HOMELABVAULT_AUTH_MODE = mode
    payload = body({"url": "https://x.lab.local"})
    assert client.post("/api/v1/probe", **payload).status_code == 401
    assert client.post("/api/v1/probe", **payload, **admin_headers).status_code == 200


@pytest.mark.parametrize("mode", ["full", "open-writes", "open"])
def test_icon_endpoints_are_admin_only_in_every_mode(
    settings, client, service, mode, monkeypatch
):
    """All four §6c carve-outs, not just the probe. Each one makes the server
    fetch or store bytes on the caller's say-so."""
    settings.HOMELABVAULT_AUTH_MODE = mode
    monkeypatch.setattr("services.api.fetch_and_store_icon", lambda url: "/media/i.png")
    monkeypatch.setattr("services.api.store_icon_bytes", lambda data, name: "/media/i.png")

    assert client.post(
        "/api/v1/icon", **body({"url": "https://x.lab.local/f.png"})
    ).status_code == 401
    assert client.post("/api/v1/icon/upload", {"file": _png()}).status_code == 401
    assert client.post("/api/v1/services/x/refetch-icon").status_code == 401


@pytest.mark.parametrize("mode", ["full", "open-writes", "open"])
def test_settings_are_admin_only_in_every_mode(settings, client, admin_headers, mode):
    """Found by this chunk's `api-security` gate, 2026-08-05.

    §6c's carve-out list names only probe and icon, because it was written
    before the settings endpoints existed. Under `open`, `admin_auth` grants
    admin to an anonymous caller — which would have published the `.env` panel
    (every security-relevant variable name, which credentials are configured,
    the `PROBE_ALLOWED_NETWORKS` value) and let anyone on the network switch
    `notifications.discord.enabled` off.

    §6a's reason for admin-scoping the table is about the nature of the data,
    not about a mode, and disabling a monitoring control is a different class of
    harm from the catalog vandalism §6c weighs. The anonymous whitelist endpoint
    still serves everything a page needs to render, so nothing legitimate breaks.
    """
    settings.HOMELABVAULT_AUTH_MODE = mode

    assert client.get("/api/v1/settings").status_code == 401
    assert client.patch(
        "/api/v1/settings", **body({"notifications.discord.enabled": False})
    ).status_code == 401
    # ...and an admin is unaffected in every mode.
    assert client.get("/api/v1/settings", **admin_headers).status_code == 200


@pytest.mark.parametrize("mode", ["full", "open-writes", "open"])
def test_the_public_settings_endpoint_stays_open_in_every_mode(settings, client, mode):
    """The narrow whitelist endpoint is anonymous by design (§6a) — closing it
    would blank the landing page for a visitor rather than protect anything."""
    settings.HOMELABVAULT_AUTH_MODE = mode
    assert client.get("/api/v1/settings/public").status_code == 200


def _png():
    from io import BytesIO

    from django.core.files.uploadedfile import SimpleUploadedFile

    buffer = BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return SimpleUploadedFile("i.png", buffer.getvalue(), content_type="image/png")


# --- Provenance and the session branch -------------------------------------


def test_an_open_mode_write_is_recorded_as_open_not_as_a_token(settings, client):
    """`created_by` must not claim a credential was presented when none was —
    a catalog that says `token:agent` for an anonymous write is lying about how
    the record got there."""
    settings.HOMELABVAULT_AUTH_MODE = "open-writes"
    client.post("/api/v1/services", **body({"launch_url": "https://n.lab.local"}))
    assert Service.objects.get(slug="n").created_by == "open:agent"


def test_a_cookie_request_is_still_csrf_checked_in_an_open_mode(settings, service):
    """The mode must not become a CSRF bypass.

    A credential always decides before the mode does, so a request carrying a
    session cookie is judged as that session — CSRF included — rather than being
    quietly re-read as anonymous-and-therefore-allowed. Stricter than the mode
    requires, and the only ordering that cannot be turned into a hole.
    """
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.get("/")
    csrf_client.post(
        "/api/v1/session",
        **body({"token": ADMIN_TOKEN}),
        HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value,
    )
    settings.HOMELABVAULT_AUTH_MODE = "open"

    response = csrf_client.patch("/api/v1/services/x", **body({"category": "nope"}))
    assert response.status_code == 403
    assert Service.objects.get(slug="x").category != "nope"


def test_an_invalid_bearer_token_is_not_rescued_by_an_open_mode(settings, client, service):
    """Header present => bearer decides, full stop. Otherwise the open mode
    would quietly launder a rejected credential into an accepted one, and the
    junk-bearer bypass this file's sibling guards would reopen from the side."""
    settings.HOMELABVAULT_AUTH_MODE = "open"
    response = client.delete("/api/v1/services/x", HTTP_AUTHORIZATION="Bearer garbage")
    assert response.status_code == 401
    assert Service.objects.filter(slug="x").exists()
