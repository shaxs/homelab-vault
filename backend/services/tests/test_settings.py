"""The settings table and its three endpoints — v1 §4, §6a, §11.2.

§11.2 calls settings "new attack surface in an app whose reads are
unauthenticated", and asks specifically that the public response be verified as
a *whitelist* rather than a blacklist. The distinguishing test is the one that
adds a key after the whitelist was written: a blacklist passes every test you
write about the keys you already know.

The other two properties this file pins are structural rather than behavioural,
which is the point of pinning them: **nothing in the registry can hold free
text**, so a credential has nowhere to live; and **there is no key path to a
deploy-time control**, so a session can never reach `PROBE_ALLOWED_NETWORKS`.
"""

import json

import pytest

from services import settings_store
from services.models import Service, Setting
from services.settings_store import SettingSpec

from .conftest import ADMIN_TOKEN

pytestmark = pytest.mark.django_db


def body(payload):
    return {"data": json.dumps(payload), "content_type": "application/json"}


# --- Scoping (§6a) ---------------------------------------------------------


def test_reading_settings_requires_admin(client, agent_headers, admin_headers):
    assert client.get("/api/v1/settings").status_code == 401
    assert client.get("/api/v1/settings", **agent_headers).status_code == 401
    assert client.get("/api/v1/settings", **admin_headers).status_code == 200


def test_writing_settings_requires_admin(client, agent_headers, admin_headers):
    payload = body({"health.fail_threshold": 4})
    assert client.patch("/api/v1/settings", **payload).status_code == 401
    assert client.patch("/api/v1/settings", **payload, **agent_headers).status_code == 401
    assert client.patch("/api/v1/settings", **payload, **admin_headers).status_code == 200


def test_the_public_endpoint_is_anonymous(client):
    assert client.get("/api/v1/settings/public").status_code == 200


def test_a_session_settings_write_is_csrf_checked(client):
    """The new write endpoint inherits the CSRF split, and inheritance is worth
    asserting rather than assuming.

    `CsrfViewMiddleware` is absent globally so bearer writes work, which means
    the only CSRF protection any cookie-authenticated write has is the check
    inside the session branch of `auth.py`. A new write endpoint that somehow
    routed around that branch would have none at all.
    """
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.get("/")
    token = csrf_client.cookies["csrftoken"].value
    csrf_client.post(
        "/api/v1/session", **body({"token": ADMIN_TOKEN}), HTTP_X_CSRFTOKEN=token
    )

    without = csrf_client.patch("/api/v1/settings", **body({"health.fail_threshold": 9}))
    assert without.status_code == 403
    assert settings_store.get_setting("health.fail_threshold") == 3

    with_token = csrf_client.patch(
        "/api/v1/settings", **body({"health.fail_threshold": 9}), HTTP_X_CSRFTOKEN=token
    )
    assert with_token.status_code == 200
    assert settings_store.get_setting("health.fail_threshold") == 9


# --- The whitelist is a whitelist (§11.2) ----------------------------------


def test_the_public_response_carries_only_whitelisted_keys(client, admin_headers):
    public = client.get("/api/v1/settings/public").json()["values"]
    assert set(public) == set(settings_store.PUBLIC_SETTING_KEYS)

    admin = {row["key"] for row in client.get("/api/v1/settings", **admin_headers).json()["settings"]}
    # The admin view is strictly larger, or the whitelist would be pointless.
    assert set(public) < admin


def test_a_key_added_after_the_whitelist_was_written_stays_private(client, monkeypatch):
    """The assertion that actually distinguishes a whitelist from a blacklist.

    A blacklist passes every test written about keys that existed when it was
    written, and leaks the first one added afterwards. So add one afterwards.
    """
    added = SettingSpec(
        key="notifications.discord.webhook_env_shadow",
        kind=settings_store.KIND_ENV_REF,
        default="SOMETHING_SENSITIVE_SOUNDING",
        label="Added later",
        helper="",
        section="Notifications",
    )
    monkeypatch.setitem(settings_store.SPECS, added.key, added)
    monkeypatch.setattr(settings_store, "REGISTRY", settings_store.REGISTRY + (added,))

    values = client.get("/api/v1/settings/public").json()["values"]
    assert added.key not in values
    assert set(values) == set(settings_store.PUBLIC_SETTING_KEYS)


def test_the_public_response_reports_the_auth_mode(client, settings):
    """§12 wants the mode visible outside the settings screen, and the catalog
    header is anonymous."""
    settings.HOMELABVAULT_AUTH_MODE = "open-writes"
    assert client.get("/api/v1/settings/public").json()["auth_mode"] == "open-writes"


def test_the_public_response_reports_a_garbage_mode_as_full(client, settings):
    """The screen must not report `opne` as active while the app behaves as
    `full` — silent wrongness of exactly the kind §5a exists to prevent."""
    settings.HOMELABVAULT_AUTH_MODE = "opne"
    assert client.get("/api/v1/settings/public").json()["auth_mode"] == "full"


# --- Defaults and persistence ---------------------------------------------


def test_unset_keys_report_their_registry_default(client, admin_headers):
    """Rows are sparse: a fresh install needs no seeding step."""
    assert not Setting.objects.exists()
    rows = {r["key"]: r for r in client.get("/api/v1/settings", **admin_headers).json()["settings"]}
    assert rows["health.default_interval_s"]["value"] == 60
    assert rows["notifications.discord.enabled"]["value"] is False


def test_a_written_value_persists_and_reads_back(client, admin_headers):
    response = client.patch(
        "/api/v1/settings",
        **body({"health.default_interval_s": 45, "notifications.discord.enabled": True}),
        **admin_headers,
    )
    assert response.status_code == 200

    rows = {r["key"]: r["value"] for r in response.json()["settings"]}
    assert rows["health.default_interval_s"] == 45
    assert rows["notifications.discord.enabled"] is True
    assert settings_store.get_setting("health.default_interval_s") == 45


def test_a_batch_write_is_all_or_nothing(client, admin_headers):
    """One bad key must not leave half a form saved."""
    client.patch("/api/v1/settings", **body({"health.fail_threshold": 5}), **admin_headers)
    response = client.patch(
        "/api/v1/settings",
        **body({"health.fail_threshold": 9, "health.default_interval_s": 2}),
        **admin_headers,
    )
    assert response.status_code == 422
    assert settings_store.get_setting("health.fail_threshold") == 5


# --- Interval floors, server-side (§6a, added 2026-08-03) -----------------


@pytest.mark.parametrize(
    ("key", "value", "floor"),
    [
        ("health.default_interval_s", 14, 15),
        ("health.default_interval_s", 0, 15),
        ("health.default_interval_s", -60, 15),
        ("proxmox.poll_interval_s", 29, 30),
        ("proxmox.poll_interval_s", 1, 30),
    ],
)
def test_interval_floors_are_enforced_on_the_write_path(
    client, admin_headers, key, value, floor
):
    """The UI stepper is a convenience; the API is the boundary."""
    response = client.patch("/api/v1/settings", **body({key: value}), **admin_headers)
    assert response.status_code == 422
    assert str(floor) in response.json()["detail"]
    assert not Setting.objects.filter(key=key).exists()


@pytest.mark.parametrize(
    ("key", "value"), [("health.default_interval_s", 15), ("proxmox.poll_interval_s", 30)]
)
def test_the_floor_itself_is_accepted(client, admin_headers, key, value):
    assert client.patch(
        "/api/v1/settings", **body({key: value}), **admin_headers
    ).status_code == 200


# --- No secrets in the table (§4) -----------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://discord.com/api/webhooks/123456789/aBcDeF-ghIjKlMnOpQrStUvWxYz",
        "hunter2",
        "sk-proj-0a1b2c3d4e5f",
        "PVEAPIToken=root@pam!vault=6f1a-4c2b",
        "Bearer abc123",
        "  HOMELABVAULT_DISCORD_WEBHOOK_URL  ",
        "homelabvault_discord_webhook_url",
    ],
)
def test_a_credential_shaped_value_is_refused(client, admin_headers, value):
    """§4 states "no secrets" as design intent, with nothing asserting it.

    The guarantee is structural rather than heuristic: `env_ref` is the only
    string-ish kind in the registry and it accepts an environment variable NAME,
    so there is no key anywhere that a URL or a token can be written into.
    """
    response = client.patch(
        "/api/v1/settings",
        **body({"notifications.discord.webhook_env": value}),
        **admin_headers,
    )
    assert response.status_code == 422
    assert not Setting.objects.filter(key="notifications.discord.webhook_env").exists()


@pytest.mark.parametrize(
    "raw_body",
    [
        '"{secret}"',
        '["{secret}"]',
        '{{"health.fail_threshold": "{secret}"}}',
        '{{"notifications.discord.webhook_env": "{secret}"}}',
        '{{"ui.landing_page": "{secret}"}}',
    ],
)
def test_a_rejected_write_never_echoes_the_value_it_rejected(
    client, admin_headers, raw_body
):
    """The realistic accident this guards: an operator pastes a credential into
    a settings field, gets a 422, and pastes the error into a chat or an issue.

    Pydantic's `ValidationError.errors()` carries the offending `input`, so an
    echo is one custom error handler away — and a refusal message is exactly the
    artifact a person copies somewhere else.

    **Deliberately excluded: a credential in the KEY position.** §4c requires a
    422 that names the offending field, and no rule separates a mistyped key
    from a credential typed as one — `PROBE_ALLOWED_NETWORKS` and
    `sk-live-9f3a…` are the same length and the same character class. The echo
    is bounded at 48 characters rather than sanitised, and the case is left
    named rather than quietly dropped. Values are the realistic paste target and
    they are covered above.
    """
    secret = "sk-live-9f3a1c7e2b4d6a8f0e1c3b5d7a9f"  # gitleaks:allow — fake by design; the test asserts it is never echoed
    response = client.patch(
        "/api/v1/settings",
        data=raw_body.format(secret=secret),
        content_type="application/json",
        **admin_headers,
    )
    assert response.status_code == 422
    assert secret not in response.content.decode()


def test_an_env_var_name_is_accepted(client, admin_headers):
    assert client.patch(
        "/api/v1/settings",
        **body({"notifications.discord.webhook_env": "MY_WEBHOOK_URL"}),
        **admin_headers,
    ).status_code == 200


def test_no_registry_key_accepts_free_text():
    """The property the test above depends on, asserted directly so that adding
    a free-text key is a deliberate act rather than an accident."""
    allowed = {
        settings_store.KIND_BOOL,
        settings_store.KIND_INT,
        settings_store.KIND_ENUM,
        settings_store.KIND_ENV_REF,
        # Added deliberately (BACKLOG.1, 2026-08-07): a Discord snowflake ID —
        # 17-20 digits or empty — validated by shape exactly like env_ref.
        # Still not free text: neither a name to parse nor a credential fits.
        settings_store.KIND_DISCORD_MENTION,
    }
    assert {spec.kind for spec in settings_store.REGISTRY} <= allowed


# --- Env-only controls are unreachable (§6a) ------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "PROBE_ALLOWED_NETWORKS",
        "probe.allowed_networks",
        "security.probe_allowed_networks",
        "PROBE_VERIFY_TLS",
        "SECRET_KEY",
        "DATABASE_URL",
        "HOMELABVAULT_ADMIN_TOKEN",
        "HOMELABVAULT_AGENT_TOKEN",
        "HOMELABVAULT_AUTH_MODE",
        "auth.mode",
        "SESSION_COOKIE_AGE",
        "session.cookie_age",
        "__class__",
        "ui.landing_page.extra",
        # V1.4's outbound path. `PROXMOX_HOST` is the destination of a
        # server-side request carrying a credential, so a key path to it would
        # be SSRF (A10) with the token attached — strictly worse than the probe,
        # which sends none. `PROXMOX_VERIFY_TLS` is the control that decides
        # whether that credential crosses a connection we can authenticate.
        "PROXMOX_HOST",
        "proxmox.host",
        "proxmox.api_token",
        "PROXMOX_API_TOKEN",
        "PROXMOX_VERIFY_TLS",
        "proxmox.verify_tls",
    ],
)
def test_deploy_time_controls_have_no_key_path(client, admin_headers, settings, key):
    """A session must never reach a security control, by any key path (§6a).

    `PROBE_ALLOWED_NETWORKS` above all: it is the boundary the SSRF policy is
    built on, and an open auth mode already widens what the probe is reachable
    from.

    **`PROXMOX_HOST` is the V1.4 addition and the reason A10 stays closed here.**
    The provider fetches whatever that variable names, with the API token in the
    header — so if it were settable through the API, an operator session (or an
    anonymous caller under `open`) could redirect an authenticated outbound
    request anywhere and read the response back off the settings screen. It is
    deploy-time configuration precisely so no request can move it.
    """
    before = list(settings.PROBE_ALLOWED_NETWORKS)
    response = client.patch(
        "/api/v1/settings", **body({key: "10.0.0.0/8"}), **admin_headers
    )
    assert response.status_code == 422
    assert key in response.json()["detail"]
    assert list(settings.PROBE_ALLOWED_NETWORKS) == before
    assert not Setting.objects.exists()


def test_the_auth_mode_cannot_be_changed_through_the_settings_api(
    client, admin_headers, settings
):
    """§6c: a control governing whether sessions matter cannot live where a
    session can reach it."""
    settings.HOMELABVAULT_AUTH_MODE = "full"
    for key in ("HOMELABVAULT_AUTH_MODE", "auth.mode", "auth_mode"):
        assert client.patch(
            "/api/v1/settings", **body({key: "open"}), **admin_headers
        ).status_code == 422
    assert client.get("/api/v1/settings/public").json()["auth_mode"] == "full"


# --- Type strictness -------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("notifications.discord.enabled", "true"),
        ("notifications.discord.enabled", 1),
        ("health.fail_threshold", True),
        ("health.fail_threshold", "3"),
        ("health.fail_threshold", 3.5),
        ("ui.landing_page", "commandcenter"),
        ("ui.landing_page", True),
    ],
)
def test_wrong_types_are_refused_naming_the_key(client, admin_headers, key, value):
    response = client.patch("/api/v1/settings", **body({key: value}), **admin_headers)
    assert response.status_code == 422
    assert key in response.json()["detail"]


def test_an_empty_write_is_refused(client, admin_headers):
    assert client.patch("/api/v1/settings", **body({}), **admin_headers).status_code == 422


# --- The .env panel --------------------------------------------------------


def test_the_env_panel_never_discloses_a_secret(client, admin_headers, settings):
    """Tokens report presence only — and the value is never read on that path,
    so a redaction bug cannot leak one."""
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/secret"
    rows = client.get("/api/v1/settings", **admin_headers).json()["environment"]
    serialised = json.dumps(rows)

    assert ADMIN_TOKEN not in serialised
    assert "secret" not in serialised
    presence_only = {
        r["env_var"] for r in rows if not r["shows_value"]
    }
    assert {"HOMELABVAULT_ADMIN_TOKEN", "HOMELABVAULT_AGENT_TOKEN",
            "HOMELABVAULT_DISCORD_WEBHOOK_URL", "PROXMOX_API_TOKEN"} <= presence_only
    for row in rows:
        if not row["shows_value"]:
            assert row["status"] in ("Configured", "Not configured")


def test_a_credential_named_variable_cannot_be_declared_displayable():
    """Turns "we declared them all correctly" into "they cannot be declared
    wrongly", which is the difference between a fact about today's list and a
    property of the code.

    Found by the `secure-code-review` gate: the disclosure mode was a per-entry
    choice, so V1.4 adding `PROXMOX_API_TOKEN` with the wrong one would have
    printed a cluster credential onto the settings screen.
    """
    from dataclasses import replace

    from django.core.exceptions import ImproperlyConfigured

    poisoned = tuple(
        replace(c, disclosure="value") if "TOKEN" in c.env_var else c
        for c in settings_store.ENV_CONTROLS
    )
    original = settings_store.ENV_CONTROLS
    settings_store.ENV_CONTROLS = poisoned
    try:
        with pytest.raises(ImproperlyConfigured):
            settings_store.env_panel()
    finally:
        settings_store.ENV_CONTROLS = original


def _env_rows(client, admin_headers):
    response = client.get("/api/v1/settings", **admin_headers)
    return {row["env_var"]: row for row in response.json()["environment"]}


def test_the_env_panel_shows_the_controls_it_does_not_own(client, admin_headers):
    rows = _env_rows(client, admin_headers)
    assert rows["SESSION_COOKIE_AGE"]["status"] == "14 days"
    assert rows["HOMELABVAULT_AUTH_MODE"]["status"] == "full"
    assert "10.0.0.0/8" in rows["PROBE_ALLOWED_NETWORKS"]["status"]


@pytest.mark.parametrize(
    "configured, expected",
    [("https://pve.lab.local:8006", "https://pve.lab.local:8006"), ("", "Not configured")],
)
def test_the_env_panel_reports_proxmox_host_from_the_environment(
    client, admin_headers, settings, configured, expected
):
    """Both directions, and **overridden rather than read from the real `.env`.**

    This assertion previously hardcoded "Not configured", which was true only
    because no developer had a Proxmox host set — so V1.4 configuring one turned
    a green test red on the machine that did the work, and it would have behaved
    differently for every operator. A test about how a value is *displayed* must
    supply the value.
    """
    settings.PROXMOX_HOST = configured
    assert _env_rows(client, admin_headers)["PROXMOX_HOST"]["status"] == expected


def test_the_proxmox_token_reports_presence_and_never_its_value(
    client, admin_headers, settings
):
    """§7a's credential must not reach an API response, even the admin one.

    `_SECRET_NAME_FRAGMENTS` already refuses to let a TOKEN-named variable be
    declared `_SHOW_VALUE`, so this asserts the *outcome* that guard exists for
    rather than the guard itself — the two fail independently.
    """
    settings.PROXMOX_API_TOKEN = "PVEAPIToken=hlv@pve!ro=00000000-1111-2222-3333-444444444444"
    body = client.get("/api/v1/settings", **admin_headers).content.decode()
    assert "00000000-1111-2222-3333-444444444444" not in body
    assert "PVEAPIToken" not in body
    assert _env_rows(client, admin_headers)["PROXMOX_API_TOKEN"]["status"] == "Configured"


# --- The recovery export excludes settings (§6a) --------------------------


def test_the_recovery_export_carries_no_settings(client, admin_headers, agent_headers):
    """`GET /api/v1/export/json` is public. Including settings would leak
    notification config and environment variable names to unauthenticated
    readers, undoing the admin scope through a side door — and `pg_dump` already
    captures the table.
    """
    client.post(
        "/api/v1/services", **body({"launch_url": "https://x.lab.local"}), **agent_headers
    )
    client.patch(
        "/api/v1/settings",
        **body({"notifications.discord.webhook_env": "MY_WEBHOOK_URL"}),
        **admin_headers,
    )

    response = client.get("/api/v1/export/json")
    assert response.status_code == 200
    payload = response.json()
    # A list of services, not an object that could grow a settings member.
    assert isinstance(payload, list)
    assert {s["slug"] for s in payload} == {"x"}

    serialised = json.dumps(payload)
    for key in settings_store.SPECS:
        assert key not in serialised
    assert "MY_WEBHOOK_URL" not in serialised
    assert "HOMELABVAULT" not in serialised


def test_the_static_export_file_carries_no_settings(client, admin_headers, agent_headers, settings, tmp_path):
    """Same rule for the file written to the NAS share (§9): it is opened by
    hand during an outage, where a theme preference helps nobody and an
    environment variable name is a hint nobody needed to leave lying around."""
    from services.export_static import run_static_export

    client.post(
        "/api/v1/services", **body({"launch_url": "https://x.lab.local"}), **agent_headers
    )
    client.patch(
        "/api/v1/settings",
        **body({"notifications.discord.webhook_env": "MY_WEBHOOK_URL"}),
        **admin_headers,
    )
    target = tmp_path / "export" / "index.html"
    settings.STATIC_EXPORT_PATH = str(target)
    run_static_export()

    written = target.read_text(encoding="utf-8")
    assert "MY_WEBHOOK_URL" not in written
    for key in settings_store.SPECS:
        assert key not in written


# --- Consumers are named (standing rule 2) --------------------------------


def test_every_key_names_the_chunk_that_consumes_it():
    """A settings screen that saves and reads back while no worker ever reads
    the table is a fully green chunk with an inert screen. Naming the consumer
    in the registry is what makes the later chunk's wire test findable."""
    unowned = [spec.key for spec in settings_store.REGISTRY if not spec.consumer]
    assert unowned == [], f"keys with no named consumer: {unowned}"


def test_services_are_untouched_by_settings_writes(client, admin_headers, agent_headers):
    """A sanity check that the new table is genuinely separate state."""
    client.post(
        "/api/v1/services", **body({"launch_url": "https://x.lab.local"}), **agent_headers
    )
    before = Service.objects.get(slug="x").updated_at
    client.patch("/api/v1/settings", **body({"health.fail_threshold": 7}), **admin_headers)
    assert Service.objects.get(slug="x").updated_at == before
