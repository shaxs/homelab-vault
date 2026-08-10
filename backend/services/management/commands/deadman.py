"""`python manage.py deadman` — v1 §5a's internal layer.

Cron this on the host, or hit `/api/v1/healthz` from the external watcher; both
run the same evaluation. What matters is only that it runs **outside** the worker
it watches, which a management command in the `web` container does and a task
inside `health_worker` fundamentally cannot.

`--loop` exists for a deployment with nowhere to put a cron entry: it is the same
pass on a timer, in its own process. Still outside the health worker, so the
guarantee holds.
"""

import time

from django.core.management.base import BaseCommand

from services.deadman import evaluate, status


class Command(BaseCommand):
    help = "Evaluate worker heartbeats and alert on stalls (v1 §5a)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Keep evaluating instead of exiting after one pass.",
        )
        parser.add_argument(
            "--every",
            type=float,
            default=30.0,
            help="Seconds between passes when --loop is given.",
        )

    def handle(self, *args, **options):
        if not options["loop"]:
            self._pass()
            return
        while True:
            self._pass()
            time.sleep(options["every"])

    def _pass(self):
        transitions = evaluate()
        for moved in transitions:
            self.stdout.write(
                f"{moved['worker']}: {moved['transition']} "
                f"({moved['age_s']}s since last cycle)"
            )
        report = status()
        if not transitions:
            state = "ok" if report["ok"] else "STALLED"
            workers = len(report["workers"])
            self.stdout.write(f"{state} — {workers} worker(s) registered")
