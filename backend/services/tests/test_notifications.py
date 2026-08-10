"""Discord notifications — v1 §5, §4 (`notify_enabled`).

Three things are being defended here, and only the first is about Discord:

1. **The webhook URL is a credential.** It must not appear in an API response,
   in a log line, or in the recovery export.
2. **Probe-derived text is attacker-influenced** (BUILD_LOG's frontend
   constraint). A service title reaching a channel unescaped is the same class of
   problem as one reaching `v-html`.
3. **Damping is symmetric**, and `notify_enabled` is not `health_enabled`.
"""

import json
import logging

import httpx
import pytest

from services import notifications
from services.health.transitions import Applied, apply_result, should_notify
from services.models import Service
from services.settings_store import write_settings

pytestmark = pytest.mark.django_db

WEBHOOK = "https://discord.com/api/webhooks/123456789/s3cr3t-token-value"


def make(**kwargs):
    defaults = {"slug": "svc", "name": "Svc", "launch_url": "https://svc.lab.local"}
    return Service.objects.create(**{**defaults, **kwargs})


# --- The webhook is a credential ------------------------------------------


def test_no_settings_key_can_hold_the_url(settings):
    """The table stores the NAME of an environment variable (§4).

    `settings_store` has no free-text kind, so this is a property of the schema
    — but assert it from this side too, because *this* is the credential the
    rule exists for.
    """
    from services.settings_store import SettingRejected

    with pytest.raises(SettingRejected):
        write_settings({"notifications.discord.webhook_env": WEBHOOK})


def test_the_admin_settings_response_never_carries_the_url(
    client, admin_headers, settings
):
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK
    response = client.get("/api/v1/settings", **admin_headers)
    body = response.content.decode()

    assert response.status_code == 200
    assert WEBHOOK not in body
    assert "s3cr3t-token-value" not in body
    # Presence, not value — the operator still needs to know it is configured.
    assert "HOMELABVAULT_DISCORD_WEBHOOK_URL" in body
    assert "Configured" in body


def test_the_recovery_export_never_carries_the_url(client, settings):
    """§9's export is written to a share and opened during an outage — a place a
    credential must never end up."""
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK
    make()

    from services.export_static import render_export

    assert WEBHOOK not in render_export(list(Service.objects.all()))
    assert WEBHOOK not in client.get("/api/v1/export/json").content.decode()


def test_a_failure_logs_the_variable_name_and_never_the_url(caplog, settings):
    """A webhook URL in a log file is a leaked credential — and the natural way
    to write this handler leaks it, because httpx exceptions stringify with the
    request URL in them."""
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK

    def explode(*args, **kwargs):
        raise httpx.ConnectError(f"failed to connect to {WEBHOOK}")

    with caplog.at_level(logging.WARNING):
        notifications.httpx.post = explode
        try:
            assert notifications.send("hello") is False
        finally:
            notifications.httpx.post = httpx.post

    assert WEBHOOK not in caplog.text
    assert "s3cr3t-token-value" not in caplog.text
    assert "HOMELABVAULT_DISCORD_WEBHOOK_URL" in caplog.text


def test_an_unset_webhook_is_a_warning_naming_the_variable_not_a_crash(caplog):
    with caplog.at_level(logging.WARNING):
        assert notifications.send("hello") is False
    assert "HOMELABVAULT_DISCORD_WEBHOOK_URL" in caplog.text


def test_the_variable_the_settings_table_names_is_the_one_resolved(
    settings, monkeypatch
):
    """The indirection has to actually work, or the setting is decoration."""
    monkeypatch.setenv("SOME_OTHER_WEBHOOK_VAR", WEBHOOK)
    write_settings({"notifications.discord.webhook_env": "SOME_OTHER_WEBHOOK_VAR"})

    assert notifications.webhook_url() == WEBHOOK
    assert notifications.is_configured() is True


# --- Attacker-influenced text ----------------------------------------------


def test_mentions_are_inert():
    """A probed page title of `@everyone` must not ping the server.

    `parse: []` is the control; escaping is cosmetic next to it, because
    Discord parses mentions out of message content by default. With nothing
    configured, the allowlist arrays are empty too — nobody can be pinged.
    """
    payload = notifications.build_payload("@everyone @here <@&1234>")
    assert payload["allowed_mentions"] == {"parse": [], "users": [], "roles": []}


DOWN = Applied(transition="down", previous="up")


def test_a_hostile_service_name_cannot_forge_a_second_alert():
    service = make(name="Svc\n🔴 **admin-panel** is down")
    message = notifications.service_transition_message(service, DOWN)
    assert "\n" not in message["content"]
    # The embed title is rendered by Discord too — same rule there.
    assert "\n" not in message["embed"]["title"]


def test_markdown_in_a_service_name_is_escaped_not_stripped():
    service = make(name="C++ `backtick` *stars*")
    message = notifications.service_transition_message(service, DOWN)
    assert "C++" in message["content"]
    assert "\\`backtick\\`" in message["content"]


def test_content_is_bounded():
    payload = notifications.build_payload("x" * 5000)
    assert len(payload["content"]) <= notifications.MAX_CONTENT


# --- The embed is a stable, parseable schema (BACKLOG.2) --------------------
#
# The point of the embed is that an agent watching the channel can act on
# fields instead of parsing prose — which only holds while the field set is
# fixed. These tests ARE that contract.


def _fields(message):
    return {f["name"]: f["value"] for f in message["embed"]["fields"]}


def test_a_down_alert_carries_the_fields_an_agent_needs():
    from django.utils import timezone

    service = make(
        name="OmniRoute",
        launch_url="http://192.168.0.10:8096",
        category="Media",
        tags=["arr", "downloads"],
        health_changed_at=timezone.now(),
    )
    message = notifications.service_transition_message(service, DOWN)

    fields = _fields(message)
    assert fields["Event"] == "went down"
    assert fields["URL"] == "http://192.168.0.10:8096"
    assert fields["Category"] == "Media"
    assert fields["Tags"] == "arr, downloads"
    assert message["embed"]["footer"]["text"] == "svc"
    assert message["embed"]["color"] == notifications.COLOR_DOWN
    assert message["embed"]["timestamp"] == service.health_changed_at.isoformat()
    # The plain line survives as `content` — push previews and the mention
    # carrier both live there.
    assert "OmniRoute" in message["content"]


def test_a_recovery_reports_how_long_it_was_down():
    """`health_changed_at` gives the outage span with no history table: the
    previous value is when it fell, the new value is when it recovered."""
    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    service = make(health_status="up", health_changed_at=now)
    applied = Applied(
        transition="up",
        previous="down",
        previous_changed_at=now - timedelta(minutes=14),
    )
    message = notifications.service_transition_message(service, applied)

    fields = _fields(message)
    assert fields["Event"] == "recovered"
    assert fields["Was down for"] == "14 minutes"
    assert message["embed"]["color"] == notifications.COLOR_UP


def test_a_fall_never_claims_an_outage_duration():
    service = make()
    message = notifications.service_transition_message(service, DOWN)
    assert "Was down for" not in _fields(message)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.domain)",
        "data:text/html,<script>1</script>",
        "192.168.0.10:8096",  # scheme-less — refused at write since the
        # 2026-08-10 rule, but rows written around the API must still not ping
        "",
    ],
)
def test_a_non_http_url_never_reaches_the_embed(url):
    """The same rule `safeUrl()` enforces in the frontend (V1.5 gate) and the
    write path now enforces at the source: the URL field exists so an agent
    can follow it blindly, so only http(s) qualifies. This sink keeps its own
    guard for rows that never met `clean()` — manual psql, a migration bug."""
    service = make(launch_url=url)
    message = notifications.service_transition_message(service, DOWN)
    assert "URL" not in _fields(message)


def test_empty_optional_fields_are_omitted_not_blank():
    service = make(category="", tags=[])
    fields = _fields(notifications.service_transition_message(service, DOWN))
    assert "Category" not in fields
    assert "Tags" not in fields


def test_embeds_are_clamped_to_discord_limits():
    """An oversized embed must degrade to truncation, never to a 400 that
    swallows the alert it was carrying.

    Fed to `build_payload` directly rather than through a builder: the
    builders' own `sanitize` caps keep everything they produce under the
    limits already (the first draft of this test went through
    `service_transition_message` and stayed green with the clamp deleted —
    the red-check harness caught it). The clamp is the net under whatever
    message path gets added next, so it is tested at its own seam.
    """
    huge = {
        "title": "T" * 5000,
        "fields": [{"name": "N" * 5000, "value": "V" * 5000}] * 30,
    }
    payload = notifications.build_payload({"content": "x", "embed": huge})
    (embed,) = payload["embeds"]
    assert len(embed["title"]) <= notifications.MAX_EMBED_TITLE
    assert len(embed["fields"]) <= 25
    for field in embed["fields"]:
        assert len(field["name"]) <= notifications.MAX_EMBED_TITLE
        assert len(field["value"]) <= notifications.MAX_FIELD_VALUE


def test_worker_and_provider_messages_are_embeds_with_an_event_field():
    """Every alert in the channel carries the same parseable shape — an agent
    should not need a second parser for worker and provider alerts."""
    messages = [
        notifications.worker_stall_message("health", 180, 200),
        notifications.worker_recovery_message("health"),
        notifications.provider_failure_message("proxmox", 3),
        notifications.provider_recovery_message("proxmox", 5),
    ]
    for message in messages:
        assert message["content"]
        assert "Event" in _fields(message)


def test_a_plain_string_message_still_builds_a_payload():
    """`send("hello")` remains valid — ops scripts and tests use it."""
    payload = notifications.build_payload("hello")
    assert payload["content"] == "hello"
    assert "embeds" not in payload


# --- The mention is an allowlist, never parsing (BACKLOG.1) ------------------


def test_a_configured_mention_pings_the_configured_id_and_nobody_else():
    """THE regression test this feature was gated on: a service literally named
    `@everyone`, plus a configured mention — the payload pings the configured
    snowflake and nothing that arrived as text.
    """
    write_settings({"notifications.discord.mention_user": "123456789012345678"})  # gitleaks:allow — synthetic snowflake
    service = make(name="@everyone")
    message = notifications.service_transition_message(service, DOWN)
    payload = notifications.build_payload(message, notifications.mention_targets())

    assert payload["allowed_mentions"] == {
        "parse": [],
        "users": ["123456789012345678"],
        "roles": [],
    }
    assert payload["content"].startswith("<@123456789012345678> ")
    # The hostile name is still IN the message — as inert text.
    assert "@everyone" in payload["content"]


def test_a_configured_role_mention_is_carried_the_same_way():
    write_settings({"notifications.discord.mention_role": "987654321098765432"})  # gitleaks:allow — synthetic snowflake
    payload = notifications.build_payload("hello", notifications.mention_targets())
    assert payload["allowed_mentions"]["roles"] == ["987654321098765432"]
    assert payload["allowed_mentions"]["parse"] == []
    assert "<@&987654321098765432>" in payload["content"]


def test_no_mention_configured_means_no_mention_arrays_populated():
    payload = notifications.build_payload("hello", notifications.mention_targets())
    assert payload["allowed_mentions"] == {"parse": [], "users": [], "roles": []}
    assert payload["content"] == "hello"


@pytest.mark.parametrize(
    "value",
    [
        "@everyone",
        "everyone",
        "jason",
        "<@123456789012345678>",  # the chat syntax, not the ID
        "1234",  # too short to be a snowflake
        "1" * 21,  # too long
        "123456789012345678 ",  # trailing junk
        123456789012345678,  # right digits, wrong type — JSON numbers lose precision
    ],
)
def test_the_mention_setting_refuses_everything_that_is_not_an_id(value):
    from services.settings_store import SettingRejected

    with pytest.raises(SettingRejected):
        write_settings({"notifications.discord.mention_user": value})


def test_a_corrupted_mention_row_is_dropped_not_pinged():
    """Defense in depth (security gate, this window): `coerce()` is the write
    boundary, but the payload builder must not trust the table either — a row
    written around the API (a manual psql session, a migration bug) fails
    closed to "mention nobody" rather than reaching the ping prefix."""
    from services.models import Setting

    Setting.objects.update_or_create(
        key="notifications.discord.mention_user", defaults={"value": "@everyone"}
    )
    Setting.objects.update_or_create(
        key="notifications.discord.mention_role", defaults={"value": "not-an-id"}
    )
    assert notifications.mention_targets() == ([], [])
    payload = notifications.build_payload("hello", notifications.mention_targets())
    assert payload["allowed_mentions"] == {"parse": [], "users": [], "roles": []}


def test_the_mention_setting_accepts_an_id_and_empty():
    write_settings({"notifications.discord.mention_user": "123456789012345678"})  # gitleaks:allow — synthetic snowflake
    assert notifications.mention_targets() == (["123456789012345678"], [])
    write_settings({"notifications.discord.mention_user": ""})
    assert notifications.mention_targets() == ([], [])


def test_dispatch_resolves_mentions_on_the_calling_thread(settings, monkeypatch):
    """The daemon thread does no ORM work — the mention IDs are settings-table
    reads, so they must travel as arguments like the webhook URL does.

    Asserted by recording WHICH thread calls `mention_targets`: the
    connection-count net cannot see this one, because `releases_connections`
    tidies up after a thread-side read and the leak test stays green (found by
    the red-check harness). The property is *where* the read happens, so that
    is what is asserted.
    """
    import threading

    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK
    write_settings({"notifications.discord.mention_user": "123456789012345678"})  # gitleaks:allow — synthetic snowflake
    calling_threads = []
    real_targets = notifications.mention_targets

    def recording_targets():
        calling_threads.append(threading.current_thread().name)
        return real_targets()

    monkeypatch.setattr(notifications, "mention_targets", recording_targets)
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(notifications.httpx, "post", fake_post)
    notifications.dispatch("hello").join(timeout=5)

    assert captured["json"]["allowed_mentions"]["users"] == ["123456789012345678"]
    assert calling_threads == ["MainThread"]  # never "hlv-discord"


# --- notify_enabled is not health_enabled ---------------------------------


def test_notify_disabled_service_is_checked_and_silent():
    """§4's separation is the whole point: loud on the dashboard, quiet at 03:00.

    The service must still transition — this is not `health_enabled=False`.
    """
    service = make(health_status="up", notify_enabled=False, health_fail_threshold=1)
    applied = apply_result(service, ok=False)

    assert service.health_status == "down"  # still checked, still red
    assert applied.transition == "down"
    assert should_notify(service, applied) is False


def test_notify_enabled_service_is_announced():
    service = make(health_status="up", notify_enabled=True, health_fail_threshold=1)
    applied = apply_result(service, ok=False)
    assert should_notify(service, applied) is True


def test_first_contact_is_not_announced_as_a_recovery():
    """"is back up" is a lie about a service that was never down — and after a
    bulk import it would be a lie posted once per service."""
    service = make(health_status="unknown")
    applied = apply_result(service, ok=True)
    assert applied.transition == "up"
    assert should_notify(service, applied) is False


def test_a_recovery_from_down_is_announced():
    service = make(health_status="down")
    write_settings({"health.recovery_threshold": 1})
    applied = apply_result(service, ok=True)
    assert applied.transition == "up"
    assert should_notify(service, applied) is True


# --- Damping, in both directions ------------------------------------------


def test_a_flapping_service_is_not_announced_on_every_bounce():
    """The failure this damping prevents, reproduced.

    Without up-damping, down/up/down/up posts four messages; the channel gets
    muted, and the mute takes the real alerts with it.
    """
    write_settings({"health.fail_threshold": 2, "health.recovery_threshold": 2})
    service = make(health_status="up")
    announced = []

    for ok in [False, True, False, True, False, True]:
        applied = apply_result(service, ok=ok)
        if should_notify(service, applied):
            announced.append(applied.transition)

    assert announced == []
    assert service.health_status == "up"


def test_sustained_failure_then_sustained_recovery_announces_once_each():
    write_settings({"health.fail_threshold": 2, "health.recovery_threshold": 2})
    service = make(health_status="up")
    announced = []

    for ok in [False, False, False, True, True, True]:
        applied = apply_result(service, ok=ok)
        if should_notify(service, applied):
            announced.append(applied.transition)

    assert announced == ["down", "up"]


# --- The transport ---------------------------------------------------------


def test_send_posts_the_payload_and_reports_success(settings, monkeypatch):
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(notifications.httpx, "post", fake_post)
    assert notifications.send("hello") is True
    assert captured["url"] == WEBHOOK
    assert captured["json"]["content"] == "hello"
    # A hung webhook must not hold a health cycle open indefinitely.
    assert captured["timeout"] is not None


def test_a_rejected_post_is_reported_not_raised(settings, monkeypatch, caplog):
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK
    monkeypatch.setattr(
        notifications.httpx,
        "post",
        lambda url, **kw: httpx.Response(404, request=httpx.Request("POST", url)),
    )
    with caplog.at_level(logging.WARNING):
        assert notifications.send("hello") is False
    assert "404" in caplog.text
    assert WEBHOOK not in caplog.text


def test_dispatch_does_not_block_the_caller(settings, monkeypatch):
    """§5: a slow or failing webhook must never block a health cycle.

    The proof is that the caller returns while the send is still in flight —
    asserted by blocking the fake transport until the test releases it.
    """
    import threading

    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK
    in_flight = threading.Event()
    release = threading.Event()

    def slow_post(url, **kwargs):
        in_flight.set()
        release.wait(timeout=5)
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(notifications.httpx, "post", slow_post)

    thread = notifications.dispatch("hello")
    assert in_flight.wait(timeout=5), "send never started"
    assert thread.is_alive()  # the caller got here with the POST still open

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


# --- The `secrets-management` gate, 2026-08-05 ----------------------------


def test_a_variable_that_does_not_hold_a_url_is_refused(settings, caplog, monkeypatch):
    """Pointing `webhook_env` at the wrong variable must not put its value in a
    request line.

    The settings table names *which* environment variable holds the webhook, so
    a wrong name is one keystroke away — and a variable that does not hold a URL
    is quite likely one holding a credential. For any syntactically valid http
    URL, sending it would mean handing that value to whatever host it names.
    """
    posted = []
    monkeypatch.setattr(notifications.httpx, "post", lambda url, **kw: posted.append(url))
    monkeypatch.setenv("SOME_TOKEN_VAR", "sk-live-9f3a2b-not-a-url")
    write_settings({"notifications.discord.webhook_env": "SOME_TOKEN_VAR"})

    with caplog.at_level(logging.WARNING):
        assert notifications.send("hello") is False

    assert posted == []
    assert "sk-live-9f3a2b" not in caplog.text
    assert "SOME_TOKEN_VAR" in caplog.text


def test_a_plain_http_webhook_is_refused(settings):
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = "http://discord.com/api/webhooks/1/x"
    assert notifications.webhook_url() is None
    assert notifications.is_configured() is False


def test_an_exception_outside_httpx_HTTPError_does_not_escape_the_thread(
    settings, monkeypatch, caplog
):
    """`httpx.InvalidURL` is not a subclass of `httpx.HTTPError`.

    Verified against httpx rather than assumed. `send` runs on a daemon thread,
    so an escaped exception is an unhandled traceback and *no* line telling the
    operator which variable is wrong — a failure that reads as the app being
    broken rather than as a misconfiguration.
    """
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK

    def explode(*args, **kwargs):
        raise httpx.InvalidURL("malformed")

    monkeypatch.setattr(notifications.httpx, "post", explode)
    with caplog.at_level(logging.WARNING):
        assert notifications.send("hello") is False
    assert "InvalidURL" in caplog.text


def test_the_webhook_is_never_written_into_a_message():
    """Belt and braces: the payload builder has no path to the credential."""
    payload = notifications.build_payload("🔴 **Svc** is down")
    assert WEBHOOK not in json.dumps(payload)


# --- The webhook must not reach the logs (2026-08-07) ----------------------


def test_the_webhook_url_never_reaches_the_logs(settings, monkeypatch, caplog):
    """A Discord webhook's secret is **in its URL path**, so anything that logs
    the request line logs the credential.

    V1.3's gate required the webhook never be logged and verified our own
    failure path. It missed that `httpx` announces every request at INFO on its
    own — so each notification wrote
    `POST https://discord.com/api/webhooks/<id>/<token>` to the console, which in
    Docker is `docker logs` plus anything shipping it onward. Found while
    investigating why the deadman had not alerted.

    Asserted as an **outcome** — no log record anywhere contains the secret —
    rather than by checking `LOGGING`, because a config assertion passes just as
    happily when a later handler re-enables it.
    """
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK
    monkeypatch.setattr(
        notifications.httpx,
        "post",
        lambda url, json=None, timeout=None: httpx.Response(
            204, request=httpx.Request("POST", url)
        ),
    )

    with caplog.at_level(logging.DEBUG):
        assert notifications.send("hello") is True

    logged = "\n".join(record.getMessage() for record in caplog.records)
    secret = WEBHOOK.rsplit("/", 1)[-1]
    assert WEBHOOK not in logged
    assert secret not in logged


def test_a_failed_send_still_names_the_variable_and_not_the_url(
    settings, monkeypatch, caplog
):
    """The diagnostic has to stay useful without becoming the leak.

    An operator needs to know *which* variable is misconfigured; they do not need
    the value, and the value is the credential.
    """
    settings.HOMELABVAULT_DISCORD_WEBHOOK_URL = WEBHOOK

    def boom(url, json=None, timeout=None):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(notifications.httpx, "post", boom)

    with caplog.at_level(logging.DEBUG):
        assert notifications.send("hello") is False

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "HOMELABVAULT_DISCORD_WEBHOOK_URL" in logged
    assert WEBHOOK not in logged
