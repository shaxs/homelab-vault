"""The deadman — v1 §5a.

**What it catches.** The health worker dies at 02:00. The web app keeps serving.
No checks run, so every status freezes at its last value, which is almost
certainly *up*. Notifications fire on transitions and there are no transitions,
because there are no checks. Nextcloud dies at 04:00 and nothing is said while
the dashboard shows healthy throughout.

Absence of a signal is itself the signal, and **a dead process cannot report its
own death** — so this never runs inside the worker it watches. Two doors, both
outside it: `manage.py deadman` (cron), and evaluation on the `/api/v1/healthz`
read, which means the external watcher §11.5 asks for also *drives* the internal
layer rather than merely observing it.

**Per worker, not global** (§5a). The registry is rows keyed by worker name, so
one dead worker is never masked by another being alive. That isolation is proven
here with two fixture heartbeats — the integration proof, stopping two real
processes, belongs to V1.4 because the second worker is V1.4's.

**Edge-triggered.** `state` moves ok -> stalled -> ok and only a *move* sends a
message. Evaluating every interval and alerting every evaluation would post
several hundred times overnight, which is indistinguishable from not alerting at
all.
"""

import logging

from django.db import transaction
from django.utils import timezone

from . import notifications
from .heartbeat import freshness
from .health.policy import current_policy
from .models import WorkerHeartbeat

logger = logging.getLogger("services.deadman")


def evaluate(now=None, notify=None, policy=None) -> list[dict]:
    """One evaluation pass. Returns the transitions it announced.

    Safe to call as often as you like — that is what edge-triggering buys. The
    `notify` seam takes a callable so a test can assert on messages without a
    network stub; production passes nothing and gets Discord.
    """
    now = now or timezone.now()
    policy = policy or current_policy()
    send = notify if notify is not None else _dispatch_if_enabled(policy)

    transitions = []
    # Names first, then one locked row at a time. Two evaluators can run at once
    # — a cron pass and a `/healthz` request — and the lock is what stops both
    # announcing the same stall. `select_for_update` needs a transaction, which
    # is why the lock lives in `_evaluate_one` rather than around this loop.
    for name in list(
        WorkerHeartbeat.objects.values_list("name", flat=True).order_by("name")
    ):
        moved = _evaluate_one(name, now=now, send=send)
        if moved is not None:
            transitions.append(moved)
    return transitions


@transaction.atomic
def _evaluate_one(name: str, now, send) -> dict | None:
    # `skip_locked` rather than a plain lock: `/api/v1/healthz` is unauthenticated
    # and evaluates on every read, so without it a watcher polling every few
    # seconds — or anyone hammering the endpoint — queues requests behind each
    # other on a row lock. Skipping means "another evaluator has this one", which
    # is both cheaper and the correct answer: it is already being handled.
    row = (
        WorkerHeartbeat.objects.select_for_update(skip_locked=True)
        .filter(name=name)
        .first()
    )
    if row is None:  # locked by another evaluator, or deleted between queries
        return None
    age = (now - row.last_beat_at).total_seconds()
    stale = age > row.stale_after_s

    if stale and row.state != WorkerHeartbeat.State.STALLED:
        row.state = WorkerHeartbeat.State.STALLED
        row.stalled_since = now
        row.alerted_at = now
        row.save(update_fields=["state", "stalled_since", "alerted_at"])
        logger.warning(
            "worker %s has stopped reporting: %ss since last cycle (threshold %ss)",
            row.name,
            int(age),
            row.stale_after_s,
        )
        send(notifications.worker_stall_message(row.name, row.stale_after_s, age))
        return {"worker": row.name, "transition": "stalled", "age_s": int(age)}

    if not stale and row.state == WorkerHeartbeat.State.STALLED:
        row.state = WorkerHeartbeat.State.OK
        row.stalled_since = None
        row.alerted_at = now
        row.save(update_fields=["state", "stalled_since", "alerted_at"])
        logger.info("worker %s is reporting again", row.name)
        send(notifications.worker_recovery_message(row.name))
        return {"worker": row.name, "transition": "recovered", "age_s": int(age)}

    return None


def _dispatch_if_enabled(policy):
    """Discord, if the settings table says so. Read live, per pass."""

    def send(content: str) -> None:
        if not policy.discord_enabled:
            return
        notifications.dispatch(content)

    return send


def status(now=None) -> dict:
    """What `/api/v1/healthz` reports, after an evaluation pass.

    `ok` is false when any registered worker is stale. A worker that has never
    registered is not listed at all — it cannot be distinguished from one that
    was never deployed, and inventing a failure for a worker nobody runs would
    make the endpoint useless as a watcher target.
    """
    now = now or timezone.now()
    workers = freshness(now=now)
    return {
        "ok": all(not worker["stale"] for worker in workers),
        "workers": workers,
        "checked_at": now,
    }
