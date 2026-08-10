"""Repair scheme-less URL rows ahead of write-time scheme validation.

The same release that applies this migration starts refusing URL values
without an explicit `http://`/`https://` scheme (`Service.clean()`, operator
ruling 2026-08-10). Rows written before that rule are repaired here rather
than grandfathered, for two reasons:

1. They are **already broken**. A digit-led `192.0.2.10:8096` parses as a
   relative path, so clicking it lands on an in-app 404; a `host.name:8096`
   parses `host.name` as its URL *scheme*, so the frontend renders dead text.
   Prepending `http://` is a repair of data that never worked, not a mutation
   of data that did.
2. A grandfathered row would be un-editable: `clean()` runs on every PATCH,
   so an unrelated edit to such a row would 422 on a field the caller never
   touched.

Only values shaped `host[:port][/path]` are repaired, and only when the
result actually parses as an http URL with a host. Anything else — a
`javascript:` scheme, say — is left alone: it was never navigable, every sink
refuses it, and the next edit of that row gets a 422 naming the field and the
fix. An `icon_url` that matches none of the accepted shapes (http(s), base64
`data:image/…`, the app's own `media/icons/…` cache path) is cleared instead:
no sink has ever rendered it, and unlike `launch_url` the column is nullable.

Constants are frozen here rather than read from live settings (the migration
0003 lesson, audit S17): `/media/` is today's served MEDIA_URL (Django
prefixes the relative setting with a slash), and this migration must keep
repairing by the rules that existed when it shipped.

Reverse is a no-op: the forward pass cannot record which rows it touched
without a bookkeeping column, and un-prepending schemes by guesswork would
corrupt legitimate rows.
"""

import re
from urllib.parse import urlsplit

from django.db import migrations

_URL_FIELDS = ("launch_url", "launch_url_external", "repo_url", "docs_url",
               "health_url")

#: The only shape the repair applies to: bare host, optional numeric port,
#: optional path. Everything else is unrepairable garbage, not a typo.
_REPAIRABLE = re.compile(r"^[A-Za-z0-9._\-]+(:\d+)?(/.*)?$")

#: Frozen copies of the write-path rules (see module docstring).
_DATA_IMAGE = re.compile(r"^data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=]*$",
                         re.IGNORECASE)
_MEDIA_ICON = re.compile(r"^/media/icons/[A-Za-z0-9._-]+$")


def _is_http(value):
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _is_tcp(value):
    parts = urlsplit(value)
    return parts.scheme == "tcp" and bool(parts.netloc)


def _acceptable_icon(value):
    return bool(
        _is_http(value) or _DATA_IMAGE.match(value) or _MEDIA_ICON.match(value)
    )


def repair(apps, schema_editor):
    Service = apps.get_model("services", "Service")
    for service in Service.objects.all().iterator():
        changed = []
        for field in _URL_FIELDS:
            value = getattr(service, field)
            if not value or _is_http(value):
                continue
            if field == "health_url" and _is_tcp(value):
                continue
            if _REPAIRABLE.match(value):
                candidate = "http://" + value
                if _is_http(candidate):
                    setattr(service, field, candidate)
                    changed.append(field)
        if service.icon_url and not _acceptable_icon(service.icon_url):
            service.icon_url = None
            changed.append("icon_url")
        if changed:
            service.save(update_fields=changed)


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0006_link_misses_and_export_state"),
    ]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]
