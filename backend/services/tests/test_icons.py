"""Favicon discovery, size handling and the manual override — spec §4c, §9."""

import base64
import json
import struct

import pytest

from services import probe as probe_module

pytestmark = pytest.mark.django_db


def test_page_relative_favicon_is_tried_before_the_site_root():
    """The OpenObserve case: the app redirects / to /web/ and serves its icon at
    /web/favicon.ico, while the site root /favicon.ico returns 401. Trying only
    the root misses every app served under a subpath."""
    candidates = probe_module._icon_candidates("http://host:5080/web/", b"")
    assert candidates == [
        "http://host:5080/web/favicon.ico",
        "http://host:5080/favicon.ico",
    ]


def test_a_declared_icon_link_is_preferred():
    body = b'<html><head><link rel="icon" href="/assets/logo.png">'
    candidates = probe_module._icon_candidates("http://host:5080/web/", body)
    assert candidates[0] == "http://host:5080/assets/logo.png"


def test_candidates_are_deduplicated():
    body = b'<html><head><link rel="icon" href="/favicon.ico">'
    candidates = probe_module._icon_candidates("http://host:5080/", body)
    assert len(candidates) == len(set(candidates))


class _FakeStream:
    def __init__(self, chunks, status=200, content_type="image/png"):
        self._chunks = chunks
        self.status_code = status
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield from self._chunks


class _FakeClient:
    def __init__(self, stream):
        self._stream = stream
        self.calls = []

    def stream(self, method, url):
        self.calls.append(url)
        return self._stream


def test_an_oversized_icon_is_refused_not_truncated(settings, tmp_path, monkeypatch):
    """A partial image is corrupt, not smaller — and it would be base64-inlined
    into the recovery export as a broken image."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.PROBE_MAX_ICON_BYTES = 1024
    monkeypatch.setattr(probe_module, "validate_url", lambda url: url)

    client = _FakeClient(_FakeStream([b"x" * 4096]))
    assert probe_module.fetch_and_store_icon("http://host/i.png", client=client) is None
    assert not list(tmp_path.glob("icons/*"))


def test_an_icon_within_the_cap_is_stored(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.PROBE_MAX_ICON_BYTES = 4096
    monkeypatch.setattr(probe_module, "validate_url", lambda url: url)

    client = _FakeClient(_FakeStream([PNG[:8], PNG[8:]]))
    stored = probe_module.fetch_and_store_icon("http://host/i.png", client=client)
    assert stored and stored.endswith(".png")
    assert len(list(tmp_path.glob("icons/*"))) == 1


def test_a_404_icon_is_skipped(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path)
    monkeypatch.setattr(probe_module, "validate_url", lambda url: url)
    client = _FakeClient(_FakeStream([b""], status=404))
    assert probe_module.fetch_and_store_icon("http://host/i.png", client=client) is None


#: A real PNG signature plus filler. The 8-byte signature matters — a partial
#: one is exactly what the sniffer is meant to reject.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _ico(images=1, truncate=0):
    """Minimal but structurally valid ICO: header, directory, then pixel data."""
    header = struct.pack("<HHH", 0, 1, images)
    directory = b""
    offset = 6 + images * 16
    payload = b""
    for _ in range(images):
        blob = b"\x00" * 64
        directory += struct.pack(
            "<BBBBHHII", 16, 16, 0, 0, 1, 32, len(blob), offset + len(payload)
        )
        payload += blob
    data = header + directory + payload
    return data[: len(data) - truncate] if truncate else data


class TestImageSniffing:
    """Neither Content-Type nor the URL suffix can be trusted — an auth-walled
    app commonly answers /favicon.ico with an HTML login page and a 200."""

    def test_an_html_page_is_not_an_icon(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        html = b"<!DOCTYPE html><html><head><title>Login</title></head>"
        assert probe_module._store_icon(html, "image/x-icon", "http://h/favicon.ico") is None

    def test_a_lying_content_type_does_not_matter(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        stored = probe_module._store_icon(PNG, "text/html", "http://h/whatever")
        assert stored and stored.endswith(".png")

    @pytest.mark.parametrize(
        "data,suffix",
        [
            (PNG, ".png"),
            (_ico(), ".ico"),
            (b"\xff\xd8\xff" + b"\x00" * 32, ".jpg"),
            (b"GIF89a" + b"\x00" * 32, ".gif"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32, ".webp"),
        ],
    )
    def test_recognises_real_formats(self, data, suffix):
        assert probe_module._sniff_image(data) == suffix

    def test_rejects_anything_else(self):
        assert probe_module._sniff_image(b"not an image at all") is None
        assert probe_module._sniff_image(b"") is None

    @pytest.mark.parametrize(
        "data",
        [
            b'<svg width="32" height="32" xmlns="http://www.w3.org/2000/svg"></svg>',
            b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
            b'\xef\xbb\xbf<svg xmlns="http://www.w3.org/2000/svg"/>',  # BOM
            b'\n  <svg xmlns="http://www.w3.org/2000/svg"/>',  # leading space
        ],
    )
    def test_recognises_svg_by_its_root_element(self, data):
        """SVG is text, so it has no magic number to sniff."""
        assert probe_module._sniff_image(data) == ".svg"

    def test_html_is_not_mistaken_for_svg(self):
        assert probe_module._sniff_image(b"<?xml ?><html><body>hi</body></html>") is None
        assert probe_module._sniff_image(b"<!DOCTYPE html><html></html>") is None


class TestSvgIcons:
    """OmniRoute publishes /favicon.svg — a perfectly good icon that the raster
    only rule refused."""

    def test_an_svg_becomes_an_inline_data_uri(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
        stored = probe_module._store_icon(svg, "image/svg+xml", "http://h/favicon.svg")

        assert stored.startswith("data:image/svg+xml;base64,")
        # Round-trips: what is stored is exactly what was fetched.
        payload = stored.split(",", 1)[1]
        assert base64.b64decode(payload) == svg

    def test_the_uploaded_route_accepts_svg_too(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        assert probe_module.store_icon_bytes(svg, "logo.svg").startswith("data:image/svg")


class TestTruncatedIco:
    """A truncated ICO parses its header fine, so only comparing the declared
    image extents against the real length catches it. This is what left a
    corrupt 131072-byte file cached under the old size cap."""

    def test_a_complete_ico_passes(self):
        assert probe_module._ico_is_complete(_ico(images=3)) is True

    def test_a_truncated_ico_is_detected(self):
        assert probe_module._ico_is_complete(_ico(images=3, truncate=40)) is False

    def test_a_truncated_ico_is_not_stored(self, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        data = _ico(images=3, truncate=40)
        assert probe_module._store_icon(data, "image/x-icon", "http://h/i.ico") is None
        assert not list(tmp_path.glob("icons/*"))


class TestMultipleDeclaredIcons:
    """A page may declare several icons; the first is often the worst choice.

    LazyLibrarian declares a 370KB `shortcut icon` .ico followed by a 4KB
    `apple-touch-icon` .png. Collecting only the first means one oversized
    candidate leaves the service with no icon at all.
    """

    def test_every_declared_icon_becomes_a_candidate(self):
        body = (
            b'<link rel="shortcut icon" type="image/png" href="images/ll.ico">'
            b'<link rel="apple-touch-icon" href="images/ll.png">'
        )
        candidates = probe_module._icon_candidates("http://h:5299/", body)
        assert candidates[:2] == [
            "http://h:5299/images/ll.ico",
            "http://h:5299/images/ll.png",
        ]

    def test_a_failing_candidate_falls_through_to_the_next(
        self, settings, tmp_path, monkeypatch
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        monkeypatch.setattr(probe_module, "validate_url", lambda url: url)
        attempted = []

        def fake(url, client=None):
            attempted.append(url)
            return "/media/icons/ok.png" if url.endswith(".png") else None

        monkeypatch.setattr(probe_module, "fetch_and_store_icon", fake)
        body = (
            b'<link rel="icon" href="/big.ico">'
            b'<link rel="apple-touch-icon" href="/small.png">'
        )
        result = probe_module._fetch_icon(None, "http://h/", body)

        assert result == "/media/icons/ok.png"
        assert attempted == ["http://h/big.ico", "http://h/small.png"]


class TestIconBudget:
    """Audit S9: the candidate list and the time spent on it are both remote
    content — a page decides how many icons it declares, and each miss costs a
    full fetch timeout. Both are bounded here, at the loop's own seam."""

    def _many_icons(self, count=30):
        return b"".join(
            f'<link rel="icon" href="/i{n}.png">'.encode() for n in range(count)
        )

    def test_the_candidate_list_is_capped(self):
        candidates = probe_module._icon_candidates("http://h/", self._many_icons())
        assert len(candidates) == probe_module._MAX_ICON_CANDIDATES

    def test_the_wall_clock_budget_stops_the_loop(self, monkeypatch, settings):
        """Each candidate is allowed its own timeout, but the loop as a whole
        is not allowed candidates × timeout."""
        import types

        settings.PROBE_TIMEOUT_S = 5.0
        clock = [1000.0]
        monkeypatch.setattr(
            probe_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])
        )
        monkeypatch.setattr(probe_module, "validate_url", lambda url: url)
        attempted = []

        def slow_miss(url, client=None):
            attempted.append(url)
            clock[0] += 100.0  # a candidate that eats far past the budget
            return None

        monkeypatch.setattr(probe_module, "fetch_and_store_icon", slow_miss)

        result = probe_module._fetch_icon(None, "http://h/", self._many_icons(5))

        assert result is None
        assert len(attempted) == 1  # the budget stopped the loop, not the cap

    def test_a_malformed_declared_href_is_skipped_not_a_crash(
        self, monkeypatch, settings, tmp_path
    ):
        """`urlsplit(...).port` raises on `:99999`, and the href comes from the
        probed page. Before the same_origin guard this 500'd admin /probe."""
        settings.MEDIA_ROOT = str(tmp_path)
        monkeypatch.setattr(probe_module, "validate_url", lambda url: url)
        attempted = []

        def fake(url, client=None):
            attempted.append(url)
            return "/media/icons/ok.png"

        monkeypatch.setattr(probe_module, "fetch_and_store_icon", fake)
        body = (
            b'<link rel="icon" href="http://h:99999/evil.png">'
            b'<link rel="icon" href="/fine.png">'
        )

        result = probe_module._fetch_icon(None, "http://h/", body)

        assert result == "/media/icons/ok.png"
        assert attempted == ["http://h/fine.png"]

    def test_an_unparseable_origin_is_not_same_origin_with_anything(self):
        assert probe_module.same_origin("http://h:99999/x", "http://h/") is False
        assert probe_module.same_origin("http://h/", "http://h:99999/x") is False


class TestInlineDataUriIcons:
    """Plenty of apps inline their favicon as a base64 data URI. The `<link>`
    parser still recognises the utf-8 SVG form (it is common in the wild), but
    `_accept_data_uri` stores only the base64 form — see below."""

    def test_a_base64_image_data_uri_is_taken_as_is(self):
        uri = "data:image/png;base64,iVBORw0KGgo="
        assert probe_module._accept_data_uri(uri) == uri

    def test_a_utf8_svg_data_uri_is_refused(self):
        """It survives into the recovery export, which interpolates it into an
        attribute; a utf-8 SVG data URI can carry a quote and break out (audit
        B3). Only the base64 alphabet is attribute-safe, so only it is stored.
        The cost: a service shipping a utf-8 SVG favicon falls back to none."""
        uri = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        assert probe_module._accept_data_uri(uri) is None

    def test_a_data_uri_survives_intact(self):
        """The real tag from the MCP Memory Service. A ">" inside the value and
        single quotes inside a double-quoted attribute both used to truncate it,
        storing a fragment like `data:image/svg+xml,<svg xmlns=` as the icon."""
        body = (
            b"""<link rel="icon" href="data:image/svg+xml,"""
            b"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'/>">"""
        )
        candidate = probe_module._icon_candidates("http://h/x/", body)[0]
        assert candidate.startswith("data:image/svg+xml,<svg")
        assert candidate.endswith("/>")
        assert "viewBox='0 0 100 100'" in candidate

    def test_rel_is_matched_as_a_whole_word(self):
        body = b'<link rel="stylesheet" href="/iconography.css">'
        assert not any(
            c.endswith("iconography.css")
            for c in probe_module._icon_candidates("http://h/", body)
        )

    def test_a_non_image_data_uri_is_refused(self):
        assert probe_module._accept_data_uri("data:text/html,<script>x</script>") is None

    def test_an_oversized_data_uri_is_refused(self, settings):
        """Inline icons ride in every GET /api/v1/services response, so they are
        capped tighter than file-backed ones."""
        settings.ICON_MAX_INLINE_BYTES = 64
        assert (
            probe_module._accept_data_uri("data:image/png;base64," + "A" * 500)
            is None
        )


class TestUpload:
    def test_an_uploaded_image_is_validated_by_signature(self, settings, tmp_path):
        """The filename and the browser's content type are both client-supplied,
        so neither decides."""
        settings.MEDIA_ROOT = str(tmp_path)
        assert probe_module.store_icon_bytes(PNG, "logo.png").endswith(".png")
        assert probe_module.store_icon_bytes(b"<html>nope</html>", "logo.png") is None

    def test_upload_endpoint_rejects_a_non_image(self, client, admin_headers, settings,
                                                 tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("evil.png", b"<html>", content_type="image/png")
        response = client.post("/api/v1/icon/upload", {"file": upload}, **admin_headers)
        assert response.status_code == 422

    def test_upload_endpoint_stores_a_real_image(self, client, admin_headers, settings,
                                                 tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("logo.png", PNG, content_type="image/png")
        response = client.post("/api/v1/icon/upload", {"file": upload}, **admin_headers)
        assert response.status_code == 200
        assert response.json()["icon_url"].endswith(".png")

    def test_upload_is_admin_only(self, client, agent_headers):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("logo.png", PNG, content_type="image/png")
        response = client.post("/api/v1/icon/upload", {"file": upload}, **agent_headers)
        assert response.status_code == 401


def test_the_manual_override_runs_the_ssrf_validator(client, admin_headers):
    """It is the same act as probing — fetching a user-supplied URL from inside
    the network — so it gets the same address policy."""
    response = client.post(
        "/api/v1/icon",
        data=json.dumps({"url": "http://127.0.0.1/favicon.ico"}),
        content_type="application/json",
        **admin_headers,
    )
    assert response.status_code == 422


def test_the_manual_override_rejects_a_bad_scheme(client, admin_headers):
    response = client.post(
        "/api/v1/icon",
        data=json.dumps({"url": "file:///etc/passwd"}),
        content_type="application/json",
        **admin_headers,
    )
    assert response.status_code == 422


def test_the_manual_override_is_admin_only(client, agent_headers):
    response = client.post(
        "/api/v1/icon",
        data=json.dumps({"url": "http://192.168.1.1/favicon.ico"}),
        content_type="application/json",
        **agent_headers,
    )
    assert response.status_code == 401


def test_the_override_returns_a_local_path_not_the_remote_url(
    client, admin_headers, settings, tmp_path, monkeypatch
):
    """Storing the remote URL would make the catalog page fetch third-party
    images, and the recovery export could not inline it (§9)."""
    settings.MEDIA_ROOT = str(tmp_path)
    monkeypatch.setattr(
        "services.api.fetch_and_store_icon", lambda url: "/media/icons/abc.png"
    )
    response = client.post(
        "/api/v1/icon",
        data=json.dumps({"url": "https://cdn.example.com/logo.png"}),
        content_type="application/json",
        **admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["icon_url"] == "/media/icons/abc.png"
