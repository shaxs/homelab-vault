"""Django settings for Homelab Vault.

Single env-switched module (spec §5 keeps all deployment config in environment
variables and explicitly rules out a settings screen). PostgreSQL everywhere —
production runs it on local Proxmox storage (spec §3) and development points at
a Postgres LXC over TCP (v1 §2a). There is no SQLite fallback.
"""

import ipaddress
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

env = environ.Env()
# .env is gitignored; .env.example is the committed template.
environ.Env.read_env(REPO_ROOT / ".env")

DEBUG = env.bool("DEBUG", default=True)

# A missing SECRET_KEY otherwise breaks every manage.py/pytest invocation in a
# fresh shell, which reads as an app bug rather than a config gap. Give dev an
# obvious throwaway and refuse it outright once DEBUG is off.
_DEV_SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
SECRET_KEY = env.str("SECRET_KEY", default=_DEV_SECRET_KEY)
if not DEBUG and SECRET_KEY == _DEV_SECRET_KEY:
    raise environ.ImproperlyConfigured(
        "SECRET_KEY must be set to a real value when DEBUG=False."
    )

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    # Required by Django 6's postgres.E005 check because Service.tags is an
    # ArrayField. Registers the array lookups and operations that field needs.
    "django.contrib.postgres",
    "services",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # No CsrfViewMiddleware here on purpose. CSRF is enforced per-request inside
    # services/auth.py, only for the session-cookie branch — bearer requests carry
    # no cookie and must stay exempt or every agent write fails (spec §4c).
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# X_FRAME_OPTIONS only takes effect with the middleware above; without it the
# catalog can be framed, and the write-gate modal with it.
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if not DEBUG:
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ],
        },
    },
]

# --- Database -------------------------------------------------------------
# PostgreSQL, everywhere, with no default (v1 §2a). SQLite existed only to make
# local development easy on a box with no database server; it was never meant
# for production, and a silent fallback is worse than no fallback — a missing
# DATABASE_URL would create a stray db.sqlite3 that starts, serves, and looks
# like a working install right up until the data is somewhere nobody backs up.
#
# Fail fast and name the variable, the same shape as docker-compose's ${VAR:?}
# guards. The message carries no credential: it can only fire when the variable
# is absent, and it never echoes the value.
try:
    DATABASES = {"default": env.db_url("DATABASE_URL")}
except environ.ImproperlyConfigured as exc:
    raise environ.ImproperlyConfigured(
        # ASCII only: this is read off a terminal, and a Windows console in
        # cp1252 turns an em dash into a replacement character.
        "DATABASE_URL must be set, e.g. "
        "postgres://USER:PASSWORD@HOST:5432/DBNAME - SQLite was removed in "
        "v1.0, so there is no local fallback."
    ) from exc
if not DATABASES["default"].get("ENGINE"):
    # An empty DATABASE_URL parses without raising and yields a config with no
    # backend, which then fails much later and much less legibly.
    raise environ.ImproperlyConfigured("DATABASE_URL is set but empty.")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

# --- Static / media -------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Vite build output; absent until `npm run build` has run at least once.
_frontend_dist = REPO_ROOT / "frontend" / "dist"
STATICFILES_DIRS = [_frontend_dist] if _frontend_dist.is_dir() else []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
WHITENOISE_INDEX_FILE = True

MEDIA_URL = "media/"
# Same CWD hazard as STATIC_EXPORT_PATH: the worker caches favicons here and the
# web process serves them, so both must agree on one directory.
_media_root = Path(env.str("MEDIA_ROOT", default=str(REPO_ROOT / "media"))).expanduser()
MEDIA_ROOT = _media_root if _media_root.is_absolute() else (REPO_ROOT / _media_root)

# --- Session / CSRF cookies (spec §4c) ------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_NAME = "hlv_session"
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=not DEBUG)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 24 * 14)

CSRF_COOKIE_SAMESITE = "Strict"
# Must stay readable by JS: the SPA reads it to send back as X-CSRFToken.
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=not DEBUG)

# --- Homelab Vault settings (spec §5) -------------------------------------
HOMELABVAULT_ADMIN_TOKEN = env.str("HOMELABVAULT_ADMIN_TOKEN", default="")
HOMELABVAULT_AGENT_TOKEN = env.str("HOMELABVAULT_AGENT_TOKEN", default="")

# full | open-writes | open (v1 §6c). Deliberately NOT validated here: an
# unrecognised value must fail *closed* to `full` at the point of use rather
# than refuse to start, because "opt in to openness, never out of it" has to
# hold for a typo as well as for a decision. services.auth.current_auth_mode()
# owns that coercion and logs it once.
HOMELABVAULT_AUTH_MODE = env.str("HOMELABVAULT_AUTH_MODE", default="full")

HEALTH_DEFAULT_TIMEOUT_MS = env.int("HEALTH_DEFAULT_TIMEOUT_MS", default=5000)
# These two are the *migration-time* defaults only. Since V1.3 a service with a
# null interval or threshold follows the settings table (`health.default_interval_s`,
# `health.fail_threshold`), which is editable at runtime — these are what stamped
# the rows created before that, and what migration 0003 unstamps against.
HEALTH_DEFAULT_INTERVAL_S = env.int("HEALTH_DEFAULT_INTERVAL_S", default=60)
HEALTH_DEFAULT_FAIL_THRESHOLD = env.int("HEALTH_DEFAULT_FAIL_THRESHOLD", default=3)

# Discord webhook (§5). A credential: it is the *only* thing needed to post to
# the channel, so it lives in the environment and never in the settings table,
# which stores the NAME of this variable instead. Never returned by an API,
# never logged, never written into the recovery export.
HOMELABVAULT_DISCORD_WEBHOOK_URL = env.str(
    "HOMELABVAULT_DISCORD_WEBHOOK_URL", default=""
)

# Security control — deliberately not UI-editable (spec §5). 100.64.0.0/10 is the
# Tailscale CGNAT range; dropping it rejects every tailnet-addressed service.
PROBE_ALLOWED_NETWORKS = [
    ipaddress.ip_network(cidr.strip())
    for cidr in env.list(
        "PROBE_ALLOWED_NETWORKS",
        default=[
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
        ],
    )
    if cidr.strip()
]
# Off by default because homelab services overwhelmingly use self-signed or
# internal-CA certificates, and failing every probe on certificate errors would
# make the feature useless. This is a deliberate, reviewable trade-off rather
# than a hidden `verify=False`: the probe returns metadata only, never the body,
# and the SSRF address policy — not TLS — is what constrains where it can reach.
# Set to True if every probed service has a publicly trusted certificate.
PROBE_VERIFY_TLS = env.bool("PROBE_VERIFY_TLS", default=False)
PROBE_TIMEOUT_S = env.float("PROBE_TIMEOUT_S", default=5.0)
PROBE_MAX_REDIRECTS = env.int("PROBE_MAX_REDIRECTS", default=3)
PROBE_MAX_BYTES = env.int("PROBE_MAX_BYTES", default=256 * 1024)
# Icons over this are refused outright rather than truncated — a partial image
# file is corrupt, not smaller, and it would be base64-inlined into the recovery
# export. Real favicons run large sometimes: OpenObserve ships a 181KB .ico.
PROBE_MAX_ICON_BYTES = env.int("PROBE_MAX_ICON_BYTES", default=512 * 1024)
# SVG icons are kept inline as data URIs rather than written to disk (see
# services/probe.py), so they ride along in every GET /api/v1/services response.
# Capped tighter than a file-backed icon to keep the catalog payload near the
# ~60KB §5 assumes.
ICON_MAX_INLINE_BYTES = env.int("ICON_MAX_INLINE_BYTES", default=32 * 1024)

# --- Proxmox, read-only (v1 §7a) -------------------------------------------
#
# Both are deploy-time configuration and neither can be reached from the
# settings table, which stores only bool/int/enum/env_ref. What IS settings-table
# state is whether polling runs and how often — `proxmox.polling.enabled` and
# `proxmox.poll_interval_s`, consumed by services/providers/proxmox.py.
#
# The scheme is required and is not defaulted in: the PVE API is HTTPS on 8006,
# and a bare `host:port` raises httpx.UnsupportedProtocol on every call. Failing
# on a missing scheme at validation time beats failing on every poll.
PROXMOX_HOST = env.str("PROXMOX_HOST", default="")
# The COMPLETE credential — `PVEAPIToken=USER@REALM!TOKENID=UUID`, exactly what
# goes in the Authorization header. The bare UUID is a 401: Proxmox identifies a
# token by USER@REALM!TOKENID and only authenticates it with the secret, so
# without the identifier the server cannot tell which token it is.
#
# One variable rather than two (an id plus a secret) because `_SECRET_NAME_FRAGMENTS`
# in settings_store.py refuses to display any variable whose name contains TOKEN,
# so a separate PROXMOX_TOKEN_ID could not be shown on the settings screen anyway
# — and splitting it would buy nothing while adding a way to configure half of it.
PROXMOX_API_TOKEN = env.str("PROXMOX_API_TOKEN", default="")
# Named and explicit, exactly like PROBE_VERIFY_TLS above and for the same
# reason: V0 security finding #4 was a hidden `verify=False`, and this is a new
# outbound path to a cluster API. Proxmox ships a self-signed certificate on
# 8006 by default, so verification off is the working default for a homelab —
# but it is a reviewable setting, not an invisible bypass. Set True if the
# cluster presents a certificate your trust store accepts.
PROXMOX_VERIFY_TLS = env.bool("PROXMOX_VERIFY_TLS", default=False)
PROXMOX_TIMEOUT_S = env.float("PROXMOX_TIMEOUT_S", default=15.0)
# Icons above this are NOT base64-inlined into the recovery export (§9). The
# spec's "at ~46 favicons the file stays small" assumes small favicons; real
# ones are not always (LazyLibrarian ships a 370KB .ico). Those services simply
# render without an icon in the export, which stays openable and quick.
STATIC_EXPORT_MAX_ICON_BYTES = env.int(
    "STATIC_EXPORT_MAX_ICON_BYTES", default=96 * 1024
)

def _anchored(raw: str) -> str:
    """Resolve a relative path against the repo root, not the process CWD.

    The web process runs from backend/ and the worker may run from anywhere, so
    a relative STATIC_EXPORT_PATH would otherwise put the recovery file in two
    different places depending on who wrote it last — and the whole point of
    that file is being findable when everything else is down.
    """
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else (REPO_ROOT / path).resolve())


STATIC_EXPORT_PATH = _anchored(
    env.str("STATIC_EXPORT_PATH", default=str(REPO_ROOT / "_export" / "index.html"))
)
STATIC_EXPORT_ENABLED = env.bool("STATIC_EXPORT_ENABLED", default=True)
# Deliberately LOCAL, not next to the export. The export lives on the NAS share,
# and the thing that makes it stale is usually the share being unreachable — a
# marker written there could not be written in exactly the case it exists for,
# and the worker's retry-on-next-cycle would silently never fire.
STATIC_EXPORT_STALE_MARKER = _anchored(
    env.str("STATIC_EXPORT_STALE_MARKER", default=str(BASE_DIR / ".cache" / "export.stale"))
)

# Failed admin-token exchanges allowed per IP per window (spec is silent; without
# a limit the admin token is freely guessable over the tailnet).
SESSION_LOGIN_MAX_ATTEMPTS = env.int("SESSION_LOGIN_MAX_ATTEMPTS", default=10)
SESSION_LOGIN_WINDOW_S = env.int("SESSION_LOGIN_WINDOW_S", default=300)

# How long a presented admin token keeps a session cleared for destructive
# operations (v1 §6b). Five minutes: long enough to delete two tags in a row,
# short enough that a tab left open all afternoon does not still carry it.
ADMIN_REAUTH_WINDOW_S = env.int("ADMIN_REAUTH_WINDOW_S", default=300)

# The admin-token login throttle lives in this cache, so it must be shared across
# gunicorn workers. LocMemCache is per-process: with 3 workers it would silently
# allow 3x the configured attempts. A file-based cache is shared by every worker
# in the container and needs no extra service and no setup step.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": env.str("CACHE_DIR", default=str(BASE_DIR / ".cache")),
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {name}: {message}", "style": "{"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # **httpx logs the full request URL at INFO, and one of the URLs this app
        # requests IS a credential**: a Discord webhook's secret lives in its
        # path, so every notification wrote
        # `POST https://discord.com/api/webhooks/<id>/<token>` into the console —
        # and in Docker the console is `docker logs`, plus whatever ships it
        # onward. Anyone with log access could post to the channel.
        #
        # V1.3's secrets-management gate required that the webhook is never
        # logged. It verified *our* failure path never echoes it and missed that
        # the HTTP client announces every request on its own. Found 2026-08-07
        # while testing why the deadman had not alerted.
        #
        # WARNING rather than removing the handler: a genuine httpx problem
        # should still surface. Nothing is lost by muting INFO — every outbound
        # path already logs its own outcome (`provider proxmox cached N guests`,
        # the probe's rejections, the notifier's failures), and those lines are
        # written by us and carry no credentials.
        "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "httpcore": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
