"""Durable event queue + session store, backed by **PostgreSQL** for multi-node use.

Where :mod:`statecharts.durable` (SQLite) gives single-node **exactly-once** delivery, this
backend gives multi-worker **at-least-once** delivery via a ``FOR UPDATE SKIP LOCKED`` lease.
It is the Postgres port designed in ``docs/design/postgres-durability.md`` (research #6).

The store keeps the same two-table shape as SQLite — ``sessions`` (working memory as JSON) and
``timers`` (the durable mailbox) — and the backend-agnostic methods (:meth:`~PostgresStore.save_session`
/ :meth:`~PostgresStore.load_session` / :meth:`~PostgresStore.session_ids` / :meth:`~PostgresStore.enqueue`
/ :meth:`~PostgresStore.cancel` / :meth:`~PostgresStore.next_due`) behave identically. What differs
is **delivery**: SQLite's peek-deliver-delete cycle is replaced by a lease ``claim``.

Delivery contract — **at-least-once** (RQ2(b)/RQ3 of the design doc)
--------------------------------------------------------------------
Instead of deleting a due timer on claim, a worker marks it ``status='in_flight',
claimed_at=now`` under ``FOR UPDATE SKIP LOCKED`` (:meth:`~PostgresStore.claim`), processes it,
and only :meth:`~PostgresStore.delete_timer` on a successful ``save_session``. A worker that
crashes mid-delivery leaves an in-flight row whose **lease** expires
(``claimed_at <= now - lease``); another worker then re-claims it. Consequences the caller
**must** design for:

1. **Delivery is at-least-once** — the same event may be delivered more than once (a crash
   after the side effect but before ``delete_timer``, or a lease that expired while a slow
   handler was still running, both cause a redelivery). Handlers must be **idempotent** or
   externally deduplicated (SCP-C-015; same requirement the SQLite docstring flags for
   external side effects, here promoted to the default for *all* delivery).
2. **Non-deterministic executable content is re-run, not replayed.** We persist only JSON-able
   working memory + pending timers — there is no deterministic replay ("Absurd" model, not
   Temporal's replay-workflow-code model). I/O, wall-clock reads, and RNG in a guard/action
   re-execute on redelivery.
3. **The unit of progress is ``(process_event -> save_session)``.** Only committed working
   memory survives a restart; an in-flight step that didn't commit is retried from the last
   committed WM.

**Not ported from the SQLite backend** (single-node-specific; see the design doc):

* The exactly-once **dead-letter / poison** machinery (``dead_letter`` / ``recover`` / the
  ``StoreError``-vs-poison classification in :meth:`DurableRuntime.tick`). The lease model
  relies on the visibility timeout + idempotent handlers instead. A poison/dead-letter policy
  for Postgres — a max-attempts cap on the ``attempts`` column kept here for the purpose — is a
  separate follow-up (RQ4).
* The #26 per-session **retry-gate** (``session_gates`` / ``gate_session`` / ``clear_gate``).
  Its multi-worker analogue is a per-session advisory lock (or claim-by-``session_id``), also a
  separate follow-up (RQ4). Absent it, two workers may process one session's events
  concurrently, so **per-session ordering is not guaranteed** across workers.

Driver: `psycopg` v3, an optional dependency (``pip install -e '.[postgres]'``). The default
install stays zero-dependency; importing this module without psycopg raises a clear error.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without the extra
    raise ModuleNotFoundError(
        "PostgresStore requires the 'postgres' extra: pip install -e '.[postgres]' "
        "(installs psycopg[binary])"
    ) from exc

from .durable import (
    DurableRuntime,
    SessionRecord,
    StoreError,
    event_from_jsonable,
    event_to_jsonable,
    wm_from_jsonable,
    wm_to_jsonable,
)
from .event_queue import Clock
from .events import Event
from .working_memory import WorkingMemory

logger = logging.getLogger(__name__)

__all__ = ["PostgresStore", "PostgresRuntime"]


# The mailbox has two extra columns vs. SQLite — ``status`` ('ready' | 'in_flight') and
# ``claimed_at`` — that carry the lease (RQ2(b)). The claim scan is served by a **partial**
# index on ``(due, id) WHERE status='ready'`` (RQ1) so leased/in-flight rows don't bloat it;
# a second partial index keeps the expired-lease reclaim scan cheap.
_PG_DDL = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        chart      TEXT NOT NULL,
        wm         TEXT NOT NULL,
        updated    DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS timers (
        id         BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL,
        due        DOUBLE PRECISION NOT NULL,
        event      TEXT NOT NULL,
        sendid     TEXT,
        attempts   INTEGER NOT NULL DEFAULT 0,
        status     TEXT NOT NULL DEFAULT 'ready',
        claimed_at DOUBLE PRECISION
    )
    """,
    "CREATE INDEX IF NOT EXISTS timers_due_ready ON timers(due, id) WHERE status='ready'",
    "CREATE INDEX IF NOT EXISTS timers_inflight ON timers(claimed_at) WHERE status='in_flight'",
    "CREATE INDEX IF NOT EXISTS timers_session ON timers(session_id)",
)

# One statement: pick the oldest deliverable timer (a fresh 'ready' row, or an 'in_flight' row
# whose lease has expired) under FOR UPDATE SKIP LOCKED so N workers take disjoint rows without
# blocking on a shared head, and mark it in-flight with a fresh lease. RETURNING hands the row
# back. Runs as its own (autocommit) transaction — the lock is held only for this statement.
_CLAIM_SQL = """
UPDATE timers SET status='in_flight', claimed_at=%(now)s
WHERE id = (
    SELECT id FROM timers
    WHERE due <= %(now)s
      AND (status='ready' OR (status='in_flight' AND claimed_at <= %(now)s - %(lease)s))
    ORDER BY due, id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, session_id, due, event, attempts, claimed_at
"""


class PostgresStore:
    """A durable store backed by PostgreSQL with lease-based (at-least-once) claim delivery.

    The backend-agnostic methods match :class:`statecharts.durable.SqliteStore`; delivery is
    driven by :meth:`claim` + :meth:`delete_timer` (see :class:`PostgresRuntime`) rather than the
    SQLite peek/delete cycle. ``schema`` isolates the tables (handy for tests): the store creates
    it and sets ``search_path`` to it. Every psycopg error from the store's own DB operations is
    wrapped in :class:`~statecharts.durable.StoreError` so callers classify infrastructure
    failures by *authority*, exactly as the SQLite store does.
    """

    #: Default visibility timeout (seconds): an in-flight row older than this is re-claimable.
    DEFAULT_LEASE_S = 30.0

    def __init__(self, dsn: str, clock: Optional[Clock] = None, *, schema: Optional[str] = None):
        try:
            self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            if schema is not None:
                # Quote to allow a generated per-test schema name; identifiers can't be bound as
                # parameters, so validate it is a plain identifier to keep this injection-safe.
                if not schema.replace("_", "").isalnum():
                    raise ValueError(f"invalid schema name: {schema!r}")
                self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                self.conn.execute(f'SET search_path TO "{schema}"')
            for stmt in _PG_DDL:
                self.conn.execute(stmt)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc
        self.schema = schema
        self.clock = clock or Clock()

    def close(self) -> None:
        self.conn.close()

    # -- error-translating helpers -----------------------------------------
    def _exec(self, sql: str, params=()):
        """Run a statement (autocommit), wrapping the store's own ``psycopg.Error`` as
        :class:`StoreError` so an infrastructure failure is told apart from a poison one by
        authority, not exception type (mirrors ``SqliteStore._exec``)."""
        try:
            return self.conn.execute(sql, params)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc

    def _query_one(self, sql: str, params=()):
        try:
            return self.conn.execute(sql, params).fetchone()
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc

    def _query_all(self, sql: str, params=()):
        try:
            return self.conn.execute(sql, params).fetchall()
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc

    # -- sessions -----------------------------------------------------------
    def save_session(self, session_id: str, chart: str, wm: WorkingMemory) -> None:
        # json.dumps(wm_to_jsonable(wm)) is evaluated *before* _exec, so an unserializable
        # working memory raises TypeError/ValueError (poison), never StoreError (SCP-C-027).
        payload = json.dumps(wm_to_jsonable(wm))
        self._exec(
            "INSERT INTO sessions(session_id, chart, wm, updated) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(session_id) DO UPDATE SET chart=excluded.chart, "
            "wm=excluded.wm, updated=excluded.updated",
            (session_id, chart, payload, self.clock.now()),
        )

    def load_session(self, session_id: str) -> Optional[SessionRecord]:
        # The SELECT is guarded (a read failure is StoreError = infra); decoding the blob is
        # separate, so a corrupt/undecodable wm raises json/KeyError = poison (SCP-C-028).
        row = self._query_one(
            "SELECT chart, wm FROM sessions WHERE session_id=%s", (session_id,)
        )
        if row is None:
            return None
        return SessionRecord(session_id, row["chart"], wm_from_jsonable(json.loads(row["wm"])))

    def session_ids(self) -> List[str]:
        return [r["session_id"] for r in self._query_all("SELECT session_id FROM sessions")]

    # -- timers (the durable mailbox) --------------------------------------
    def enqueue(self, session_id: str, event: Event, due: float, sendid: Optional[str] = None) -> None:
        self._exec(
            "INSERT INTO timers(session_id, due, event, sendid) VALUES(%s,%s,%s,%s)",
            (session_id, due, json.dumps(event_to_jsonable(event)), sendid),
        )

    def cancel(self, session_id: str, sendid: str) -> None:
        self._exec("DELETE FROM timers WHERE session_id=%s AND sendid=%s", (session_id, sendid))

    def next_due(self) -> Optional[float]:
        """The earliest ``due`` among **ready** timers, or ``None`` if none are ready.

        In-flight (leased) rows are excluded: they are being processed, and a poller waking on
        ``next_due()`` wants the next *fresh* delivery time. A crashed worker's row reappears
        only after its lease expires — reclaiming it is the worker loop's job
        (:meth:`PostgresRuntime.tick`), not this scheduling hint's."""
        row = self._query_one("SELECT MIN(due) AS d FROM timers WHERE status='ready'")
        return row["d"] if row and row["d"] is not None else None

    def claim(self, now: float, lease: Optional[float] = None) -> Optional[dict]:
        """Lease the single oldest deliverable timer, or ``None`` if none is available.

        Deliverable = a ``ready`` row with ``due <= now``, **or** an ``in_flight`` row whose
        lease has expired (``claimed_at <= now - lease`` — a crashed/slow worker's row). The row
        is marked ``in_flight`` with ``claimed_at = now`` and returned. ``FOR UPDATE SKIP LOCKED``
        lets concurrent workers take **disjoint** rows without blocking on a shared queue head.
        The caller processes the event and calls :meth:`delete_timer` on success; if it crashes,
        the lease expires and another worker re-claims (at-least-once)."""
        lease = self.DEFAULT_LEASE_S if lease is None else lease
        return self._query_one(_CLAIM_SQL, {"now": now, "lease": lease})

    def delete_timer(self, timer_id: int) -> None:
        self._exec("DELETE FROM timers WHERE id=%s", (timer_id,))

    def ready_count(self) -> int:
        """Number of timers currently in the ``ready`` state (test/introspection helper)."""
        row = self._query_one("SELECT COUNT(*) AS n FROM timers WHERE status='ready'")
        return int(row["n"]) if row else 0


class PostgresRuntime(DurableRuntime):
    """At-least-once delivery over :class:`PostgresStore`.

    Reuses :class:`~statecharts.durable.DurableRuntime` for the backend-agnostic parts
    (:meth:`~DurableRuntime.start` / :meth:`~DurableRuntime.enqueue` / :meth:`~DurableRuntime.load`
    / ``_deliver`` — all of which use only ported store methods) and replaces
    :meth:`~DurableRuntime.tick` with a lease **claim** loop. There is no dead-letter cap and no
    per-session gate here (see the module docstring): a failed delivery simply leaves its row
    in-flight to be re-claimed after the lease, giving at-least-once retry.
    """

    def tick(self, now: Optional[float] = None, max_steps: int = 10_000,
             lease: Optional[float] = None) -> int:
        """Claim and deliver every currently-deliverable timer, returning the count delivered.

        Each iteration claims one row (marking it in-flight), delivers it, and deletes it on
        success. On a **poison** failure (bad handler/data) or an undecodable payload the row is
        left in-flight — its lease will expire and a later tick re-claims it (at-least-once; no
        dead-letter in this model). On a :class:`StoreError` (infrastructure) the tick stops and
        the caller re-polls, exactly as the SQLite runtime does. A row just claimed keeps a fresh
        lease, so it is not re-claimed within the same tick — the loop advances to the next
        ready row and terminates when :meth:`PostgresStore.claim` returns ``None``."""
        delivered = 0
        for _ in range(max_steps):
            t = self.store.clock.now() if now is None else now
            try:
                row = self.store.claim(t, lease)
            except StoreError as exc:
                logger.warning("durable(pg): store unavailable, will retry on the next tick: %s", exc)
                break
            if row is None:
                break

            timer_id = row["id"]
            session_id = row["session_id"]
            try:
                event = event_from_jsonable(json.loads(row["event"]))
                status = self._deliver(session_id, event)
            except StoreError as exc:
                # Infrastructure: leave the row in-flight (its lease will let it be reclaimed),
                # stop this tick, let the caller re-poll. Never dead-lettered, never raised.
                logger.warning("durable(pg): store unavailable, will retry on the next tick: %s", exc)
                break
            except Exception as exc:  # noqa: BLE001 — poison (handler / data / decode / unknown)
                # At-least-once: no dead-letter cap in this backend. Leave the row in-flight so
                # its lease expiry re-delivers it. A permanently-poison event therefore retries on
                # the lease interval indefinitely (a dead-letter policy is a follow-up, RQ4).
                logger.warning(
                    "durable(pg): delivery failed for session %r (id=%s), will retry after lease: %s",
                    session_id, timer_id, exc,
                )
                continue

            # Success (or a moot event for a stopped session): the timer's work is done, delete it.
            self.store.delete_timer(timer_id)
            if status == "delivered":
                delivered += 1
            elif status == "dropped":
                logger.debug("durable(pg): dropped event for stopped session %r", session_id)
        return delivered
