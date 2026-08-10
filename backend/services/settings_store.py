"""Runtime preferences — v1 §4 (`settings`) and §6a.

The first app-level state in this project that is not service data, which §11.2
names as new attack surface in an app whose reads are unauthenticated. Three
structural decisions carry that weight, in order of how much they matter:

1. **There is no key that can hold free text.** Every key is bool, int, enum or
   `env_ref`, and `env_ref` accepts only an environment-variable *name*. §4 says
   values that are credentials are stored as the name of an environment variable
   and resolved at use time; this is what makes that a property of the schema
   rather than a rule someone has to remember. A webhook URL cannot be stored
   here because there is nowhere to put it.
2. **The public whitelist is a separate frozenset**, not a flag on the registry.
   §11.2 asks us to verify the whitelist is a whitelist and not a blacklist; a
   `public=True` field would make "leaks by default" one careless keystroke
   away, whereas a key added to the registry is invisible to the public endpoint
   unless somebody names it in a second place.
3. **Deploy-time configuration is not here at all** (§6a). `PROBE_ALLOWED_NETWORKS`
   above all: it is a security control and must not be editable by anyone
   holding a session. Unknown keys are refused, so there is no key path to it.

Each spec names the chunk that *consumes* it. That is standing rule 2 written
into the code: a settings screen that saves and reads back while no worker ever
reads the table is a fully green chunk with an inert screen.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from .models import Setting

# --- Kinds -----------------------------------------------------------------

KIND_BOOL = "bool"
KIND_INT = "int"
KIND_ENUM = "enum"
KIND_ENV_REF = "env_ref"
KIND_DISCORD_MENTION = "discord_mention"

#: An environment variable NAME. Deliberately narrow: the realistic mistake is
#: an operator pasting the webhook URL itself into the field, and a URL fails on
#: the scheme, the lowercase letters and the punctuation. This is a shape check
#: — the actual guarantee that no secret lands in this table is that no key has
#: a free-text kind.
ENV_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")

#: A Discord snowflake ID, or empty for "mention nobody". A shape check like
#: `env_ref`, NOT a free-text kind: the value space is seventeen-to-twenty
#: digits, so neither a name to be parsed nor a credential can live here. IDs
#: are what keep BACKLOG.1 an allowlist — the payload names this exact ID and
#: never switches mention *parsing* on, which is what keeps a service whose
#: probed title says `@everyone` inert.
DISCORD_MENTION_RE = re.compile(r"^\d{17,20}$")


class SettingRejected(Exception):
    """A settings write that must come back as a 422 naming the key."""


def _bounded(key: Any, limit: int = 48) -> str:
    """Caller-supplied text on its way into an error message."""
    text = str(key)
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str
    default: Any
    label: str
    helper: str
    section: str
    #: Which build chunk actually reads this value. Empty means "this chunk".
    consumer: str = ""
    unit: str | None = None
    minimum: int | None = None
    step: int | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)


# --- The registry ----------------------------------------------------------
#
# Sections and copy follow `design/HomeLab OS Settings v1.html`, reconciled
# against the spec on 2026-08-04. Theme is deliberately absent: it is a
# per-device preference in localStorage (AS_BUILT §3 row 6), which the mockup
# agrees with and §6a's table does not.

REGISTRY: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="ui.landing_page",
        kind=KIND_ENUM,
        default="catalog",
        choices=("catalog", "dashboard"),
        label="Landing page",
        helper=(
            "The Service Catalog stays reachable at /catalog regardless of this "
            "setting."
        ),
        section="Appearance",
        consumer="V1.5",
    ),
    SettingSpec(
        key="ui.catalog.density",
        kind=KIND_ENUM,
        default="compact",
        choices=("compact", "comfortable"),
        label="Catalog density",
        helper=(
            "Row height, icon and text size in the service catalog. Compact is "
            "the original dense table; Comfortable is larger and easier to scan."
        ),
        section="Appearance",
        consumer="BACKLOG.5",
    ),
    SettingSpec(
        key="ui.panels.status_summary",
        kind=KIND_BOOL,
        default=True,
        label="Status Summary",
        helper="Counts of up, down, stale and unchecked services.",
        section="Panels",
        consumer="V1.5",
    ),
    SettingSpec(
        key="ui.panels.proxmox_cluster",
        kind=KIND_BOOL,
        default=True,
        label="Proxmox Cluster Summary",
        helper="Quorum, compute and storage totals for the cluster.",
        section="Panels",
        consumer="V1.5",
    ),
    SettingSpec(
        key="ui.panels.proxmox_nodes",
        kind=KIND_BOOL,
        default=True,
        label="Proxmox Nodes",
        helper="Per-node status, load and storage table.",
        section="Panels",
        consumer="V1.5",
    ),
    SettingSpec(
        key="health.default_interval_s",
        kind=KIND_INT,
        default=60,
        minimum=15,
        step=5,
        unit="seconds",
        label="Poll interval",
        helper=(
            "How often the worker probes each health_url. Applies to every "
            "service that has not set its own interval. Default 60, minimum 15."
        ),
        section="Health checks",
        consumer="V1.3",
    ),
    SettingSpec(
        key="health.fail_threshold",
        kind=KIND_INT,
        default=3,
        minimum=1,
        unit="failures",
        label="Fail threshold",
        helper="Consecutive failed probes before a service is marked down.",
        section="Health checks",
        consumer="V1.3",
    ),
    SettingSpec(
        key="health.recovery_threshold",
        kind=KIND_INT,
        default=2,
        minimum=1,
        unit="successes",
        label="Recovery threshold",
        helper="Consecutive successful probes before a service returns to up.",
        section="Health checks",
        consumer="V1.3",
    ),
    SettingSpec(
        key="notifications.discord.enabled",
        kind=KIND_BOOL,
        default=False,
        label="Discord notifications",
        helper="Send state changes to the configured Discord channel.",
        section="Notifications",
        consumer="V1.3",
    ),
    SettingSpec(
        key="notifications.discord.webhook_env",
        kind=KIND_ENV_REF,
        default="HOMELABVAULT_DISCORD_WEBHOOK_URL",
        label="Webhook variable",
        helper=(
            "The NAME of the environment variable holding the webhook URL. The "
            "URL itself is never stored here, displayed here, or returned by the "
            "API."
        ),
        section="Notifications",
        consumer="V1.3",
    ),
    SettingSpec(
        key="notifications.discord.mention_user",
        kind=KIND_DISCORD_MENTION,
        default="",
        label="Mention user ID",
        helper=(
            "Discord USER ID to mention on every notification — for an agent "
            "or a person who should be pinged. Empty mentions nobody. IDs only, "
            "never names: text in a message body can never trigger a ping."
        ),
        section="Notifications",
        consumer="BACKLOG.1",
    ),
    SettingSpec(
        key="notifications.discord.mention_role",
        kind=KIND_DISCORD_MENTION,
        default="",
        label="Mention role ID",
        helper=(
            "Discord ROLE ID to mention on every notification. Empty mentions "
            "no role. Right-click a role in Discord (developer mode) to copy "
            "its ID."
        ),
        section="Notifications",
        consumer="BACKLOG.1",
    ),
    SettingSpec(
        key="proxmox.polling.enabled",
        kind=KIND_BOOL,
        default=False,
        label="Proxmox polling",
        helper="Poll the cluster for node, guest and storage state.",
        section="Proxmox",
        consumer="V1.4",
    ),
    SettingSpec(
        key="proxmox.poll_interval_s",
        kind=KIND_INT,
        default=120,
        minimum=30,
        step=30,
        unit="seconds",
        label="Proxmox poll interval",
        helper=(
            "Separate from the service health interval — the cluster API is "
            "heavier. Minimum 30."
        ),
        section="Proxmox",
        consumer="V1.4",
    ),
)

SPECS: dict[str, SettingSpec] = {spec.key: spec for spec in REGISTRY}

#: Keys an ANONYMOUS caller may read (§6a). Written out by hand, on purpose —
#: see decision 2 in the module docstring. Everything else in the table
#: describes infrastructure and belongs behind the admin scope.
PUBLIC_SETTING_KEYS = frozenset(
    {
        "ui.landing_page",
        "ui.catalog.density",
        "ui.panels.status_summary",
        "ui.panels.proxmox_cluster",
        "ui.panels.proxmox_nodes",
    }
)

#: Order the settings screen renders its cards in.
SECTIONS = ("Appearance", "Panels", "Health checks", "Notifications", "Proxmox")


# --- Validation ------------------------------------------------------------


def coerce(spec: SettingSpec, value: Any) -> Any:
    """Validate one value against its spec, or raise `SettingRejected`.

    Strict about types rather than helpful: a settings write arrives as JSON
    from a screen that knows the schema, so `"true"` for a boolean is a client
    bug worth surfacing, not an input to be rescued.
    """
    if spec.kind == KIND_BOOL:
        if not isinstance(value, bool):
            raise SettingRejected(f"{spec.key} takes true or false.")
        return value

    if spec.kind == KIND_INT:
        # bool is a subclass of int, so `True` would otherwise store as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingRejected(f"{spec.key} takes a whole number.")
        if spec.minimum is not None and value < spec.minimum:
            # The floor lives here, on the write path, and not only in the UI
            # stepper (§6a, added 2026-08-03): the stepper is a convenience, the
            # API is the boundary.
            raise SettingRejected(
                f"{spec.key} has a minimum of {spec.minimum}"
                f"{' ' + spec.unit if spec.unit else ''}."
            )
        return value

    if spec.kind == KIND_ENUM:
        if value not in spec.choices:
            raise SettingRejected(
                f"{spec.key} takes one of: {', '.join(spec.choices)}."
            )
        return value

    if spec.kind == KIND_ENV_REF:
        if not isinstance(value, str) or not ENV_REF_RE.match(value):
            raise SettingRejected(
                f"{spec.key} takes the NAME of an environment variable "
                "(A-Z, digits and underscores), never a value. Secrets are "
                "never stored in the settings table."
            )
        return value

    if spec.kind == KIND_DISCORD_MENTION:
        if not isinstance(value, str) or (
            value != "" and not DISCORD_MENTION_RE.match(value)
        ):
            raise SettingRejected(
                f"{spec.key} takes a Discord ID (17-20 digits) or nothing. "
                "Names are never accepted — an ID cannot be forged by text in "
                "a message."
            )
        return value

    raise SettingRejected(f"{spec.key} has an unsupported type.")  # pragma: no cover


# --- Reading ---------------------------------------------------------------


def _stored() -> dict[str, Any]:
    return {row.key: row.value for row in Setting.objects.all()}


def get_setting(key: str) -> Any:
    """One value, falling back to the registry default.

    Read live on every call rather than cached at import: a value snapshotted at
    process start satisfies "the worker reads the setting" and is still wrong,
    because changing it then does nothing until a restart.
    """
    spec = SPECS[key]
    row = Setting.objects.filter(key=key).first()
    return row.value if row is not None else spec.default


def all_settings() -> dict[str, Any]:
    """Every registry key with its effective value."""
    stored = _stored()
    return {spec.key: stored.get(spec.key, spec.default) for spec in REGISTRY}


def public_settings() -> dict[str, Any]:
    """Only the display keys an anonymous viewer needs to draw a page (§6a)."""
    values = all_settings()
    return {key: values[key] for key in sorted(PUBLIC_SETTING_KEYS)}


# --- Writing ---------------------------------------------------------------


@transaction.atomic
def write_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate every key, then write them all, or write none.

    Validate-all-first because the screen saves a batch: a partial write leaves
    the operator looking at a form where some rows took and some did not, with
    no indication of which.
    """
    if not isinstance(payload, dict) or not payload:
        raise SettingRejected("Send at least one setting to change.")

    validated: dict[str, Any] = {}
    for key, value in payload.items():
        spec = SPECS.get(key)
        if spec is None:
            # Covers both a typo and a deliberate attempt at a key that is not
            # ours — `PROBE_ALLOWED_NETWORKS`, `SECRET_KEY`, a token. Deploy-time
            # configuration and security controls live in .env (§6a) and there is
            # no key path to them from here.
            #
            # The key is named because §4c requires a 422 that names the
            # offending field, and bounded because that message is caller-
            # supplied text a person may paste elsewhere. Bounding is not the
            # same as sanitising: no rule separates a mistyped key from a
            # credential typed into the key position, and the realistic paste
            # target — a *value* — is never echoed at all.
            raise SettingRejected(f"{_bounded(key)} is not a settings key.")
        validated[key] = coerce(spec, value)

    for key, value in validated.items():
        Setting.objects.update_or_create(key=key, defaults={"value": value})
    return all_settings()


# --- The .env panel --------------------------------------------------------
#
# §6a's "what lives where" made visible: the settings screen shows the
# deploy-time controls it does NOT own, with the variable to edit and no
# affordance. A fixed list, never an iteration over os.environ — and secrets
# report presence only, which is enforced by never reading their value at all
# rather than by remembering to redact it.

_SHOW_VALUE = "value"
_SHOW_PRESENCE = "presence"


@dataclass(frozen=True)
class EnvControl:
    label: str
    env_var: str
    disclosure: str
    note: str


ENV_CONTROLS: tuple[EnvControl, ...] = (
    EnvControl(
        label="Auth mode",
        env_var="HOMELABVAULT_AUTH_MODE",
        disclosure=_SHOW_VALUE,
        note=(
            "Changing the mode means editing .env and restarting the server — it "
            "cannot be changed from the UI at any privilege level."
        ),
    ),
    EnvControl(
        label="Session lifetime",
        env_var="SESSION_COOKIE_AGE",
        disclosure=_SHOW_VALUE,
        note="How long a browser stays authenticated after a successful token exchange.",
    ),
    EnvControl(
        label="Admin token",
        env_var="HOMELABVAULT_ADMIN_TOKEN",
        disclosure=_SHOW_PRESENCE,
        note=(
            "Grants writes, deletes, bulk import and access to this settings "
            "screen. Rotating it signs out every active browser session — every "
            "operator has to re-enter the new token."
        ),
    ),
    EnvControl(
        label="Agent token",
        env_var="HOMELABVAULT_AGENT_TOKEN",
        disclosure=_SHOW_PRESENCE,
        note="Used by the health worker and any external agent posting check results.",
    ),
    EnvControl(
        label="Probe allowed networks",
        env_var="PROBE_ALLOWED_NETWORKS",
        disclosure=_SHOW_VALUE,
        note=(
            "The probe endpoint refuses any target outside these ranges, in every "
            "auth mode."
        ),
    ),
    EnvControl(
        label="Discord webhook",
        env_var="HOMELABVAULT_DISCORD_WEBHOOK_URL",
        disclosure=_SHOW_PRESENCE,
        note=(
            "The URL is never displayed here or returned by the API. Rotate it by "
            "editing .env and restarting the server."
        ),
    ),
    EnvControl(
        label="Proxmox host",
        env_var="PROXMOX_HOST",
        disclosure=_SHOW_VALUE,
        note=(
            "The cluster API endpoint. Changing it means editing .env and "
            "restarting the server."
        ),
    ),
    EnvControl(
        label="Proxmox API token",
        env_var="PROXMOX_API_TOKEN",
        disclosure=_SHOW_PRESENCE,
        note=(
            "Bind the token to a PVEAuditor role. Homelab Vault only ever reads "
            "from Proxmox and needs no write access."
        ),
    ),
)


#: Name fragments that mean "this variable holds a credential". An entry whose
#: variable matches one of these may never be declared `_SHOW_VALUE`.
#:
#: The module docstring claims a secret's value is never read on the display
#: path — but that was only true because every current entry happens to be
#: declared correctly, which is a fact about today's list rather than a property
#: of the code. The check below makes it a property: adding
#: `PROXMOX_API_TOKEN` with the wrong disclosure mode fails at import, not in
#: production.
_SECRET_NAME_FRAGMENTS = ("TOKEN", "SECRET", "PASSWORD", "WEBHOOK", "_KEY", "CREDENTIAL")


def _assert_no_secret_is_displayed() -> None:
    offenders = [
        control.env_var
        for control in ENV_CONTROLS
        if control.disclosure == _SHOW_VALUE
        and any(frag in control.env_var.upper() for frag in _SECRET_NAME_FRAGMENTS)
    ]
    if offenders:
        raise ImproperlyConfigured(
            "These settings-screen entries would display a credential: "
            f"{', '.join(offenders)}. Declare them presence-only."
        )


def _display_value(env_var: str) -> str:
    raw = getattr(django_settings, env_var, None)
    if env_var == "SESSION_COOKIE_AGE" and isinstance(raw, int):
        return f"{raw // 86400} days"
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(item) for item in raw)
    return str(raw or "")


def env_panel() -> list[dict[str, Any]]:
    """The read-only `.env` rows for the settings screen.

    A secret's *value* is never read on this path — `_display_value` is called
    only for entries declared `_SHOW_VALUE`, and `_assert_no_secret_is_displayed`
    refuses to let a credential-named variable be declared that way at all.
    """
    _assert_no_secret_is_displayed()
    rows = []
    for control in ENV_CONTROLS:
        if control.disclosure == _SHOW_VALUE:
            value = _display_value(control.env_var)
            status = value or "Not configured"
        else:
            status = (
                "Configured"
                if getattr(django_settings, control.env_var, None)
                else "Not configured"
            )
        rows.append(
            {
                "label": control.label,
                "env_var": control.env_var,
                "status": status,
                "shows_value": control.disclosure == _SHOW_VALUE,
                "note": control.note,
            }
        )
    return rows
