"""URL probing and SSRF hardening — spec §4c.

`/api/v1/probe` fetches arbitrary user-supplied URLs from inside the network, which
makes it an SSRF endpoint by construction. Favicon retrieval is a *second*
server-side fetch of an attacker-influenced URL, so it runs through the same
validator.

Address policy (the part that is easy to get subtly wrong):

  1. Hard-deny loopback, link-local, multicast, unspecified and reserved
     addresses. Link-local catches the cloud metadata address 169.254.169.254;
     it is also listed explicitly for readability and for IPv6 fd00:ec2::254.
  2. Then allow only if the address is inside PROBE_ALLOWED_NETWORKS *or* is
     globally routable.

Note what step 2 does NOT do: branch on `is_private`. CPython has reclassified
ranges between versions — on Python 3.14, 100.64.0.0/10 (the Tailscale CGNAT
range that spec §5 ships in the default allowlist) reports `is_private=False`
AND `is_global=False`, landing in neither bucket. A policy shaped like
"if private -> check allowlist, elif global -> allow, else deny" therefore
rejects every tailnet-addressed service in the lab, and would do so only on
some Python versions. Falling through to the allowlist is what makes the CGNAT
range work, and makes the policy version-independent.

RESIDUAL RISK, accepted for v0 and documented rather than hidden: DNS rebinding.
A hostname is resolved and validated, then httpx resolves it again when
connecting, so a TOCTOU window exists. Pinning the socket to the validated
address breaks SNI and name-based virtual hosting, which most homelab services
behind a reverse proxy depend on. Mitigations in place: this endpoint is admin
scope only, every redirect hop is re-validated, responses are size-capped, and
only extracted metadata is ever returned.
"""

import base64
import hashlib
import ipaddress
import logging
import re
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cloud metadata endpoints. Both are already covered by the link-local and
# unique-local denials; listed explicitly so the intent survives a refactor.
METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# Tag matchers that tolerate ">" and quotes INSIDE attribute values. An inline
# SVG favicon looks like:
#     <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://…'/>">
# Naive `[^>]*>` truncates at the ">" inside the data URI, and a naive
# `["']([^"']+)["']` truncates the value at the first inner quote — which
# silently stores a fragment of a data URI as the icon.
_META_RE = re.compile(r"""<meta\b((?:[^>"']|"[^"]*"|'[^']*')*)/?>""", re.IGNORECASE)
_LINK_RE = re.compile(r"""<link\b((?:[^>"']|"[^"]*"|'[^']*')*)/?>""", re.IGNORECASE)
#: Honours the opening quote, so the value may contain the other quote char.
_ATTR_RE = re.compile(
    r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
)


def _attrs(raw: str) -> dict[str, str]:
    found = {}
    for match in _ATTR_RE.finditer(raw):
        key = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        found.setdefault(key, value)
    return found

# Metadata an app publishes to say what it is, most authoritative first. These
# matter when the probe lands on a login screen, where the title describes the
# page rather than the service.
_SITE_NAME_KEYS = (
    "og:site_name",
    "application-name",
    "apple-mobile-web-app-title",
    "og:title",
)
# SVG is supported, but is NEVER written under MEDIA_ROOT — see _store_icon.
# An SVG served from our own origin is an active document, so navigating
# straight to a stored one would run its script in our origin (stored XSS).
# Held as a data URI it cannot be navigated to and only ever renders inside an
# <img>, where scripts, external fetches and interactivity are all disabled.
_ICON_EXTENSIONS = {
    "image/png": ".png",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_ICON_SUFFIXES = frozenset({".png", ".ico", ".jpg", ".jpeg", ".gif", ".webp"})
_SVG_PREFIXES = (b"<svg", b"<?xml", b"<!doctype svg")


def _sniff_image(data: bytes) -> str | None:
    """Identify an image by its own bytes, returning a file suffix.

    Neither the Content-Type header nor the URL suffix can be trusted: a request
    for /favicon.ico on an auth-walled app commonly returns an HTML login page
    or error document with status 200, which would otherwise be filed away as
    a .ico and render as a broken image forever.
    """
    if len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\x00\x00\x01\x00"):
        return ".ico"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"

    # SVG is text, so it is sniffed by its root element rather than a magic
    # number. A leading XML declaration or BOM is common.
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(_SVG_PREFIXES) and b"<svg" in data[:4096].lower():
        return ".svg"
    return None


def _ico_is_complete(data: bytes) -> bool:
    """Check an ICO's directory against the bytes actually present.

    Guards against a truncated download being cached: the header parses fine, so
    only comparing the declared image extents to the file length catches it.
    """
    try:
        _, _, count = struct.unpack_from("<HHH", data, 0)
        for index in range(count):
            entry = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
            byte_count, image_offset = entry[6], entry[7]
            if image_offset + byte_count > len(data):
                return False
    except struct.error:
        return False
    return True


class ProbeRejected(Exception):
    """The URL failed policy. The message is safe to return to an admin caller."""


@dataclass
class ProbeResult:
    ok: bool
    url: str
    final_url: str
    status: int | None = None
    title: str | None = None
    #: og:site_name / application-name — what the app calls itself.
    site_name: str | None = None
    description: str | None = None
    icon_url: str | None = None
    redirects: list[str] = field(default_factory=list)
    error: str | None = None


# --- Address policy -------------------------------------------------------


def _normalise(address: ipaddress._BaseAddress):
    """Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) so the v4 rules apply."""
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def address_allowed(address) -> bool:
    ip = _normalise(ipaddress.ip_address(address))

    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or ip in METADATA_ADDRESSES
    ):
        return False

    if any(ip in network for network in settings.PROBE_ALLOWED_NETWORKS):
        return True
    return bool(ip.is_global)


def resolve_host(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ProbeRejected(f"Could not resolve host: {hostname}") from exc
    return sorted({info[4][0] for info in infos})


def validate_url(url: str) -> str:
    """Validate a URL against the SSRF policy. Returns the URL unchanged, or
    raises ProbeRejected. Every fetched URL — original, each redirect hop, and
    the favicon — passes through here."""
    parts = urlsplit((url or "").strip())

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ProbeRejected(
            f"Scheme '{parts.scheme or ''}' is not allowed; use http or https."
        )
    if not parts.hostname:
        raise ProbeRejected("URL has no host.")
    try:
        parts.port
    except ValueError:
        # `:99999` raises here rather than at fetch time, where it surfaces as
        # httpx.InvalidURL — which is NOT an HTTPError (verified in V1.3) and
        # so would escape every handler on the probe path (audit S9).
        raise ProbeRejected("URL port must be between 1 and 65535.") from None

    for address in resolve_host(parts.hostname):
        if not address_allowed(address):
            raise ProbeRejected(
                f"Address {address} for host {parts.hostname} is not permitted "
                f"by PROBE_ALLOWED_NETWORKS."
            )
    return url


def same_origin(a: str, b: str) -> bool:
    """Scheme, host and port all match — the §4c favicon fence.

    **A URL whose origin cannot be parsed is not same-origin with anything**
    (audit S9). `urlsplit(...).port` raises `ValueError` on a port like
    `:99999`, and that URL comes from a *probed page's* `<link href>` — remote
    content — so before this guard a malformed third-party href escaped the
    probe's error boundary and turned admin `/probe` into a 500. Refusing the
    candidate is the safe answer in both callers: the icon is skipped, the
    probe completes.
    """
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)
    except ValueError:
        return False


# --- Fetching -------------------------------------------------------------


def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read at most `limit` bytes. Truncated output is fine for HTML, where we
    only want the <head>."""
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) >= limit:
            return bytes(body[:limit])
    return bytes(body)


def _read_whole_or_none(response: httpx.Response, limit: int) -> bytes | None:
    """Read a binary body, or give up if it exceeds `limit`.

    Distinct from _read_capped because a truncated image is a corrupt file, not
    a smaller one — storing the first N bytes of an icon yields a broken image
    that also gets base64-inlined into the recovery export.
    """
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def probe_url(url: str) -> ProbeResult:
    """Fetch a URL and return metadata only — status, title, favicon, final URL.

    The raw response body is never returned to the caller (§4c).
    """
    try:
        current = validate_url(url)
    except ProbeRejected as exc:
        return ProbeResult(ok=False, url=url, final_url=url, error=str(exc))

    redirects: list[str] = []
    timeout = httpx.Timeout(settings.PROBE_TIMEOUT_S)

    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            verify=settings.PROBE_VERIFY_TLS,
        ) as client:
            for _ in range(settings.PROBE_MAX_REDIRECTS + 1):
                body = None
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            break
                        nxt = urljoin(current, location)
                        # Re-validate after every redirect (§4c) — a permitted
                        # host must not be able to bounce us to a denied one.
                        current = validate_url(nxt)
                        redirects.append(current)
                        continue

                    body = _read_capped(response, settings.PROBE_MAX_BYTES)
                    status_code = response.status_code
                    content_type = response.headers.get("content-type")

                # Icon fetch happens after the page stream is closed. Starting a
                # second request while the first is still open and only partially
                # read (the size cap can stop mid-body) leaves the pooled
                # connection in an indeterminate state.
                final_url = current
                meta = _extract_meta(body, content_type)
                return ProbeResult(
                    ok=True,
                    url=url,
                    final_url=final_url,
                    status=status_code,
                    title=_extract_title(body, content_type),
                    site_name=meta.get("site_name"),
                    description=meta.get("description"),
                    icon_url=_fetch_icon(client, final_url, body),
                    redirects=redirects,
                )
            return ProbeResult(
                ok=False,
                url=url,
                final_url=current,
                redirects=redirects,
                error=f"More than {settings.PROBE_MAX_REDIRECTS} redirects.",
            )
    except ProbeRejected as exc:
        return ProbeResult(
            ok=False, url=url, final_url=current, redirects=redirects, error=str(exc)
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            ok=False,
            url=url,
            final_url=current,
            redirects=redirects,
            error=f"{type(exc).__name__}: unreachable",
        )


def _extract_title(body: bytes, content_type: str | None) -> str | None:
    if content_type and "html" not in content_type.lower():
        return None
    text = body.decode("utf-8", errors="replace")
    match = _TITLE_RE.search(text)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title[:200] or None


def _extract_meta(body: bytes, content_type: str | None) -> dict[str, str]:
    """Pull the identity-bearing <meta> tags out of a page head."""
    if content_type and "html" not in content_type.lower():
        return {}
    text = body.decode("utf-8", errors="replace")

    found: dict[str, str] = {}
    for match in _META_RE.finditer(text):
        attrs = _attrs(match.group(1))
        key = attrs.get("name") or attrs.get("property")
        content = (attrs.get("content") or "").strip()
        if key and content:
            found.setdefault(key.lower(), content[:200])

    result: dict[str, str] = {}
    for candidate in _SITE_NAME_KEYS:
        if found.get(candidate):
            result["site_name"] = found[candidate]
            break
    if found.get("description"):
        result["description"] = found["description"]
    return result


#: At most this many icon candidates are attempted (audit S9). A page can
#: declare any number of `<link rel="icon">`s, and each miss costs a full
#: fetch timeout — unbounded, that is remote content deciding how long the
#: probe runs. Eight covers every multi-size declaration seen in the wild
#: (LazyLibrarian's worst case is two).
_MAX_ICON_CANDIDATES = 8

#: Wall-clock budget for the whole icon loop, as a multiple of the per-fetch
#: timeout (audit S9). With the candidate cap this bounds the probe's total
#: bytes too: at most `_MAX_ICON_CANDIDATES × PROBE_MAX_ICON_BYTES` ever cross
#: the wire after the page itself.
_ICON_BUDGET_TIMEOUTS = 3


def _fetch_icon(client: httpx.Client, page_url: str, body: bytes) -> str | None:
    """Fetch and cache the page's favicon.

    Spec §4c: a `<link rel="icon">` can point anywhere, so v0 fetches favicons
    only from the same origin as the probed page; if the icon lives elsewhere,
    skip it and use the default.
    """
    deadline = time.monotonic() + settings.PROBE_TIMEOUT_S * _ICON_BUDGET_TIMEOUTS
    for candidate in _icon_candidates(page_url, body):
        if time.monotonic() >= deadline:
            logger.info("Icon budget exhausted for %s; using no icon", page_url)
            return None
        if candidate.startswith("data:"):
            inline = _accept_data_uri(candidate)
            if inline:
                return inline
            continue
        if not same_origin(candidate, page_url):
            logger.info("Skipping cross-origin favicon for %s", page_url)
            continue
        stored = fetch_and_store_icon(candidate, client=client)
        if stored:
            return stored
    return None


#: Only the base64 form of an inline icon is accepted (audit B3). The base64
#: alphabet cannot contain a quote or an angle bracket, so nothing matching
#: this can break out of an HTML attribute downstream.
_DATA_URI_ICON_RE = re.compile(
    r"^data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=]*$", re.IGNORECASE
)


def _accept_data_uri(candidate: str) -> str | None:
    """Take an inline base64 `data:image/...` icon as-is.

    Plenty of modern apps inline their favicon this way, and skipping them
    leaves those services blank. Nothing is fetched, so there is no SSRF
    surface, and nothing is written to MEDIA_ROOT.

    **Only the `;base64,` form is accepted.** This function's earlier claim —
    that a data URI is harmless because an `<img>` never executes scripts —
    was true of the SPA and false of the recovery export, which interpolated
    the value into an attribute: `data:image/svg+xml,<svg/>" onerror="…`
    breaks out cleanly (audit B3, verified by execution). The utf-8 SVG form
    can carry arbitrary markup; the base64 alphabet cannot, so requiring it
    makes every stored inline icon attribute-safe by construction. The cost is
    that a non-base64 inline favicon (some apps ship utf-8 SVG) is refused and
    the service falls back to no icon.
    """
    if not _DATA_URI_ICON_RE.match(candidate):
        return None
    if len(candidate) > settings.ICON_MAX_INLINE_BYTES:
        logger.info("Ignoring oversized inline icon (%s bytes)", len(candidate))
        return None
    return candidate


def _icon_candidates(page_url: str, body: bytes) -> list[str]:
    """Icon URLs to try, best first. Each is attempted until one sticks.

    EVERY declared icon link is collected, not just the first. Pages routinely
    declare several at different sizes, and the first is often the worst choice:
    LazyLibrarian declares a 370KB `shortcut icon` .ico followed by a 4KB
    `apple-touch-icon` .png, so taking only the first hits the size cap and the
    service ends up with no icon at all despite offering a perfectly good one.

    The page-relative `favicon.ico` matters for apps served under a subpath:
    OpenObserve redirects `/` to `/web/` and serves its icon at
    `/web/favicon.ico`, while the site root `/favicon.ico` returns 401.
    """
    candidates: list[str] = []
    text = body.decode("utf-8", errors="replace") if body else ""
    for match in _LINK_RE.finditer(text):
        attrs = _attrs(match.group(1))
        # "icon", "shortcut icon", "apple-touch-icon", "mask-icon" — but not
        # "stylesheet" pointing at iconography.css.
        rel = attrs.get("rel", "").lower()
        if not any(t == "icon" or t.endswith("-icon") for t in rel.split()):
            continue
        value = attrs.get("href", "").strip()
        if not value:
            continue
        # A data: URI is self-contained; urljoin would mangle it.
        candidates.append(value if value.startswith("data:") else urljoin(page_url, value))

    candidates.append(urljoin(page_url, "favicon.ico"))  # relative to the page
    candidates.append(urljoin(page_url, "/favicon.ico"))  # site root

    seen, unique = set(), []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    # Capped (audit S9): the list is remote content, and each entry can cost a
    # full timeout. The two `favicon.ico` fallbacks sit last, so a page
    # declaring eight or more icons forfeits them — a page with that many
    # declared icons does not need a guessed one.
    return unique[:_MAX_ICON_CANDIDATES]


def fetch_and_store_icon(url: str, client: httpx.Client | None = None) -> str | None:
    """Validate, fetch and cache one icon URL. Returns its local media path.

    Shared by probe autodiscovery and the admin's manual icon override, so both
    go through the same SSRF validator and the same size cap, and both land in
    MEDIA_ROOT/icons where the recovery export can inline them (§9).
    """
    owned = client is None
    if owned:
        client = httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(settings.PROBE_TIMEOUT_S),
            verify=settings.PROBE_VERIFY_TLS,
        )
    try:
        validate_url(url)
        with client.stream("GET", url) as response:
            if response.status_code >= 400:
                return None
            content_type = (response.headers.get("content-type") or "").split(";")[0]
            data = _read_whole_or_none(response, settings.PROBE_MAX_ICON_BYTES)
    except (ProbeRejected, httpx.HTTPError):
        return None
    finally:
        if owned:
            client.close()

    if not data:
        return None
    return _store_icon(data, content_type, url)


def store_icon_bytes(data: bytes, source_name: str = "upload") -> str | None:
    """Public entry point for bytes that did not come from a fetch (an upload).

    Same validation as a fetched icon: the signature decides, never the
    filename or a client-supplied content type.
    """
    return _store_icon(data, "", source_name)


def _store_icon(data: bytes, content_type: str, source_url: str) -> str | None:
    # The bytes decide. Content-Type and the URL suffix are both hints supplied
    # by the remote server and are routinely wrong.
    suffix = _sniff_image(data)
    if suffix is None:
        logger.info("Ignoring non-image response for icon %s", source_url)
        return None
    if suffix == ".ico" and not _ico_is_complete(data):
        logger.info("Ignoring truncated icon %s", source_url)
        return None

    if suffix == ".svg":
        # NEVER write an SVG under MEDIA_ROOT. Held inline it cannot be
        # navigated to, so it cannot execute in our origin. Capped tighter than
        # a file-backed icon because this string travels in every
        # GET /api/v1/services response.
        if len(data) > settings.ICON_MAX_INLINE_BYTES:
            logger.info("Ignoring oversized SVG icon %s", source_url)
            return None
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"
    digest = hashlib.sha256(data).hexdigest()[:32]
    icons_dir = Path(settings.MEDIA_ROOT) / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    path = icons_dir / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return f"{settings.MEDIA_URL}icons/{path.name}"
