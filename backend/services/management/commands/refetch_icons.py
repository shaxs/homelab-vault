"""`python manage.py refetch_icons` — re-discover and re-cache service icons.

Fixing icon fetching does not repair records that already point at a bad file,
so this is the repair path. It is also the way to pick up an icon for services
that were catalogued while they were down.

    python manage.py refetch_icons                 # every service missing an icon
    python manage.py refetch_icons --all           # every service
    python manage.py refetch_icons --slug jellyfin # just one
    python manage.py refetch_icons --all --prune   # and delete unreferenced files
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from services.export_static import schedule_static_export
from services.models import Service
from services.probe import probe_url


class Command(BaseCommand):
    help = "Re-fetch cached icons for catalogued services."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="Only this service.")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include services that already have an icon.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete cached icon files no service references.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report without writing."
        )

    def handle(self, *args, **options):
        services = Service.objects.all()
        if options["slug"]:
            services = services.filter(slug=options["slug"])
        elif not options["all"]:
            services = services.filter(icon_url__isnull=True) | services.filter(
                icon_url=""
            )

        changed = 0
        for service in services:
            before = service.icon_url
            result = probe_url(service.probe_url)
            after = result.icon_url

            if not after:
                self.stdout.write(f"  {service.slug}: no icon found")
                continue
            if after == before:
                self.stdout.write(f"  {service.slug}: unchanged")
                continue

            self.stdout.write(
                self.style.SUCCESS(f"  {service.slug}: {before or '(none)'} -> {after}")
            )
            if not options["dry_run"]:
                service.icon_url = after
                service.save(update_fields=["icon_url", "updated_at"])
            changed += 1

        if options["prune"] and not options["dry_run"]:
            self._prune()

        if changed and not options["dry_run"]:
            # The recovery export inlines icons, so it is now out of date.
            schedule_static_export()
        self.stdout.write(f"{changed} icon(s) updated.")

    def _prune(self):
        icons_dir = Path(settings.MEDIA_ROOT) / "icons"
        if not icons_dir.is_dir():
            return
        referenced = {
            Path(url).name
            for url in Service.objects.exclude(icon_url__isnull=True)
            .exclude(icon_url="")
            .values_list("icon_url", flat=True)
        }
        for path in icons_dir.iterdir():
            if path.is_file() and path.name not in referenced:
                path.unlink()
                self.stdout.write(f"  pruned {path.name}")
