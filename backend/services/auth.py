"""Authentication and scopes — spec §4c.

Two roles, tokens in environment variables and never the database:

    agent    GET, POST, PATCH          — scripts and future assistants
    admin    all of the above, DELETE  — the UI

Reads are unauthenticated by design (§4c): the catalog must open as a browser
home page with no interstitial.

CSRF is the load-bearing control here. `CsrfViewMiddleware` is absent from
MIDDLEWARE, because applying it globally would also cover bearer requests and
break every agent write. Instead CSRF is enforced *inside* the session branch
below — the only branch that authenticates from a cookie, and therefore the only
one with CSRF exposure. If that branch is ever bypassable, cookie-auth writes
have no CSRF protection at all.

The check itself is `ninja.utils.check_csrf`, not a hand-rolled
`CsrfViewMiddleware` call: it runs `process_request` first, which loads the CSRF
secret from the cookie. Skipping that step makes the check fail open in the
"no cookie" case rather than rejecting.

Sessions additionally carry a *fingerprint* of the admin token they were minted
from (v1 §6b), so rotating `HOMELABVAULT_ADMIN_TOKEN` invalidates them.
"""

import hashlib
import hmac
import logging
import time

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core.cache import cache
from ninja.errors import HttpError
from ninja.security import HttpBearer
from ninja.utils import check_csrf

logger = logging.getLogger(__name__)

SCOPE_AGENT = "agent"
SCOPE_ADMIN = "admin"

# --- Auth modes (v1 §6c) ---------------------------------------------------
# An env var, never a setting: a control governing whether sessions matter
# cannot live where a session can reach it, and making this file depend on a
# database read that it itself gates unravels from a single bug. Flipping the
# mode requires shell access, which is the right bar.
AUTH_MODE_FULL = "full"
AUTH_MODE_OPEN_WRITES = "open-writes"
AUTH_MODE_OPEN = "open"
AUTH_MODES = (AUTH_MODE_FULL, AUTH_MODE_OPEN_WRITES, AUTH_MODE_OPEN)

#: Scope an unauthenticated request is granted in each mode. `full` grants none.
#: `open-writes` grants agent, so admin-scoped operations — every destructive
#: one — still demand a token. `open` grants admin.
_OPEN_MODE_SCOPES = {
    AUTH_MODE_OPEN_WRITES: SCOPE_AGENT,
    AUTH_MODE_OPEN: SCOPE_ADMIN,
}

SESSION_SCOPE_KEY = "hlv_scope"
#: Fingerprint of the admin token this session was minted from (§6b).
SESSION_TOKEN_FP_KEY = "hlv_token_fp"
#: When the admin token was last presented, for the destructive-op re-prompt.
SESSION_REAUTH_AT_KEY = "hlv_reauth_at"

# The single local account the admin session attaches to. There is no login
# screen and no user management in v0 (§5); Django's login() just needs a user
# to bind the session to.
ADMIN_USERNAME = "homelab-vault-admin"


def _token_matches(candidate: str, configured: str) -> bool:
    """Constant-time compare. An unset token never matches, so a deployment that
    forgot to configure HOMELABVAULT_AGENT_TOKEN does not accept the empty
    string as valid."""
    if not configured or not candidate:
        return False
    try:
        return hmac.compare_digest(candidate, configured)
    except TypeError:
        # compare_digest refuses str arguments containing non-ASCII. A token
        # with an accented character would otherwise raise on every single
        # authentication attempt — a 500, and a stack trace under DEBUG, where
        # the honest answer is simply "that is not the token".
        logger.warning(
            "Token comparison received non-ASCII input; treating as no match. "
            "Tokens must be ASCII."
        )
        return False


_warned_auth_modes: set[str] = set()


def current_auth_mode() -> str:
    """The active auth mode, **failing closed** to `full` (§6c).

    Unset, misspelled, wrong case, or outright garbage all resolve to `full`.
    §6c's principle is "opt in to openness, never out of it", and this is the
    most consequential environment variable in the project: a typo in the
    *variable name* must never silently open the app.

    Read live rather than captured at import, so the settings screen reports
    what is actually in force and a test override behaves like a real restart.

    A value that was set but not understood is logged once — silent coercion is
    correct behaviour and a terrible operator experience.
    """
    raw = getattr(settings, "HOMELABVAULT_AUTH_MODE", "") or ""
    mode = raw.strip().lower()
    if mode in AUTH_MODES:
        return mode
    if mode and mode not in _warned_auth_modes:
        _warned_auth_modes.add(mode)
        # Deliberately does NOT echo the value. The legitimate values are three
        # known words, so printing the garbage would add little — and the
        # realistic way this variable ends up holding something unexpected is an
        # operator pasting a token onto the wrong line of `.env`, which would
        # then be sitting in the log in cleartext.
        logger.warning(
            "HOMELABVAULT_AUTH_MODE is set to an unrecognised value; falling "
            "back to %r. Valid values: %s.",
            AUTH_MODE_FULL,
            ", ".join(AUTH_MODES),
        )
    return AUTH_MODE_FULL


def scope_for_token(token: str) -> str | None:
    """Map a bearer token to its scope. Admin is checked first so that setting
    both env vars to the same value grants the higher scope rather than the
    lower one."""
    if _token_matches(token, settings.HOMELABVAULT_ADMIN_TOKEN):
        return SCOPE_ADMIN
    if _token_matches(token, settings.HOMELABVAULT_AGENT_TOKEN):
        return SCOPE_AGENT
    return None


def _enforce_csrf(request) -> None:
    """Run Django's CSRF check for a request that authenticated via cookie."""
    if check_csrf(request) is not None:
        raise HttpError(403, "CSRF verification failed.")


# --- Session binding to the admin token (§6b) ------------------------------


def admin_token_fingerprint(token: str) -> str:
    """A session-storable stand-in for the admin token.

    Never the token itself: session rows are database rows, and a leaked one
    must not leak `HOMELABVAULT_ADMIN_TOKEN`. Keyed on `SECRET_KEY` so the
    fingerprint is useless in another deployment, and truncated because 128 bits
    is far past the point where a collision matters for an equality check.

    Returns "" for an unset token, which `_session_token_is_current` treats as
    "never matches" rather than "matches every empty fingerprint".
    """
    if not token:
        return ""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _session_token_is_current(request) -> bool:
    """Is this session still bound to the configured admin token?

    Rotating the token must invalidate live browser sessions (§6b): the previous
    behaviour locked out Hermes while leaving every browser signed in, which is
    backwards for a credential rotation.

    Fails closed in both directions — a session predating this mechanism has no
    stored fingerprint and is refused, and an unset admin token cannot match
    anything.
    """
    stored = request.session.get(SESSION_TOKEN_FP_KEY) or ""
    current = admin_token_fingerprint(settings.HOMELABVAULT_ADMIN_TOKEN)
    if not stored or not current:
        return False
    # Secret-dependent comparison in the single load-bearing security control.
    return hmac.compare_digest(stored, current)


class ScopedAuth(HttpBearer):
    """Bearer-first, session-fallback authentication.

    Returns the granted scope string (truthy) or None. Ninja stores the return
    value on `request.auth`.
    """

    #: Scopes accepted by this instance.
    allowed_scopes: tuple[str, ...] = (SCOPE_AGENT, SCOPE_ADMIN)
    #: Whether `HOMELAB_AUTH_MODE` can grant this operation without a token.
    #: False makes an endpoint admin-only in *every* mode, structurally rather
    #: than by a conditional somebody has to remember to write (§6c).
    honors_auth_mode: bool = True

    def __init__(self, allowed_scopes=None, honors_auth_mode=None):
        super().__init__()
        if allowed_scopes is not None:
            self.allowed_scopes = tuple(allowed_scopes)
        if honors_auth_mode is not None:
            self.honors_auth_mode = honors_auth_mode

    def __call__(self, request):
        # Ninja's HttpBearer.__call__ only invokes authenticate() when a
        # well-formed `Authorization: Bearer` header is present; otherwise it
        # returns None. A malformed or invalid bearer header must NOT fall
        # through to the session branch, or an attacker could skip the CSRF
        # check by attaching a junk bearer header to a cookie-authenticated
        # request. So: header present => bearer decides, full stop.
        if request.headers.get("Authorization"):
            return super().__call__(request)
        scope = self._authenticate_session(request)
        if scope is not None:
            return scope
        return self._authenticate_open_mode(request)

    def _authenticate_open_mode(self, request):
        """Grant an unauthenticated request whatever the mode allows (§6c).

        Last, deliberately: a real credential always decides first, so a session
        keeps its own scope and its CSRF obligation rather than being quietly
        upgraded by the mode.
        """
        if not self.honors_auth_mode:
            return None
        granted = _OPEN_MODE_SCOPES.get(current_auth_mode())
        if granted is None or granted not in self.allowed_scopes:
            return None
        # Marks the request as authorised by policy rather than by credential,
        # which `actor_name` records and the delete re-prompt reads.
        request.hlv_open_mode = True
        return granted

    def authenticate(self, request, token):
        scope = scope_for_token(token)
        if scope is None:
            return None
        return scope if scope in self.allowed_scopes else None

    def _authenticate_session(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        scope = request.session.get(SESSION_SCOPE_KEY)
        if scope not in self.allowed_scopes:
            return None
        if not _session_token_is_current(request):
            # Rotation INVALIDATES; it does not erase (§6b). Django cannot reach
            # into the store to flush a session it is not currently serving, so
            # untouched sessions for a retired token sit in the table until
            # SESSION_COOKIE_AGE expires them — unable to authenticate, but
            # present. This is the one moment we *are* serving it, so flush.
            request.session.flush()
            return None
        # Cookie-authenticated => CSRF applies (§4c).
        _enforce_csrf(request)
        return scope


agent_auth = ScopedAuth(allowed_scopes=(SCOPE_AGENT, SCOPE_ADMIN))
admin_auth = ScopedAuth(allowed_scopes=(SCOPE_ADMIN,))
#: Admin-only in EVERY mode, no exceptions (§6c). Two groups:
#:
#: * **Probe and icon**, named by §6c. They make the server fetch a URL of the
#:   caller's choosing, so opening them opens SSRF to anyone who can reach the
#:   box — the actual cost of an open mode, and the one part never worth paying.
#: * **The settings endpoints**, added 2026-08-05 by this chunk's `api-security`
#:   gate. §6c's carve-out list was written before those endpoints existed, and
#:   §6a's reason for admin-scoping them — "the table describes infrastructure …
#:   none of that belongs in an unauthenticated response" — is about the nature
#:   of the data, not about a mode. Left alone, `open` would publish the .env
#:   panel and let anyone on the network switch notifications off, silently
#:   disabling alerting. §6c argues the cost of an open mode is catalog
#:   vandalism; turning off a monitoring control is a different thing, and the
#:   narrow public endpoint already exposes everything an anonymous *page* needs.
admin_only_auth = ScopedAuth(allowed_scopes=(SCOPE_ADMIN,), honors_auth_mode=False)


def request_is_admin(request) -> bool:
    """Is this request admin-authenticated? For *detail gating*, not access.

    `/api/v1/healthz` is public by design and shows more to an admin (§5a's
    thresholds, which §6a keeps out of the public settings response). This is the
    one place a route decides what to *include* rather than whether to answer, so
    it is written once, here, next to the auth it consults — and it uses
    `admin_only_auth`, which ignores the auth mode, so an open deployment does
    not silently promote every anonymous caller to the detailed view.

    Safe on a read: `ScopedAuth` returns None rather than raising for a bad or
    absent credential, and the CSRF check inside the session branch exempts safe
    methods. Do not reach for this to protect anything — an endpoint that must
    refuse anonymous callers declares `auth=admin_only_auth` and lets Ninja do it.
    """
    return admin_only_auth(request) == SCOPE_ADMIN


def actor_name(request) -> str:
    """Value stamped into `created_by` on API writes (§4c). Never the token."""
    scope = getattr(request, "auth", None) or "unknown"
    if getattr(request, "hlv_open_mode", False):
        # Not "token:agent": nobody presented a credential. A catalog whose
        # provenance says otherwise is lying about how a record got there.
        return f"open:{scope}"
    if request.headers.get("Authorization"):
        return f"token:{scope}"
    return f"session:{scope}"


# --- Admin token exchange -------------------------------------------------


def _throttle_key(request) -> str:
    ip = request.META.get("REMOTE_ADDR", "?")
    return f"hlv:session-attempts:{ip}"


def check_login_throttle(request) -> None:
    key = _throttle_key(request)
    if cache.get(key, 0) >= settings.SESSION_LOGIN_MAX_ATTEMPTS:
        raise HttpError(429, "Too many attempts. Try again later.")


def record_failed_login(request) -> None:
    key = _throttle_key(request)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=settings.SESSION_LOGIN_WINDOW_S)


def clear_login_throttle(request) -> None:
    cache.delete(_throttle_key(request))


def start_admin_session(request) -> None:
    """Attach an admin-scoped Django session to the request (§3b: `POST
    /api/v1/session` validates the admin token and calls `login()`)."""
    user_model = get_user_model()
    user, _ = user_model.objects.get_or_create(
        username=ADMIN_USERNAME, defaults={"is_active": True}
    )
    if user.has_usable_password():
        # The token is the credential; this account must not be password-usable.
        user.set_unusable_password()
        user.save(update_fields=["password"])
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.cycle_key()
    request.session[SESSION_SCOPE_KEY] = SCOPE_ADMIN
    # Bind the session to the token it was minted from, so rotating the token
    # invalidates it (§6b). `login()` already stamps Django's own
    # `_auth_user_hash`, but `set_unusable_password()` above binds that to a
    # random string rather than to the token — the app opted out of the
    # mechanism it should have used, so this is the substitute.
    request.session[SESSION_TOKEN_FP_KEY] = admin_token_fingerprint(
        settings.HOMELABVAULT_ADMIN_TOKEN
    )
    mark_reauthenticated(request)


# --- Re-prompt on destructive operations (§6b) -----------------------------


class ReauthRequired(Exception):
    """The admin token must be re-presented before this operation.

    A distinct exception rather than `HttpError(403)` so the response can carry
    a machine-readable `code`: the SPA already opens its write gate on any 403,
    and without the code it cannot tell "you are not signed in" from "confirm
    this deletion", which need different words.
    """


def mark_reauthenticated(request) -> None:
    """Record that the admin token was presented just now."""
    request.session[SESSION_REAUTH_AT_KEY] = time.time()


def require_recent_admin_auth(request) -> None:
    """Gate a destructive operation on a recent token presentation (§6b).

    Called explicitly at the top of the four DELETE handlers rather than applied
    as a decorator: django-ninja builds each operation's signature by
    introspection, and a wrapper is one more thing that has to get that right
    for a control whose failure mode is silent.

    Two deliberate exemptions:

    * **Bearer requests.** The credential is on the request itself, so it is
      already being re-presented on every call. Gating them would break Hermes
      and every script for no gain.
    * **Open auth modes.** Under `open` the operator has declared these
      endpoints need no token at all (§6c); demanding one here would contradict
      the mode rather than harden it. `open-writes` still routes destructive
      operations through admin, so the gate applies there as normal.
    """
    if request.headers.get("Authorization"):
        return
    if getattr(request, "hlv_open_mode", False):
        return
    stamped = request.session.get(SESSION_REAUTH_AT_KEY)
    if stamped is not None:
        age = time.time() - float(stamped)
        # A clock that jumped backwards must not grant an unbounded window.
        if 0 <= age <= settings.ADMIN_REAUTH_WINDOW_S:
            return
    minutes = max(1, settings.ADMIN_REAUTH_WINDOW_S // 60)
    raise ReauthRequired(
        "Re-enter the admin token to confirm this deletion. It was last "
        f"entered more than {minutes} minute(s) ago."
    )
