"""Static recovery export — spec §9.

On every catalog mutation, regenerate a dependency-free single HTML file listing
every service name and launch URL, written to the NAS share.

The ordering in §9 is the whole point and is not negotiable:

    1. Commit the change to PostgreSQL.
    2. Attempt the static export outside that transaction.
    3. On failure: keep the database change, log it, mark the export stale,
       and retry on the next mutation or worker cycle.

"A NAS outage must not prevent saving a service. Otherwise the fallback
system acquires the power to take down the primary catalog, which inverts its
entire purpose."
"""

import base64
import html
import logging
import mimetypes
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction

from .db import releases_connections

logger = logging.getLogger(__name__)

#: Advisory lock key serialising export writers across every process and
#: container that shares the database (audit S7). The bytes spell "HLVEXPOR";
#: the value only has to be stable and unlikely to collide.
_EXPORT_LOCK_KEY = 0x484C564558504F52


def schedule_static_export() -> None:
    """Queue an export to run after the current transaction commits.

    Registered via `transaction.on_commit` so it runs outside the transaction,
    and dispatched to a daemon thread so a hung NAS mount cannot make a catalog
    write block.
    """
    if not settings.STATIC_EXPORT_ENABLED:
        return
    transaction.on_commit(_dispatch)


def _dispatch() -> threading.Thread:
    """Start the export thread. Returns it so callers can join deterministically.

    This function *owns* the thread, so it is where the thread's database
    connection is released — see services/db.py. `run_static_export` itself must
    stay hygiene-free: it is also called synchronously, inside transactions it
    does not own.
    """
    thread = threading.Thread(
        target=releases_connections(run_static_export), name="hlv-static-export"
    )
    thread.daemon = True
    thread.start()
    return thread


def run_static_export() -> bool:
    """Render and write the export. Returns True on success. Never raises.

    Safe to call from anywhere — a fresh thread, a management command, or
    synchronously inside somebody else's transaction. It deliberately does **no**
    connection management: whoever owns the thread does that. See services/db.py
    for what happens when this function tears down a connection it did not open.

    **Serialised across processes and containers by a Postgres advisory lock,
    and coalesced through `StaticExportState.pending`** (audit S7). Before
    this, every committed mutation raced its own thread against three web
    workers and the health-worker retry over one shared target file: an
    export could read the catalog, be overtaken by a newer export, then
    resume and overwrite newer data with its older snapshot — and a late
    success could clear a stale marker a newer failure had just set. Bulk
    import is exactly the burst that produced it. Now: every request marks
    `pending`; whoever holds the lock clears the flag *before* reading, so a
    mutation landing mid-export triggers exactly one follow-up pass; a
    request that loses the lock just leaves, because the holder re-checks the
    flag after publishing **and again after releasing** — the second check is
    what closes the window where a request arrives between the two.
    """
    try:
        _mark_pending()
        result = True
        while _pending():
            if not _try_lock():
                # Somebody else is exporting right now and will see the flag
                # set above. Their pass (or their follow-up) carries this
                # request's committed data — on_commit dispatched us, so the
                # data is already visible to any read that starts after this.
                return True
            try:
                _clear_pending()
                result = _render_and_publish()
            finally:
                _unlock()
        return result
    except Exception:  # noqa: BLE001 - the export must never take down a write
        logger.exception("Static export failed; catalog write is unaffected.")
        _set_stale_marker()
        return False


def _render_and_publish() -> bool:
    """One render-and-replace, under the advisory lock. Never raises."""
    try:
        from .models import Service

        services = list(Service.objects.all())
        document = render_export(services)

        target = Path(settings.STATIC_EXPORT_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace so a crash mid-write cannot leave a truncated
        # recovery file, which is the one moment it needs to be readable. The
        # temp name is unique per attempt (audit S7): a fixed `.tmp` shared
        # by every writer on a shared volume let two writers interleave one
        # another's half-written files.
        tmp = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(document, encoding="utf-8")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)  # a no-op after a successful replace

        _clear_stale_marker()
        return True
    except Exception:  # noqa: BLE001 - the export must never take down a write
        logger.exception("Static export failed; catalog write is unaffected.")
        _set_stale_marker()
        return False


# --- Cross-process coordination (audit S7) ---------------------------------


def _try_lock() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_EXPORT_LOCK_KEY])
        return bool(cursor.fetchone()[0])


def _unlock() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [_EXPORT_LOCK_KEY])


def _mark_pending() -> None:
    from .models import StaticExportState

    StaticExportState.objects.get_or_create(pk=1)
    StaticExportState.objects.filter(pk=1).update(pending=True)


def _clear_pending() -> None:
    from .models import StaticExportState

    StaticExportState.objects.filter(pk=1).update(pending=False)


def _pending() -> bool:
    from .models import StaticExportState

    row = StaticExportState.objects.filter(pk=1).only("pending").first()
    return bool(row and row.pending)


def _stale_marker_path() -> Path:
    # Local state, never the share — see STATIC_EXPORT_STALE_MARKER in settings.
    return Path(settings.STATIC_EXPORT_STALE_MARKER)


def _set_stale_marker() -> None:
    try:
        marker = _stale_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError:
        logger.warning("Could not write static-export stale marker.")


def _clear_stale_marker() -> None:
    try:
        _stale_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def export_is_stale() -> bool:
    return _stale_marker_path().exists()


#: The only data-URI shape this module will emit into the page. The base64
#: alphabet cannot carry a quote or an angle bracket, so a URI matching this
#: cannot break out of an attribute even before escaping. The probe enforces
#: the same shape on ingest (`_accept_data_uri`); this copy is the export's own
#: seam, because `icon_url` is writable through the API with no validation and
#: rows may predate the probe-side rule (audit B3).
_SAFE_DATA_URI_RE = re.compile(
    r"^data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=]*$", re.IGNORECASE
)


def _is_http_url(url: str) -> bool:
    """Only http(s) may become a clickable link (audit B3).

    `html.escape` is not enough for the anchor: `javascript:alert(1)` contains
    no escapable character, so an escaped copy is still a working script link —
    the same class the SPA's `safeUrl()` guards, in a renderer that inherited
    none of it.
    """
    try:
        scheme = urlsplit((url or "").strip()).scheme
    except ValueError:
        return False
    return scheme.lower() in ("http", "https")


def _icon_data_uri(icon_url: str | None) -> str | None:
    """Inline the cached favicon as base64 (§9).

    Favicons are cached under MEDIA_ROOT/icons, which a `file://` page cannot
    reach, so the export inlines them or it renders broken images.
    """
    if not icon_url:
        return None
    # Some services publish their icon as an inline data URI; it is already
    # self-contained and needs no work — but only the base64 image form is
    # passed through. A utf-8 SVG data URI can carry arbitrary markup, and the
    # recovery file is opened as file:// at the worst possible moment.
    if icon_url.startswith("data:"):
        if not _SAFE_DATA_URI_RE.match(icon_url):
            return None
        return icon_url if len(icon_url) <= settings.STATIC_EXPORT_MAX_ICON_BYTES else None

    name = icon_url.rsplit("/", 1)[-1]
    path = Path(settings.MEDIA_ROOT) / "icons" / name
    try:
        if path.stat().st_size > settings.STATIC_EXPORT_MAX_ICON_BYTES:
            # A few hundred KB of base64 per service would bloat the one file
            # that has to stay instantly openable during an outage.
            return None
        data = path.read_bytes()
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def render_export(services) -> str:
    """Build the dependency-free recovery page.

    Every link is an absolute http(s) URL so it works when opened as a local
    file from the SMB share (§9).
    """
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for service in sorted(services, key=lambda s: (s.category or "", s.name.lower())):
        icon = _icon_data_uri(service.icon_url)
        # Escaped like every neighbouring cell (audit B3): the icon can come
        # from a probed page, and this cell was the one that trusted it.
        icon_html = (
            f'<img src="{html.escape(icon, quote=True)}" alt="" width="20" height="20">'
            if icon
            else '<span class="noicon"></span>'
        )
        launch_url = service.launch_url or ""
        name_html = html.escape(service.name)
        # A rejected URL is still displayed in the URL column so the operator
        # can see what is wrong with it — it just never becomes an anchor.
        if _is_http_url(launch_url):
            name_cell = (
                f"<td><a href='{html.escape(launch_url, quote=True)}'>"
                f"{name_html}</a></td>"
            )
        else:
            name_cell = f"<td>{name_html}</td>"
        rows.append(
            "<tr>"
            f"<td class=i>{icon_html}</td>"
            f"{name_cell}"
            f"<td class=m>{html.escape(service.category or '')}</td>"
            f"<td class=m>{html.escape(service.host_label or '')}</td>"
            f"<td class=u>{html.escape(launch_url)}</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Homelab Vault — recovery export</title>
<style>
 body {{ font: 14px/1.5 system-ui, sans-serif; margin: 32px; background: #f4f4f7;
        color: #20202a; }}
 h1 {{ font-size: 20px; margin: 0 0 4px; }}
 p.sub {{ color: #5c5c68; margin: 0 0 24px; font-size: 12.5px; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff;
         border: 1px solid #e9e9ef; border-radius: 10px; overflow: hidden; }}
 td, th {{ padding: 8px 12px; border-bottom: 1px solid #f0f0f5; text-align: left; }}
 th {{ font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em;
      color: #8f8f9c; }}
 td.i {{ width: 28px; }}
 td.m {{ color: #5c5c68; font-size: 12.5px; }}
 td.u {{ color: #8f8f9c; font-family: ui-monospace, monospace; font-size: 11.5px; }}
 a {{ color: #5b48e0; text-decoration: none; font-weight: 600; }}
 a:hover {{ text-decoration: underline; }}
 .noicon {{ display: inline-block; width: 20px; height: 20px; border-radius: 6px;
           background: #ececf3; }}
</style>
</head>
<body>
<h1>Homelab Vault — recovery export</h1>
<p class="sub">{len(rows)} services · generated {generated} · this file depends on
nothing and works from a local drive.</p>
<table>
<tr><th></th><th>Service</th><th>Category</th><th>Location</th><th>URL</th></tr>
{chr(10).join(rows)}
</table>
</body>
</html>
"""
