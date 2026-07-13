"""Durable event queue + session store, backed by SQLite (stdlib, file-based, free).

This makes the "long-lived session" story real: a workflow can wait for hours or
days across process restarts.  Two things are persisted:

* **sessions** — each session's :class:`WorkingMemory` as JSON, keyed by session id.
* **timers** — the durable mailbox: pending (delayed) events with absolute due times.

Charts are *code* (their guards/actions are Python callables), so they are registered
by name in a :class:`ChartRegistry` rather than persisted; only JSON-able working
memory and events go in the database.

Scope: SQLite gives durability and safe multi-process access on a single machine.
The schema/queries port to Postgres for true multi-node distribution: the Postgres
port replaces the peek-deliver-delete cycle with a ``SELECT ... FOR UPDATE SKIP LOCKED``
lease (see ``docs/design/postgres-durability.md``).

Delivery contract: :meth:`DurableRuntime.tick` delivers **one event per transaction** —
claiming a due timer, persisting the resulting working memory, and any cascade ``<send>``
it enqueues all run inside a single :meth:`SqliteStore.atomic` transaction. A crash/error
during delivery rolls that event back (so it is redelivered on the next tick and takes
effect **exactly once** — no lost or double-applied events; bug #21), while events already
delivered in the same tick keep their committed progress (independent sessions are
isolated — one failing event never reverts another's). The multi-worker Postgres port instead
uses a lease/visibility-timeout for **at-least-once** delivery (which then requires
idempotent handlers) — see ``docs/design/postgres-durability.md``.

**Per-session ordering under failure is not guaranteed.** Events are delivered oldest-due
first, but when a session's event fails and is backed off, that session's *later* events are
not held behind it — they may be delivered while it waits to retry. This is the deliberate #24
tradeoff (isolate other sessions rather than block the whole queue on one). Strict per-session
FIFO under failure is deferred to its own ticket (#26 — a session-level retry gate).

**"Exactly once" covers working memory, not external side effects.** The atomicity above is
about the persisted state (working memory + timers). If a guard/action performs an *external*
side effect (an HTTP call, a notification) and then a later step fails, the working-memory
write is rolled back but the side effect is not — and a poison retry re-runs the handler. So
side-effecting executable content must be **idempotent** or externally deduplicated (SCP-C-015;
same requirement as the Postgres at-least-once port).

Poison events: delivery failures are classified by *where* they occur, not by exception type
(a handler can raise the same types the store does).

* A **handler** failure — the chart logic (``_run`` / ``process_event``) raising while
  processing the event — is *poison*: the timer's attempt count is incremented and its ``due``
  is pushed into the future (exponential **backoff**), so a briefly-unavailable dependency gets
  a real spaced retry window and the failing event can't head-of-line-block newer events. A
  warning is logged on *every* attempt. After ``DurableRuntime.DEAD_LETTER_CAP`` (default 5)
  failed attempts the event is moved to the ``dead_letters`` table — never silently dropped. An
  event whose stored payload can't be decoded at all is unrecoverable and dead-lettered at once.
* A **store/infrastructure** failure — loading or persisting the session raising (disk-full /
  db-locked / I/O, of any exception type) — is *not* poison: it propagates out of
  :meth:`DurableRuntime.tick` and rolls back, so a healthy event caught in an infrastructure
  outage is retried indefinitely and never dead-lettered (SCP-C-013 / SCP-C-019 / SCP-C-023).
* A ``BaseException`` (``SystemExit``/``KeyboardInterrupt``) is treated as a *crash*: it
  propagates, the :meth:`SqliteStore.atomic` block rolls back, and **no** attempt is burned —
  so the event is redelivered and applied exactly once (bug #21), never mistaken for poison.

Note a deliberate consequence: a *healthy* event that keeps failing for a non-payload,
non-infrastructure reason (e.g. a datamodel value that is never JSON-serializable, or a
deterministic bug in a guard/action) is parked after the cap **by design** — it does not fail
loud. It is preserved in ``dead_letters`` with a per-attempt warning, not lost, so the
operator signal is the WARNING logs plus the queryable ``dead_letters`` table rather than a
raised exception (SCP-C-012). Inspect parked events with :meth:`SqliteStore.dead_letters`.

Limitation: active child ``<invoke>`` sessions are not persisted (they live only in
an in-process run); durable sessions are for the persist-between-external-events use
case, where data is JSON-serializable.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional

from .algorithm import initialize, process_event
from .chart import Chart, make_chart
from .elements import StateNode
from .environment import make_env
from .event_queue import Clock
from .events import Event, coerce_event
from .working_memory import WorkingMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# (De)serialization
# ---------------------------------------------------------------------------


def wm_to_jsonable(wm: WorkingMemory) -> dict:
    if wm.invocations:
        raise ValueError("cannot persist a session with active <invoke> children")
    return {
        "configuration": sorted(wm.configuration),
        "datamodel": wm.datamodel,
        "history_value": {k: sorted(v) for k, v in wm.history_value.items()},
        "running": wm.running,
        "initialized": wm.initialized,
    }


def wm_from_jsonable(d: dict) -> WorkingMemory:
    return WorkingMemory(
        configuration=frozenset(d["configuration"]),
        datamodel=d["datamodel"],
        history_value={k: frozenset(v) for k, v in d["history_value"].items()},
        running=d["running"],
        initialized=d["initialized"],
    )


def event_to_jsonable(ev: Event) -> dict:
    return {
        "name": ev.name, "data": ev.data, "type": ev.type, "sendid": ev.sendid,
        "origin": ev.origin, "origintype": ev.origintype, "invokeid": ev.invokeid,
    }


def event_from_jsonable(d: dict) -> Event:
    return Event(
        name=d["name"], data=d.get("data"), type=d.get("type", "external"),
        sendid=d.get("sendid"), origin=d.get("origin"),
        origintype=d.get("origintype"), invokeid=d.get("invokeid"),
    )


# ---------------------------------------------------------------------------
# Chart registry
# ---------------------------------------------------------------------------


class ChartRegistry:
    """Name -> Chart. Durable sessions reference their chart by this name."""

    def __init__(self):
        self._charts: Dict[str, Chart] = {}

    def register(self, name: str, chart) -> "ChartRegistry":
        if isinstance(chart, StateNode):
            chart = make_chart(chart)
        if not isinstance(chart, Chart):
            raise TypeError("chart must be a StateNode or Chart")
        self._charts[name] = chart
        return self

    def get(self, name: str) -> Chart:
        return self._charts[name]


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    chart      TEXT NOT NULL,
    wm         TEXT NOT NULL,
    updated    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS timers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    due        REAL NOT NULL,
    event      TEXT NOT NULL,
    sendid     TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS timers_due ON timers(due);
CREATE INDEX IF NOT EXISTS timers_session ON timers(session_id);
CREATE TABLE IF NOT EXISTS dead_letters (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    due        REAL NOT NULL,
    event      TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    last_error TEXT,
    dead_at    REAL NOT NULL
);
"""


@dataclass
class SessionRecord:
    session_id: str
    chart: str
    wm: WorkingMemory


class SqliteStore:
    def __init__(self, path: str = ":memory:", clock: Optional[Clock] = None):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # Wait (rather than fail fast) for a concurrent writer's lock — so the migration below
        # and normal writes block on the lock instead of raising "database is locked" under the
        # documented multi-process use (SCP-C-022).
        self.conn.execute("PRAGMA busy_timeout=5000")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")  # durability + concurrent readers
        self.conn.executescript(_SCHEMA)
        # Migrate a pre-existing timers table that lacks the `attempts` column
        # (CREATE TABLE IF NOT EXISTS won't add a column to an existing table). Guard against
        # a start-up race: two processes opening an old DB at once can both see the column
        # missing and both ALTER. With busy_timeout set above, the loser's ALTER blocks until
        # the winner commits and then fails with "duplicate column name" (not "database is
        # locked", SCP-C-022); either way, tolerate the error once the column is present rather
        # than crashing in __init__ (SCP-C-016).
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(timers)")}
        if "attempts" not in cols:
            try:
                self.conn.execute("ALTER TABLE timers ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(timers)")}
                if "attempts" not in cols:
                    raise  # a real failure, not the concurrent-migration race
        self.conn.commit()
        self.clock = clock or Clock()
        self._depth = 0  # >0 while inside atomic(): per-call commits are deferred
        self._sp = 0     # savepoint nesting counter (unique names)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def atomic(self):
        """A write transaction that batches the otherwise per-call commits of
        ``save_session`` / ``enqueue`` / ``cancel`` / ``defer`` into one unit, so a
        crash mid-batch rolls the whole thing back. This is what makes delivery atomic:
        claiming a timer, persisting the resulting working memory, and any cascade
        ``<send>`` it enqueues all commit together, or not at all (bug #21). Nesting is
        depth-counted — only the outermost block begins and commits/rolls back."""
        outer = self._depth == 0
        if outer:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            if outer:
                self.conn.rollback()
            raise
        else:
            if outer:
                self.conn.commit()
        finally:
            # Always restore depth, even if commit/rollback raised; `outer` (captured at
            # entry) — not a re-read of _depth — decides who commits, so the count can't
            # drift on nested or failing blocks.
            self._depth -= 1

    def _commit(self) -> None:
        """Commit now, unless we're inside an ``atomic()`` block (then defer to it)."""
        if self._depth == 0:
            self.conn.commit()

    @contextmanager
    def savepoint(self):
        """A nested rollback point *inside* an ``atomic()`` block. On error it undoes only
        this block's writes (not the whole transaction), so the caller can then commit
        compensating changes. Used to discard a failed delivery's working-memory write
        while still recording the attempt / dead-lettering it (#24)."""
        name = f"sp{self._sp}"
        self._sp += 1
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self.conn.execute(f"RELEASE {name}")
        finally:
            self._sp -= 1

    # -- sessions -----------------------------------------------------------
    def save_session(self, session_id: str, chart: str, wm: WorkingMemory) -> None:
        self.conn.execute(
            "INSERT INTO sessions(session_id, chart, wm, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET chart=excluded.chart, "
            "wm=excluded.wm, updated=excluded.updated",
            (session_id, chart, json.dumps(wm_to_jsonable(wm)), self.clock.now()),
        )
        self._commit()

    def load_session(self, session_id: str) -> Optional[SessionRecord]:
        row = self.conn.execute(
            "SELECT chart, wm FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return SessionRecord(session_id, row["chart"], wm_from_jsonable(json.loads(row["wm"])))

    def session_ids(self) -> List[str]:
        return [r["session_id"] for r in self.conn.execute("SELECT session_id FROM sessions")]

    # -- timers (the durable mailbox) --------------------------------------
    def enqueue(self, session_id: str, event: Event, due: float, sendid: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO timers(session_id, due, event, sendid) VALUES(?,?,?,?)",
            (session_id, due, json.dumps(event_to_jsonable(event)), sendid),
        )
        self._commit()

    def cancel(self, session_id: str, sendid: str) -> None:
        self.conn.execute(
            "DELETE FROM timers WHERE session_id=? AND sendid=?", (session_id, sendid)
        )
        self._commit()

    def next_due(self) -> Optional[float]:
        row = self.conn.execute("SELECT MIN(due) AS d FROM timers").fetchone()
        return row["d"] if row and row["d"] is not None else None

    def peek_one_due(self, now: float) -> Optional[sqlite3.Row]:
        """The single oldest due timer (``due <= now``), **without** deleting it. Returns a
        row (``id, session_id, due, event, attempts``) or ``None``. Delivery
        (:meth:`DurableRuntime.tick`) peeks, delivers, and deletes on success in one
        transaction — so a failed delivery can be recorded (backoff / dead-letter) instead of
        silently vanishing. A failed event is rescheduled into the future (:meth:`defer`), so
        it is no longer ``due`` this tick and can't be re-peeked — no exclusion set needed."""
        return self.conn.execute(
            "SELECT id, session_id, due, event, attempts FROM timers "
            "WHERE due<=? ORDER BY due, id LIMIT 1",
            (now,),
        ).fetchone()

    def delete_timer(self, timer_id: int) -> None:
        self.conn.execute("DELETE FROM timers WHERE id=?", (timer_id,))
        self._commit()

    def defer(self, timer_id: int, new_due: float) -> None:
        """Record a failed delivery attempt and reschedule the timer to ``new_due`` (backoff),
        atomically. Because the event is no longer due until ``new_due`` it can't
        head-of-line-block newer events, and it gets a real spaced retry window (#24, SCP-C-001).

        Note: this does not hold back the *same* session's later events, so under a delivery
        failure a session's events may be delivered out of order (the deliberate #24 tradeoff —
        isolate other sessions rather than block on one). Strict per-session FIFO under failure
        is tracked separately (see the durable module docstring)."""
        self.conn.execute(
            "UPDATE timers SET attempts=attempts+1, due=? WHERE id=?", (new_due, timer_id)
        )
        self._commit()

    def dead_letter(self, row: sqlite3.Row, error: str) -> None:
        """Move a repeatedly-failing (or undecodable) timer out of the live queue into
        ``dead_letters``, recording the final attempt count and the error (#24). ``row`` is
        the timer row the caller already peeked, so there is no redundant re-SELECT
        (SCP-C-009)."""
        self.conn.execute(
            "INSERT INTO dead_letters(session_id, due, event, attempts, last_error, dead_at) "
            "VALUES(?,?,?,?,?,?)",
            (row["session_id"], row["due"], row["event"], row["attempts"] + 1,
             error, self.clock.now()),
        )
        self.conn.execute("DELETE FROM timers WHERE id=?", (row["id"],))
        self._commit()

    def dead_letters(self) -> List[sqlite3.Row]:
        """Events parked after exceeding the delivery-attempt cap — the queryable signal
        that something needs attention (rows: session_id, due, event, attempts,
        last_error, dead_at)."""
        return self.conn.execute(
            "SELECT session_id, due, event, attempts, last_error, dead_at "
            "FROM dead_letters ORDER BY id"
        ).fetchall()


# ---------------------------------------------------------------------------
# Per-session durable event queue (EventQueue protocol)
# ---------------------------------------------------------------------------


class SqliteEventQueue:
    """The ``env.event_queue`` for a durable session: a ``<send>`` writes a timer row.

    Delivery is driven by :class:`DurableRuntime` (which reads the store), so ``tick``
    here is a no-op — the events live in the database, not in memory."""

    def __init__(self, store: SqliteStore, session_id: str):
        self.store = store
        self.session_id = session_id
        self.clock = store.clock

    def send(self, event: Event, *, delay: int = 0, sendid: Optional[str] = None) -> None:
        due = self.clock.now() + (delay / 1000.0)
        self.store.enqueue(self.session_id, event, due, sendid)

    def cancel(self, sendid: str) -> None:
        self.store.cancel(self.session_id, sendid)

    def tick(self, now: Optional[float] = None) -> List[Event]:
        return []  # durable delivery is driven by DurableRuntime, not per-session tick


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class DurableRuntime:
    """Ties the store + registry together: start sessions, enqueue external events,
    and deliver due events (persisting working memory after each step)."""

    #: Delivery attempts before an event is dead-lettered (#23 ruling). Tunable.
    DEAD_LETTER_CAP = 5
    #: Exponential-backoff base (seconds) for a failed delivery: attempt ``n`` reschedules
    #: to ``now + BACKOFF_BASE_S * 2**(n-1)``, clamped to ``[BACKOFF_MIN_S, BACKOFF_MAX_S]``.
    #: The min is a floor so ``due`` always advances even if BASE is tuned to 0 (SCP-C-018).
    BACKOFF_BASE_S = 1.0
    BACKOFF_MIN_S = 1e-3
    BACKOFF_MAX_S = 3600.0

    def __init__(self, store: SqliteStore, registry: ChartRegistry, *,
                 data_model=None, execution_model=None, extra: Optional[dict] = None):
        self.store = store
        self.registry = registry
        self._data_model = data_model
        self._execution_model = execution_model
        self._extra = extra or {}

    def _env(self, session_id: str, chart: Chart):
        kwargs = {"event_queue": SqliteEventQueue(self.store, session_id)}
        if self._data_model is not None:
            kwargs["data_model"] = self._data_model
        if self._execution_model is not None:
            kwargs["execution_model"] = self._execution_model
        env = make_env(chart, **kwargs)
        env.extra.update(self._extra)
        env.extra["_sessionid"] = session_id
        return env

    def start(self, chart_name: str, session_id: str, data: Optional[dict] = None) -> WorkingMemory:
        chart = self.registry.get(chart_name)
        env = self._env(session_id, chart)
        wm = initialize(env, data)
        self.store.save_session(session_id, chart_name, wm)
        return wm

    def enqueue(self, session_id: str, event, *, delay: int = 0) -> None:
        """Submit an external event to a durable session (delivered on the next tick)."""
        due = self.store.clock.now() + (delay / 1000.0)
        self.store.enqueue(session_id, coerce_event(event), due)

    def load(self, session_id: str) -> Optional[WorkingMemory]:
        rec = self.store.load_session(session_id)
        return rec.wm if rec else None

    def _run(self, rec: SessionRecord, session_id: str, event: Event) -> WorkingMemory:
        """Run the chart for one due event on an already-loaded session record and return the
        new working memory. Exceptions raised here are the *handler's* — the chart logic
        rejecting/failing this event — and are classified as poison by :meth:`tick`. Loading
        and persisting the session are the caller's job, so a store failure stays outside this
        method and is classified as infrastructure (SCP-C-019)."""
        env = self._env(session_id, self.registry.get(rec.chart))
        return process_event(env, rec.wm, event)

    def tick(self, now: Optional[float] = None, max_steps: int = 10_000) -> int:
        """Deliver every event that is due at ``now`` (default: the clock), draining
        cascades (a delivered event may enqueue more due-now events). Returns the
        number of events delivered."""
        delivered = 0
        for _ in range(max_steps):
            t = self.store.clock.now() if now is None else now
            # Lock-free pre-check: don't take the write lock (BEGIN IMMEDIATE) just to discover
            # the queue is idle — that would serialize all pollers on an empty queue (SCP-C-025).
            if self.store.peek_one_due(t) is None:
                break
            # One transaction PER EVENT: peek the oldest due timer, deliver + persist +
            # cascade-enqueues + delete-on-success all commit together (or roll back — the event
            # is never lost, bug #21). Failures are classified by *where* they occur:
            #   * handler (`_run` / process_event) raising  -> poison: back off / dead-letter.
            #   * store persistence (load/save/delete) raising a sqlite3.Error -> infrastructure:
            #     propagate, roll back, retry indefinitely, no attempt burned (SCP-C-013/019/023).
            #   * BaseException -> crash: propagate, roll back, no attempt burned (bug #21).
            with self.store.atomic():
                row = self.store.peek_one_due(t)
                if row is None:
                    break
                timer_id = row["id"]
                session_id = row["session_id"]

                # Decode first, inside the handled path: an undecodable payload can never
                # succeed, so it is dead-lettered immediately rather than wedging the queue
                # or being pointlessly retried (SCP-C-002).
                try:
                    event = event_from_jsonable(json.loads(row["event"]))
                except Exception as exc:  # noqa: BLE001 — corrupt/undecodable payload
                    self.store.dead_letter(row, f"undecodable event: {exc!r}")
                    logger.warning(
                        "durable: dead-lettered undecodable event for session %r (id=%s): %s",
                        session_id, timer_id, exc,
                    )
                    continue

                # Load (infrastructure): a failure here propagates (retried indefinitely).
                rec = self.store.load_session(session_id)
                if rec is None or not rec.wm.running:
                    self.store.delete_timer(timer_id)  # session gone/stopped: drop the event
                    delivered += 1
                    continue

                # Run the handler (poison zone): only chart logic is caught here.
                try:
                    with self.store.savepoint():
                        wm = self._run(rec, session_id, event)
                except Exception as exc:  # noqa: BLE001 — handler/event error = poison
                    self._park_or_backoff(row, session_id, t, exc)
                    continue

                # Persist (infrastructure zone): a failure here — of any type — propagates and
                # rolls back the whole event, so it is retried, never dead-lettered.
                self.store.save_session(session_id, rec.chart, wm)
                self.store.delete_timer(timer_id)
                delivered += 1
        return delivered

    def _park_or_backoff(self, row, session_id: str, t: float, exc: Exception) -> None:
        """A handler (poison) failure: back the timer off for a spaced retry, or dead-letter it
        once the attempt cap is reached (#24). Logs every attempt (SCP-C-007)."""
        n = row["attempts"] + 1
        if n >= self.DEAD_LETTER_CAP:
            self.store.dead_letter(row, str(exc))
            logger.warning(
                "durable: dead-lettered event for session %r after %d attempts: %s",
                session_id, n, exc,
            )
        else:
            backoff = min(self.BACKOFF_BASE_S * (2 ** (n - 1)), self.BACKOFF_MAX_S)
            backoff = max(backoff, self.BACKOFF_MIN_S)
            # Guarantee `due` strictly advances even at large float epochs where the backoff
            # would round away, so a failed timer is never re-peeked within one tick (SCP-C-024).
            new_due = max(t + backoff, math.nextafter(t, math.inf))
            self.store.defer(row["id"], new_due)
            logger.warning(
                "durable: delivery failed for session %r (attempt %d/%d); "
                "retrying after %.3gs backoff: %s",
                session_id, n, self.DEAD_LETTER_CAP, backoff, exc,
            )

    def next_due(self) -> Optional[float]:
        return self.store.next_due()
