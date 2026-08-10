"""The worker heartbeat contract — v1 §5a.

**This module is V1.3's product and V1.4's input** (standing rule 2: the producer
names its consumer, the consumer proves the wire). V1.4 registers the Proxmox
worker by calling `beat("proxmox", interval_s=...)` once per cycle and changes
nothing else — no deadman edit, no new evaluator branch, no second table.

The contract, in three sentences:

1. **A worker registers by beating.** There is no separate registration step;
   `beat()` upserts, so a worker that has never run has no row and a worker that
   ran once and died has a stale one. Absence and staleness are different
   states and the evaluator treats them differently — a worker that never
   started is not a worker that stopped.
2. **A cycle records the time, a counter, and the worker's current cadence.**
   `interval_s` is re-read from settings by the worker on every beat, so the
   deadman's threshold tracks a settings change without anyone restarting
   anything, and the evaluator never has to know which settings key drives which
   worker.
3. **The evaluator reads rows, never processes.** Which is what makes it able to
   run somewhere else entirely — §5a's whole point being that a dead process
   cannot report its own death.
"""

from django.db import transaction
from django.utils import timezone

from .models import WorkerHeartbeat

#: The health worker's key.
HEALTH_WORKER = "health"
#: The Proxmox worker's key (V1.4). Registering it required **only this line and
#: one `beat()` call** — no deadman edit, no new evaluator branch, no second
#: table — which is the contract in the module docstring being kept rather than
#: merely asserted.
PROXMOX_WORKER = "proxmox"


@transaction.atomic
def beat(name: str, interval_s: int, now=None, started_at=None) -> WorkerHeartbeat:
    """Record one completed cycle for `name`.

    `started_at` is written only when the row is created or when the caller says
    the process restarted; the counter resets with it, so `cycles` means "cycles
    since this process started" rather than "since the beginning of time".
    """
    now = now or timezone.now()
    row = WorkerHeartbeat.objects.select_for_update().filter(name=name).first()
    if row is None:
        return WorkerHeartbeat.objects.create(
            name=name,
            interval_s=interval_s,
            started_at=started_at or now,
            last_beat_at=now,
            cycles=1,
        )
    row.interval_s = interval_s
    row.last_beat_at = now
    row.cycles += 1
    fields = ["interval_s", "last_beat_at", "cycles"]
    if started_at is not None and started_at != row.started_at:
        row.started_at = started_at
        row.cycles = 1
        fields += ["started_at"]
    row.save(update_fields=fields)
    return row


def freshness(now=None) -> list[dict]:
    """Per-worker freshness, as `/api/v1/healthz` and the deadman both see it.

    `stale_after_s` is computed from the interval the worker last declared —
    never from a constant, and never from a value this process read at startup.
    """
    now = now or timezone.now()
    rows = []
    for row in WorkerHeartbeat.objects.all():
        age = (now - row.last_beat_at).total_seconds()
        rows.append(
            {
                "name": row.name,
                "state": row.state,
                "interval_s": row.interval_s,
                "stale_after_s": row.stale_after_s,
                "age_s": int(age),
                "stale": age > row.stale_after_s,
                "started_at": row.started_at,
                "last_beat_at": row.last_beat_at,
                "cycles": row.cycles,
            }
        )
    return rows
