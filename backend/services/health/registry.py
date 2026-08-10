"""Check-type registry — spec §8.

"Check types are a registry (type -> checker) from day one. v0 ships `http`;
`tcp` is a few lines on top of the same registry."

The registry key comes from the probe URL's scheme (see Service.check_type), so
adding a type needs no migration.
"""

import asyncio
from typing import Awaitable, Callable

#: type -> async checker. A checker returns True when the service is alive.
CHECKERS: dict[str, Callable[..., Awaitable[bool]]] = {}


def register(name: str):
    def decorator(func):
        CHECKERS[name] = func
        return func

    return decorator


async def run_check(service) -> bool:
    checker = CHECKERS.get(service.check_type)
    if checker is None:
        return False
    return await checker(service)


async def run_check_bounded(service, ceiling_s: float | None = None) -> bool:
    """`run_check` under a hard wall-clock ceiling (audit S1).

    The per-phase timeout the checkers pass to httpx is not a wall-clock bound:
    connect, read and write each get the full value, and a server dribbling one
    byte per read keeps the request alive indefinitely. Both schedulers — the
    worker's gathered pass and the request-thread recheck — use this wrapper so
    no single check can hold them past the model's own maximum, whatever a
    checker or a hostile peer does.
    """
    if ceiling_s is None:
        from services.models import Service

        ceiling_s = Service.MAX_TIMEOUT_MS / 1000 + 5
    try:
        return await asyncio.wait_for(run_check(service), timeout=ceiling_s)
    except asyncio.TimeoutError:
        return False


from . import http as _http  # noqa: E402,F401  (registers "http")
from . import tcp as _tcp  # noqa: E402,F401  (registers "tcp")
