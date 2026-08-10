"""Database-connection hygiene for code that runs off the request path.

Django hands a request's connections back for it — `request_finished` fires
`close_old_connections`. Nothing does that for a thread we start ourselves, so
any worker thread that touches the ORM opens a connection that stays open until
the process exits. Under pytest that is what leaves a session on
`test_homelabvault_dev` and makes teardown fail with *"database is being
accessed by other users"*, orphaning the database and breaking the **next** run.

The rule this module encodes: **connection hygiene belongs to whoever owns the
thread, never to the library code running on it.**

`run_static_export()` broke that rule. It called `close_old_connections()`
itself, and called synchronously inside a transaction it did not open — which is
what a test does under pytest-django's atomic wrapper —
`close_if_unusable_or_obsolete()` compares `get_autocommit()` (`False`, because
we are in an atomic block) against `settings_dict["AUTOCOMMIT"]` (`True`),
concludes the connection is unusable, and closes it. `BaseDatabaseWrapper.close()`
then takes its in-atomic-block branch: it sets `closed_in_transaction = True` and
*deliberately does not* set `self.connection = None`. `ensure_connection()`
therefore sees a non-`None` connection and never reconnects, and the next query
raises `OperationalError: the connection is closed`.

SQLite could expose neither bug. Its test database is in-memory and that
backend's `_close()` refuses to close an in-memory database, so the close request
was silently discarded — those were not passing tests, they were unrunnable ones.
"""

from __future__ import annotations

import functools

from django.db import connections


def close_this_threads_connections() -> None:
    """Release every database connection this thread opened.

    Any connection currently inside an atomic block is left alone: that
    transaction belongs to a caller further up the stack, and closing it is the
    bug described above. A thread whose lifecycle we own is never legitimately
    mid-transaction once its work is finished, so skipping is the correct branch
    rather than a compromise.
    """
    for connection in connections.all(initialized_only=True):
        if connection.in_atomic_block:
            continue
        connection.close()


def releases_connections(fn):
    """Mark a callable as owning its thread's connection lifecycle.

    Wrap the **entry point** of a thread — a `threading.Thread` target, or the
    function handed to `sync_to_async` — never an inner helper, which may be
    running inside a transaction somebody else opened.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            close_this_threads_connections()

    return wrapper
