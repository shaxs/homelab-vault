"""The global half of the health policy — v1 §5, §5a, §6a.

Four values live in the `settings` table and are consumed here: the default poll
interval, the fail threshold, the recovery threshold, and whether Discord is on.
V1.2 built the table and named V1.3 as their consumer; this module is that
consumer, and standing rule 2 is why every one of them has a test proving the
wire carries rather than merely that the screen saves.

**Read live, never snapshotted.** `current_policy()` opens the table on every
call. A value captured at process start satisfies "the worker reads the setting"
and is still wrong: change the interval and nothing happens until a restart,
which is the silent-wrongness §5a exists to prevent (and, per §5a's 2026-08-03
note, arriving through the door §5a nailed shut). One query, not four — the
worker calls this once per pass and the API once per response, so the cost is
paid per cycle rather than per service.

**Per-service values pin; null follows the global.** `health_interval_s` and
`health_fail_threshold` are nullable as of V1.3, and null is the *default* state
for a new service. `interval_for()`/`fail_threshold_for()` are the only place
that rule is written down, so a caller cannot accidentally read the raw column
and get `None`.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HealthPolicy:
    """A live read of the settings the health subsystem obeys."""

    interval_s: int
    fail_threshold: int
    recovery_threshold: int
    discord_enabled: bool

    def interval_for(self, service) -> int:
        """Seconds between checks for this service — its own, or the global."""
        if service.health_interval_s is not None:
            return max(service.health_interval_s, 1)
        return max(self.interval_s, 1)

    def fail_threshold_for(self, service) -> int:
        """Consecutive failures before `down` — its own, or the global."""
        if service.health_fail_threshold is not None:
            return max(service.health_fail_threshold, 1)
        return max(self.fail_threshold, 1)

    def recovery_threshold_for(self, service) -> int:
        """Consecutive successes before `up`.

        Global only, deliberately: §5 asks for damping symmetry, not for a second
        per-service column. `services` gained no recovery column, so there is
        nothing here to pin — if that changes, this is the one method to edit.
        """
        return max(self.recovery_threshold, 1)


def current_policy() -> HealthPolicy:
    """Read the four values from the settings table. One query, every call."""
    from ..settings_store import all_settings

    values = all_settings()
    return HealthPolicy(
        interval_s=values["health.default_interval_s"],
        fail_threshold=values["health.fail_threshold"],
        recovery_threshold=values["health.recovery_threshold"],
        discord_enabled=values["notifications.discord.enabled"],
    )
