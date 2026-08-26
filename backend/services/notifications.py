"""Discord notifications — v1 §5.

**Direct, not routed through Hermes.** Uptime alerting must not depend on an LLM
being reachable: the moment it does, the thing that tells you the lab is broken
shares a failure mode with the lab.

Three properties this module is built to hold, in the order they matter.

**1. The webhook URL is a credential and never leaves this module.** It is not in
the settings table — nothing there can hold free text (`settings_store`, decision
1) — so the table holds the NAME of an environment variable and the value is
resolved at use time. It is never returned by an API, never written to the
recovery export, and never logged: failures log the variable *name* and the HTTP
status, which is everything you need to fix a misconfiguration and nothing an
attacker can post with.

**2. Nothing service-supplied is trusted.** A service `name` can come from a
probed page title, which is attacker-influenced text (BUILD_LOG's frontend
constraint says the same thing about `v-html`). Three defences, because Discord
renders what you send: `allowed_mentions` is set to parse nothing, so `@everyone`
in a page title stays inert; backticks and markdown are escaped; and the text is
truncated. The first one is the load-bearing one — the other two are cosmetic by
comparison.

**3. A slow webhook must never block a health cycle.** `send()` runs on a
short-lived daemon thread. `dispatch()` resolves the webhook **on the calling
thread** and hands the thread a plain string, so the thread does no ORM work and
opens no connection of its own.

> That last property was asserted here before it was true, and the suite caught
> it: `send()` reaches the settings table to find out which environment variable
> to read, so a dispatched thread *did* open a connection and never hand it back
> — which does not fail the run that leaks it. It fails that run's
> `DROP DATABASE`, orphans the test database, and breaks the **next** run. Same
> shape as V1.0's `sync_to_async` leak, and the same rule applies: **connection
> hygiene belongs to whoever owns the thread.** `dispatch` owns this one, so it
> resolves first and wraps the target in `releases_connections` as a net for
> whatever gets added to `send()` later.
"""

import logging
import os
import re
import threading

import httpx
from django.conf import settings

from .db import releases_connections

logger = logging.getLogger("services.notifications")

#: Discord rejects bodies over 2000 characters outright.
MAX_CONTENT = 1800
#: A page title is a line of text; anything longer is not a service name.
MAX_NAME = 120
#: Discord's own embed limits, enforced here so a probed title can never make
#: the webhook 400 and silently swallow the alert it was carrying.
MAX_EMBED_TITLE = 256
MAX_FIELD_VALUE = 1024

#: Embed accent colours. Discord's brand palette, not the app theme — the
#: "CSS variables only" rule governs our UI; an embed renders in Discord's,
#: where these are the colours its own clients use for the same meanings.
COLOR_DOWN = 0xED4245
COLOR_UP = 0x57F287
COLOR_WARN = 0xE67E22

_TIMEOUT_S = 5.0

#: Characters Discord treats as formatting. Escaped rather than stripped so a
#: service legitimately called `C++` still reads as itself.
_MARKDOWN = re.compile(r"([\\`*_~|>])")
#: C0 controls minus nothing: a newline injected into a service name lets the
#: message impersonate a second alert.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def webhook_env_name() -> str:
    """The environment variable the settings table points at."""
    from .settings_store import get_setting

    return get_setting("notifications.discord.webhook_env")


def mention_targets() -> tuple[list[str], list[str]]:
    """The configured mention IDs — `(users, roles)`, each empty or one ID.

    **This is BACKLOG.1's security shape: an explicit allowlist, never parsing.**
    The IDs come from settings keys whose kind refuses anything but a Discord
    snowflake, and they are named verbatim in `allowed_mentions` — mentions
    resolve because this exact ID was configured, not because parsing was
    switched on. Text arriving through a probed page title stays inert however
    it is spelled.

    Reads the settings table: callers on a daemon thread must resolve this on
    the dispatching thread, exactly like the webhook URL.

    Re-validated against the snowflake shape here even though `coerce()`
    already refused anything else at write time (security gate, this window):
    a row written around the API — a manual psql session, a migration bug —
    must fail closed to "mention nobody", never reach the ping prefix. Same
    layering as the auth-mode coercion: the boundary check and the use-site
    check defend independently.
    """
    from .settings_store import DISCORD_MENTION_RE, get_setting

    def valid(value):
        return [value] if isinstance(value, str) and DISCORD_MENTION_RE.match(value) else []

    return (
        valid(get_setting("notifications.discord.mention_user")),
        valid(get_setting("notifications.discord.mention_role")),
    )


def webhook_url(env_name: str | None = None) -> str | None:
    """Resolve the webhook URL at use time. Never cached, never returned upward.

    Reads Django settings first so a deployment can carry it the same way it
    carries every other secret, then the raw environment — the worker container
    gets it through compose either way.

    **The value must look like an https URL** (`secrets-management` gate,
    2026-08-05). The settings table names *which* variable holds the webhook, so
    a wrong name is one keystroke away — and a variable holding something that
    is not a URL is very likely one holding a credential. Without this check the
    app takes that value and puts it in a request line, which for any
    syntactically valid http URL means sending the credential to whatever host
    it happens to name. Refusing here means the only thing that ever leaves is
    something that was already a webhook endpoint.

    Nothing about the value is logged — only the variable's name.
    """
    name = env_name or webhook_env_name()
    raw = getattr(settings, name, None) or os.environ.get(name)
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return None
    if not value.startswith("https://"):
        logger.warning(
            "%s does not hold an https:// URL, so no notification was sent. "
            "Check that it names the Discord webhook and not another variable.",
            name,
        )
        return None
    return value


def is_configured() -> bool:
    """True when a webhook URL actually resolves. Used by `/healthz` and the
    settings screen, neither of which may see the value itself."""
    return webhook_url() is not None


def sanitize(text: str, limit: int = MAX_NAME) -> str:
    """Neutralise probe-derived text on its way into a Discord message."""
    cleaned = _CONTROL.sub(" ", str(text)).strip()
    cleaned = _MARKDOWN.sub(r"\\\1", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned or "(unnamed)"


def build_payload(message, mentions: tuple[list[str], list[str]] | None = None) -> dict:
    """The request body. `allowed_mentions` is the security control here.

    Discord parses `@everyone` out of message *content* by default. A service
    whose probed title is `@everyone` would otherwise ping the server on every
    transition — attacker-influenced text reaching a side effect, which is the
    definition of the problem. `parse` stays empty **always**; a configured
    mention works by being named explicitly in `users`/`roles`, which is the
    only way BACKLOG.1 could be added without handing that power back.

    Mention strings only ping from `content` (never from an embed), so the
    configured IDs are prepended there. `message` is a builder dict
    (`content` + optional `embed`) or a plain string.
    """
    if isinstance(message, str):
        message = {"content": message}
    users, roles = mentions or ([], [])
    prefix = "".join(
        [f"<@{uid}> " for uid in users] + [f"<@&{rid}> " for rid in roles]
    )
    payload = {
        "content": (prefix + message.get("content", ""))[:MAX_CONTENT],
        "allowed_mentions": {"parse": [], "users": users, "roles": roles},
    }
    embed = message.get("embed")
    if embed:
        payload["embeds"] = [_capped(embed)]
    return payload


def _capped(embed: dict) -> dict:
    """Clamp an embed to Discord's limits so an oversized field degrades to a
    truncated message instead of a 400 that swallows the whole alert."""
    capped = dict(embed)
    if "title" in capped:
        capped["title"] = str(capped["title"])[:MAX_EMBED_TITLE]
    capped["fields"] = [
        {
            "name": str(f["name"])[:MAX_EMBED_TITLE],
            "value": str(f["value"])[:MAX_FIELD_VALUE],
            "inline": bool(f.get("inline", True)),
        }
        for f in capped.get("fields", [])[:25]
    ]
    return capped


def send(
    content,
    url: str | None = None,
    env_name: str | None = None,
    mentions: tuple[list[str], list[str]] | None = None,
) -> bool:
    """POST one message. Returns success; never raises, never logs the URL.

    Passing `url`, `env_name` and `mentions` makes this function ORM-free,
    which is what `dispatch()` does — see the module docstring. Called without
    them it resolves all three itself, which is what a synchronous caller wants.
    """
    env_name = env_name or webhook_env_name()
    target = url or webhook_url(env_name)
    if mentions is None:
        mentions = mention_targets()
    if not target:
        logger.warning(
            "Discord notification skipped: %s is not set. Set it in .env and "
            "restart, or switch notifications off in Settings.",
            env_name,
        )
        return False
    try:
        response = httpx.post(
            target, json=build_payload(content, mentions), timeout=_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - see below
        # Two reasons this is deliberately broad, both found by the gate:
        #
        # 1. `httpx.InvalidURL` is **not** a subclass of `httpx.HTTPError`
        #    (verified against httpx directly, not assumed), so catching
        #    HTTPError alone lets it escape — and `send` runs on a daemon
        #    thread, where an escaped exception is an unhandled-exception
        #    traceback and no log line telling the operator which variable is
        #    wrong.
        # 2. Notifications are never load-bearing. Nothing about failing to
        #    announce a status change justifies affecting anything else.
        #
        # The class name rather than `exc`: an httpx error stringifies with the
        # request URL in it, and the URL is the credential.
        logger.warning(
            "Discord notification failed: %s (webhook from %s)",
            type(exc).__name__,
            env_name,
        )
        return False
    if response.status_code >= 400:
        logger.warning(
            "Discord rejected the notification: HTTP %s (webhook from %s)",
            response.status_code,
            env_name,
        )
        return False
    return True


def dispatch(content) -> threading.Thread:
    """Send on a daemon thread. Returns it so tests can join deterministically.

    §5's guarantee is that a slow or failing webhook never blocks a health cycle,
    and this is where that is enforced.

    The lookups happen **here**, on the caller's thread — the webhook URL and
    the mention IDs both live behind the settings table — so the thread this
    function starts never touches the database. `releases_connections` is the
    net under that, not the mechanism: it costs nothing today and catches the
    day somebody adds an ORM read to `send()`.
    """
    env_name = webhook_env_name()
    target = webhook_url(env_name)
    mentions = mention_targets()
    thread = threading.Thread(
        target=releases_connections(send),
        args=(content, target, env_name, mentions),
        name="hlv-discord",
        daemon=True,
    )
    thread.start()
    return thread


# --- Messages --------------------------------------------------------------
#
# Built here rather than at each call site so every message that can reach
# Discord passes through `sanitize`. Each builder returns a message dict —
# `content` (the plain line: what a push notification shows, and the only
# place a mention can ping from) plus an `embed` whose titled fields are a
# STABLE schema an agent watching the channel can parse (BACKLOG.2). The
# fields are deliberately fixed in code, not templated: a parseable alert
# format is only parseable while nobody can reshape it.


def _human_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if seconds < 86400:
        hours = round(seconds / 3600 * 10) / 10
        hours = int(hours) if hours == int(hours) else hours
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = round(seconds / 86400 * 10) / 10
    days = int(days) if days == int(days) else days
    return f"{days} day{'s' if days != 1 else ''}"


def _url_field(raw) -> str:
    """A service URL on its way into an embed field.

    Not `sanitize()`: markdown-escaping corrupts a URL (`_` is common in real
    ones), and the risk profile differs — an embed never turns text into a
    navigation, so the only URL worth carrying is one that is *usable*, which
    means http(s) only. The same rule `safeUrl()` applies in the frontend
    (api-security gate, V1.5): a stored `javascript:` URL is dropped here too,
    so the agent reading the field can follow it blindly.
    """
    text = _CONTROL.sub("", str(raw or "")).strip()
    lowered = text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return ""
    return text[:500]


def service_transition_message(service, applied) -> dict:
    """One service fell or recovered. `applied` is the transition that says so."""
    name = sanitize(service.name)
    slug = sanitize(service.slug, limit=80)
    down = applied.transition == "down"

    fields = [{"name": "Event", "value": "went down" if down else "recovered",
               "inline": True}]
    if not down and applied.previous_changed_at and service.health_changed_at:
        outage = (service.health_changed_at - applied.previous_changed_at).total_seconds()
        fields.append(
            {"name": "Was down for", "value": _human_duration(outage), "inline": True}
        )
    url = _url_field(service.launch_url)
    if url:
        fields.append({"name": "URL", "value": url, "inline": False})
    if service.category:
        fields.append(
            {"name": "Category", "value": sanitize(service.category), "inline": True}
        )
    if service.tags:
        fields.append(
            {
                "name": "Tags",
                "value": sanitize(", ".join(service.tags), limit=200),
                "inline": True,
            }
        )

    if down:
        content = f"🔴 **{name}** is down (`{slug}`)"
        title = f"🔴 {name} is down"
    else:
        content = f"🟢 **{name}** is back up (`{slug}`)"
        title = f"🟢 {name} is back up"
    embed = {
        "title": title,
        "color": COLOR_DOWN if down else COLOR_UP,
        "fields": fields,
        "footer": {"text": slug},
    }
    if service.health_changed_at:
        embed["timestamp"] = service.health_changed_at.isoformat()
    return {"content": content, "embed": embed}


def worker_stall_message(name: str, stale_after_s: int, age_s: int) -> dict:
    worker = sanitize(name, limit=60)
    return {
        "content": (
            f"⚠️ Worker **{worker}** has stopped reporting — "
            f"no cycle for {int(age_s)}s (alerts after {int(stale_after_s)}s)."
        ),
        "embed": {
            "title": f"⚠️ Worker {worker} has stopped reporting",
            "color": COLOR_WARN,
            "fields": [
                {"name": "Event", "value": "worker stalled", "inline": True},
                {"name": "No cycle for", "value": f"{int(age_s)}s", "inline": True},
                {"name": "Alerts after", "value": f"{int(stale_after_s)}s", "inline": True},
                {
                    "name": "Effect",
                    "value": (
                        "Checks are not running; every service status is frozen "
                        "at its last value."
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": worker},
        },
    }


def worker_recovery_message(name: str) -> dict:
    worker = sanitize(name, limit=60)
    return {
        "content": f"✅ Worker **{worker}** is reporting again.",
        "embed": {
            "title": f"✅ Worker {worker} is reporting again",
            "color": COLOR_UP,
            "fields": [
                {"name": "Event", "value": "worker recovered", "inline": True},
            ],
            "footer": {"text": worker},
        },
    }


def provider_failure_message(provider: str, failures: int) -> dict:
    """A provider poll has kept failing (BACKLOG.6).

    Every value here is a closed internal string or a number. The snapshot's
    own `detail` is written for the admin-scoped settings screen and can name
    an environment variable or quote a `pveum` command carrying the token's
    `user@realm` — the same boundary `/api/v1/panels` draws with its closed
    public strings, held for the same reason: Discord is not the settings
    screen.
    """
    name = sanitize(provider, limit=60)
    return {
        "content": (
            f"🟠 Provider **{name}** polling is failing — "
            f"{failures} consecutive failures. The Settings screen has the reason."
        ),
        "embed": {
            "title": f"🟠 Provider {name} polling is failing",
            "color": COLOR_WARN,
            "fields": [
                {"name": "Event", "value": "provider poll failing", "inline": True},
                {"name": "Consecutive failures", "value": str(failures), "inline": True},
                {
                    "name": "Diagnosis",
                    "value": (
                        "Open Settings → Proxmox for the full reason. Locations "
                        "and panels serve the last good data meanwhile."
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": name},
        },
    }


def provider_recovery_message(provider: str, failures: int) -> dict:
    name = sanitize(provider, limit=60)
    return {
        "content": (
            f"🟢 Provider **{name}** polling recovered after "
            f"{failures} failed poll{'s' if failures != 1 else ''}."
        ),
        "embed": {
            "title": f"🟢 Provider {name} polling recovered",
            "color": COLOR_UP,
            "fields": [
                {"name": "Event", "value": "provider poll recovered", "inline": True},
                {"name": "Failed polls", "value": str(failures), "inline": True},
            ],
            "footer": {"text": name},
        },
    }


def provider_alert_message(provider: str, alert) -> dict:
    """A provider's OWN alerting system raised a critical/high alert (TN2).

    Distinct from `provider_failure_message`, which is about *our* poll failing:
    this forwards the provider's native alert — a TrueNAS `/alert/list` entry — so
    the operator hears about a degrading disk from the box that should have
    flagged it, which is the whole reason for this feature.

    **`alert.message` is provider-authored `formatted` text** naming pools or
    datasets that anyone able to name a dataset on the box could influence, so it
    is treated exactly like a probed page title: `sanitize()`d here (control
    characters removed, markdown escaped, length-capped), and
    `allowed_mentions.parse=[]` in `build_payload` keeps a `@everyone` inside it
    inert. `level` is a closed TrueNAS enum but is sanitised too — defence in
    depth costs nothing and the alternative is trusting the wire.
    """
    name = sanitize(provider, limit=60)
    level = sanitize(alert.level or "ALERT", limit=20)
    text = sanitize(alert.message, limit=MAX_FIELD_VALUE)
    return {
        "content": f"🔴 **{name}** {level} alert: {text}",
        "embed": {
            "title": f"🔴 {name} raised a {level} alert",
            "color": COLOR_DOWN,
            "fields": [
                {"name": "Event", "value": "provider alert", "inline": True},
                {"name": "Level", "value": level, "inline": True},
                {"name": "Alert", "value": text, "inline": False},
            ],
            "footer": {"text": name},
        },
    }


def provider_alert_cleared_message(provider: str, count: int) -> dict:
    """One or more of a provider's native alerts cleared (TN2).

    Edge-triggered like every recovery here: announced once, on the pass a
    previously-notified alert is no longer active. It names a count rather than
    the cleared alert's text — the text is gone from `/alert/list`, and a recovery
    message's job is to say "go look, it is fine now", not to re-quote a message
    that no longer exists. No provider-authored text rides this path.
    """
    name = sanitize(provider, limit=60)
    plural = "s" if count != 1 else ""
    return {
        "content": (
            f"🟢 **{name}** — {count} alert{plural} cleared."
        ),
        "embed": {
            "title": f"🟢 {name} alert{plural} cleared",
            "color": COLOR_UP,
            "fields": [
                {"name": "Event", "value": "provider alert cleared", "inline": True},
                {"name": "Cleared", "value": str(count), "inline": True},
            ],
            "footer": {"text": name},
        },
    }
