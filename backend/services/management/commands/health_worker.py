"""`python manage.py health_worker` — spec §8, §3b.

A management command rather than Celery: "46 checks per minute does not justify
a broker. Celery Beat would add Redis plus two processes to a compose file that
other people are meant to run."

Scheduling is the simple loop §8 asks for — wake often, check whichever services
are due by their own `health_interval_s`, run them concurrently.
"""

import asyncio
import logging
import signal

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from django.utils import timezone

from services import notifications
from services.db import releases_connections
from services.export_static import export_is_stale, run_static_export
from services.health.policy import current_policy
from services.health.registry import run_check_bounded
from services.health.transitions import apply_result, is_due, should_notify
from services.heartbeat import HEALTH_WORKER, beat
from services.models import Service

logger = logging.getLogger("services.health_worker")

# Every ORM call below crosses `sync_to_async` onto a thread-pool thread, which
# gets its own connection and nothing to close it — the worker would hold a
# session open for the life of the process. These wrappers are where the worker
# owns that thread boundary; see services/db.py.
_export = releases_connections(run_static_export)


class Command(BaseCommand):
    help = "Run health checks on a loop, honouring each service's interval."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tick", type=float, default=5.0, help="Seconds between scheduling passes."
        )
        parser.add_argument(
            "--concurrency", type=int, default=20, help="Simultaneous checks."
        )
        parser.add_argument(
            "--once", action="store_true", help="Run one pass and exit (for tests)."
        )

    def handle(self, *args, **options):
        asyncio.run(
            _run(
                tick=options["tick"],
                concurrency=options["concurrency"],
                once=options["once"],
            )
        )


async def _run(tick: float, concurrency: int, once: bool):
    stopping = asyncio.Event()
    _install_signal_handlers(stopping)
    semaphore = asyncio.Semaphore(concurrency)
    started_at = timezone.now()

    logger.info("health_worker started (tick=%ss, concurrency=%s)", tick, concurrency)
    while not stopping.is_set():
        try:
            # One read per pass, and a fresh one every pass: change the interval
            # or the thresholds in Settings and the next cycle obeys, with no
            # restart. A policy captured here, above the loop, would satisfy
            # "the worker reads the setting" and still be wrong (§5a).
            policy = await _policy()
            checked = await run_pass(semaphore, policy)
            if checked:
                logger.info("checked %s service(s)", checked)
            # The beat carries the interval this pass actually ran under, which
            # is what makes the deadman's threshold track a settings change.
            # After the work, not before: a beat written before a pass that then
            # hangs would report a worker that is not working.
            await _beat(policy.interval_s, started_at)
            # "retry on the next mutation or worker cycle" (§9).
            if export_is_stale():
                await sync_to_async(_export, thread_sensitive=False)()
        except Exception:  # noqa: BLE001 - a bad pass must not kill the worker
            logger.exception("health pass failed")
        if once:
            return
        try:
            await asyncio.wait_for(stopping.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
    logger.info("health_worker stopped")


async def run_pass(semaphore: asyncio.Semaphore, policy=None) -> int:
    policy = policy or await _policy()
    services = await _due_services(policy)
    if not services:
        return 0
    await asyncio.gather(*(_check_one(s, semaphore, policy) for s in services))
    return len(services)


@sync_to_async
@releases_connections
def _policy():
    return current_policy()


@sync_to_async
@releases_connections
def _beat(interval_s: int, started_at):
    return beat(HEALTH_WORKER, interval_s=interval_s, started_at=started_at)


@sync_to_async
@releases_connections
def _due_services(policy):
    return [
        s
        for s in Service.objects.filter(health_enabled=True)
        if is_due(s, policy=policy)
    ]


async def _check_one(service: Service, semaphore: asyncio.Semaphore, policy):
    async with semaphore:
        try:
            # Bounded so one check can never hold the gathered pass (audit S1):
            # a stalled check would otherwise stall `asyncio.gather`, no later
            # pass or beat runs, and the deadman fires on the worker itself.
            ok = await run_check_bounded(service)
        except Exception:  # noqa: BLE001 - one bad service must not stop the pass
            logger.exception("check failed for %s", service.slug)
            ok = False
    await _save_result(service, ok, policy)


@sync_to_async
@releases_connections
def _save_result(service: Service, ok: bool, policy=None):
    policy = policy or current_policy()
    applied = apply_result(service, ok, policy=policy)
    # update_fields keeps the worker from overwriting a concurrent catalog edit
    # with the stale copy it loaded at the start of the pass.
    service.save(update_fields=applied.changed)
    if policy.discord_enabled and should_notify(service, applied):
        # Dispatched to a daemon thread: §5 requires that a slow or failing
        # webhook never blocks a health cycle, and awaiting an outbound POST
        # inside the pass is exactly how that guarantee gets lost.
        notifications.dispatch(
            notifications.service_transition_message(service, applied)
        )
    return applied


def _install_signal_handlers(stopping: asyncio.Event):
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            # Windows dev boxes: Ctrl-C still raises KeyboardInterrupt.
            signal.signal(sig, lambda *_: stopping.set())
