"""Static recovery export — spec §9."""

from pathlib import Path

import pytest

from services import export_static
from services.export_static import render_export, run_static_export
from services.models import Service

pytestmark = pytest.mark.django_db


@pytest.fixture
def export_path(settings, tmp_path):
    settings.STATIC_EXPORT_ENABLED = True
    path = tmp_path / "share" / "index.html"
    settings.STATIC_EXPORT_PATH = str(path)
    settings.STATIC_EXPORT_STALE_MARKER = str(tmp_path / "local" / "export.stale")
    return path


def make(**kwargs):
    defaults = {
        "slug": "jellyfin",
        "name": "Jellyfin",
        "launch_url": "https://jellyfin.lab.local",
        "category": "media",
        "host_label": "pve-c · CT 117",
    }
    return Service.objects.create(**{**defaults, **kwargs})


def test_relative_export_path_is_anchored_to_the_repo_root():
    """A relative path must not resolve against the process CWD: the web process
    runs from backend/ and the worker may run from anywhere, so they would write
    the recovery file to two different places."""
    from django.conf import settings

    assert Path(settings.STATIC_EXPORT_PATH).is_absolute()


# --- Concurrency (audit S7) ------------------------------------------------
#
# Every committed mutation used to race its own daemon thread against three
# web workers and the health-worker retry over one shared target file and one
# fixed `.tmp` name. The mechanism is now: mark pending -> try the advisory
# lock -> the holder clears pending before reading and re-checks it after
# publishing and after releasing. These tests pin each leg of that mechanism.


def test_a_request_that_loses_the_lock_defers_and_leaves_pending_set(
    export_path, monkeypatch
):
    """The loser must not write anything — and must leave the flag up so the
    holder's re-check runs for it. Its data is committed (on_commit dispatched
    it), so the holder's read carries it."""
    make()
    monkeypatch.setattr(export_static, "_try_lock", lambda: False)
    rendered = []
    monkeypatch.setattr(
        export_static, "_render_and_publish", lambda: rendered.append(1) or True
    )

    assert run_static_export() is True

    assert rendered == []
    assert export_static._pending() is True


def test_a_mutation_landing_mid_export_triggers_exactly_one_follow_up(
    export_path, monkeypatch
):
    """The coalescing contract: pending set while the holder is publishing
    means one more pass — not one pass per queued request, and never zero."""
    make()
    real_publish = export_static._render_and_publish
    passes = []

    def publish_with_interruption():
        passes.append(1)
        if len(passes) == 1:
            # A concurrent request lands while we are mid-publish.
            export_static._mark_pending()
        return real_publish()

    monkeypatch.setattr(export_static, "_render_and_publish", publish_with_interruption)

    assert run_static_export() is True

    assert len(passes) == 2
    assert export_static._pending() is False


def test_a_burst_of_requests_coalesces_into_one_pass(export_path, monkeypatch):
    """Bulk import: many mutations, each marking pending — one export."""
    make()
    for _ in range(5):
        export_static._mark_pending()
    passes = []
    real_publish = export_static._render_and_publish
    monkeypatch.setattr(
        export_static,
        "_render_and_publish",
        lambda: passes.append(1) or real_publish(),
    )

    assert run_static_export() is True

    assert len(passes) == 1


def test_each_attempt_writes_its_own_temp_file(export_path, monkeypatch):
    """A fixed `.tmp` name shared by every writer on a shared volume lets two
    writers interleave one another's half-written files (audit S7)."""
    make()
    written = []
    real_write = Path.write_text

    def recording_write(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            written.append(self.name)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", recording_write)

    run_static_export()
    run_static_export()

    assert len(written) == 2
    assert written[0] != written[1]


def test_no_temp_file_survives_a_successful_publish(export_path):
    make()
    run_static_export()
    assert [p.name for p in export_path.parent.iterdir()] == [export_path.name]


def test_a_deferring_request_does_not_touch_the_stale_marker(
    export_path, monkeypatch
):
    """Ordering of marker writes is what the lock exists for: a request that
    did not export may not clear (or set) the outcome of the one that did."""
    make()
    export_static._set_stale_marker()
    monkeypatch.setattr(export_static, "_try_lock", lambda: False)

    run_static_export()

    assert export_static.export_is_stale() is True


def test_export_is_written_and_lists_every_service(export_path):
    make()
    make(slug="karakeep", name="Karakeep", launch_url="https://karakeep.lab.local")

    assert run_static_export() is True
    html = export_path.read_text(encoding="utf-8")
    assert "Jellyfin" in html and "Karakeep" in html
    assert "https://jellyfin.lab.local" in html


def test_export_depends_on_nothing_external(export_path):
    """"It is a file, not a served page" — it must open from a share with
    file:// and work with the app stopped."""
    make()
    run_static_export()
    html = export_path.read_text(encoding="utf-8")

    assert "<script" not in html.lower()
    # No external stylesheet, font, or image references.
    assert 'href="http' not in html and 'src="http' not in html
    # Every service link is an absolute URL so it works from a local file.
    assert "href='https://jellyfin.lab.local'" in html


def test_service_text_is_escaped(export_path):
    """The title can come from a probed third-party page, so it is attacker-
    influenced text landing in an HTML document."""
    make(name='Evil <script>alert(1)</script>', category='"onmouseover="x')
    run_static_export()
    html = export_path.read_text(encoding="utf-8")

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_hostile_icon_url_cannot_break_out_of_the_img_attribute(export_path):
    """`icon_url` has no validation on any write path and can be set through the
    API, so a `data:image/svg+xml,...` value can carry an attribute breakout
    (audit B3). Fable's verified payload put a live `onerror` in the recovery
    file, which the operator opens as file:// during an outage.

    The renderer now only inlines the base64 `data:image/` form (no quote or
    angle bracket possible), and escapes the cell regardless, so the payload
    survives as neither a working attribute nor an inlined image."""
    make(
        icon_url='data:image/svg+xml,<svg/>" onerror="alert(document.domain)" data-x="'
    )
    run_static_export()
    html = export_path.read_text(encoding="utf-8")

    assert 'onerror="alert' not in html
    assert 'onerror=' not in html
    # The utf-8 SVG data URI is refused outright, so it never reaches an img src.
    assert "<svg/>" not in html


def test_a_base64_image_data_uri_still_inlines(export_path):
    """The safe form — the one real services actually ship — is unaffected."""
    make(icon_url="data:image/png;base64,iVBORw0KGgo=")
    run_static_export()
    html = export_path.read_text(encoding="utf-8")

    assert 'src="data:image/png;base64,iVBORw0KGgo="' in html


def test_a_non_http_launch_url_is_rendered_as_text_not_a_link(export_path):
    """`html.escape` leaves `javascript:alert(1)` intact — no special character
    — so escaping alone still yields a working script link (audit B3). Only
    http(s) becomes an anchor; anything else is shown as plain text so the
    operator can still see the bad value."""
    make(slug="js", name="JS", launch_url="javascript:alert(document.domain)")
    run_static_export()
    html = export_path.read_text(encoding="utf-8")

    assert "href='javascript:" not in html
    assert "href=\"javascript:" not in html
    # The value is still visible in the URL column, just not clickable.
    assert "javascript:alert(document.domain)" in html


def test_an_http_launch_url_is_still_a_link(export_path):
    make()
    run_static_export()
    html = export_path.read_text(encoding="utf-8")
    assert "href='https://jellyfin.lab.local'" in html


def test_failure_leaves_a_stale_marker_and_never_raises(settings, tmp_path):
    """A NAS outage must not prevent saving a service (§9).

    The marker has to be written even when the share is the thing that failed,
    which is why it lives locally rather than beside the export.
    """
    make()
    # Simulate an unreachable share: a path whose parent cannot be created.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    settings.STATIC_EXPORT_PATH = str(blocker / "sub" / "index.html")
    settings.STATIC_EXPORT_STALE_MARKER = str(tmp_path / "local" / "export.stale")

    assert run_static_export() is False
    assert export_static.export_is_stale() is True


def test_stale_marker_is_not_stored_on_the_share(settings):
    """If it lived next to the export, an unreachable share would prevent
    recording staleness in exactly the case it matters, and the worker's
    retry-on-next-cycle would never fire."""
    from django.conf import settings as live

    assert not str(live.STATIC_EXPORT_STALE_MARKER).startswith(
        str(Path(live.STATIC_EXPORT_PATH).parent)
    )


def test_successful_export_clears_the_stale_marker(export_path, settings):
    make()
    marker = Path(settings.STATIC_EXPORT_STALE_MARKER)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("stale")

    assert run_static_export() is True
    assert not marker.exists()


def test_render_handles_an_empty_catalog():
    assert "0 services" in render_export([])


def test_a_base64_data_uri_icon_passes_straight_through(settings):
    from services.export_static import _icon_data_uri

    uri = "data:image/png;base64,iVBORw0KGgo="
    assert _icon_data_uri(uri) == uri


def test_a_utf8_svg_data_uri_icon_is_refused(settings):
    """The recovery export interpolates this into an attribute, and a utf-8 SVG
    data URI can carry a quote (audit B3). Only the base64 form is accepted."""
    from services.export_static import _icon_data_uri

    assert _icon_data_uri("data:image/svg+xml,<svg/>") is None


def test_an_oversized_icon_is_not_inlined(settings, tmp_path):
    """§9 assumes favicons are small; real ones are not always. A few hundred KB
    of base64 per service would bloat the one file that must stay instantly
    openable during an outage."""
    from services.export_static import _icon_data_uri

    settings.MEDIA_ROOT = str(tmp_path)
    settings.STATIC_EXPORT_MAX_ICON_BYTES = 1024
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
    (icons / "small.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    assert _icon_data_uri("/media/icons/big.png") is None
    assert _icon_data_uri("/media/icons/small.png").startswith("data:image/png;base64,")
