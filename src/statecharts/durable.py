"""Durable event queue + session store, backed by SQLite (stdlib, file-based, free).

This makes the "long-lived session" story real: a workflow can wait for hours or
days across process restarts.  Two things are persisted:

* **sessions** — each session's :class:`WorkingMemory` as JSON, keyed by session id.
* **timers** — the durable mailbox: pending (delayed) events with absolute due times.

Charts are *code* (their guards/actions are Python callables), so they are registered
by name in a :class:`ChartRegistry` rather than persisted; only JSON-able working
memory and events go in the database.

Scope: SQLite gives durability and safe multi-process access on a single machine.
The schema/queries port directly to Postgres for true multi-node distribution
(replace :meth:`SqliteStore.claim_due`'s transaction with
``SELECT ... FOR UPDATE SKIP LOCKED``).

Delivery contract: :meth:`DurableRuntime.tick` delivers **one event per transaction** —
claiming a due timer, persisting the resulting working memory, and any cascade ``<send>``
it enqueues all run inside a single :meth:`SqliteStore.atomic` transaction. A crash/error
during delivery rolls that event back (so it is redelivered on the next tick and takes
effect **exactly once** — no lost or double-applied events; bug #21), while events already
delivered in the same tick keep their committed progress (independent sessions are
isolated — one failing event never reverts another's). The multi-worker Postgres port
instead uses a lease/visibility-timeout for **at-least-once** delivery (which then requires
idempotent handlers) — see ``docs/design/postgres-durability.md``.

Limitation: active child ``<invoke>`` sessions are not persisted (they live only in
an in-process run); durable sessions are for the persist-between-external-events use
case, where data is JSON-serializable.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .algorithm import initialize, process_event
from .chart import Chart, make_chart
from .elements import StateNode
from .environment import make_env
from .event_queue import Clock
from .events import Event, coerce_event
from .working_memory import WorkingMemory


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
    sendid     TEXT
);
CREATE INDEX IF NOT EXISTS timers_due ON timers(due);
CREATE INDEX IF NOT EXISTS timers_session ON timers(session_id);
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
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")  # durability + concurrent readers
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self.clock = clock or Clock()
        self._depth = 0  # >0 while inside atomic(): per-call commits are deferred

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def atomic(self):
        """A write transaction that batches the otherwise per-call commits of
        ``save_session`` / ``enqueue`` / ``cancel`` / ``claim_due`` into one unit, so a
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

    def claim_due(self, now: float) -> List[Tuple[str, Event]]:
        """Atomically take all timers due at/<= ``now`` (oldest first) and return
        ``(session_id, event)`` pairs. The single write transaction is what makes this
        safe for multiple processes on one machine. Delivery (:meth:`DurableRuntime.tick`)
        uses :meth:`claim_one_due` instead, so one failing event can't roll back its
        co-due batch-mates."""
        with self.atomic():
            rows = self.conn.execute(
                "SELECT id, session_id, event FROM timers WHERE due<=? ORDER BY due, id",
                (now,),
            ).fetchall()
            if rows:
                self.conn.executemany(
                    "DELETE FROM timers WHERE id=?", [(r["id"],) for r in rows]
                )
        return [(r["session_id"], event_from_jsonable(json.loads(r["event"]))) for r in rows]

    def claim_one_due(self, now: float) -> Optional[Tuple[str, Event]]:
        """Atomically claim (delete) the **single** oldest due timer, or ``None`` if
        nothing is due. Delivering one event per transaction keeps sessions isolated: a
        crash/error reverts and retries only that event, never its co-due batch-mates
        (bug #21 follow-up — per-event, not per-batch, atomicity)."""
        with self.atomic():
            row = self.conn.execute(
                "SELECT id, session_id, event FROM timers WHERE due<=? ORDER BY due, id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("DELETE FROM timers WHERE id=?", (row["id"],))
            return (row["session_id"], event_from_jsonable(json.loads(row["event"])))


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

    def _deliver(self, session_id: str, event: Event) -> None:
        rec = self.store.load_session(session_id)
        if rec is None or not rec.wm.running:
            return
        env = self._env(session_id, self.registry.get(rec.chart))
        wm = process_event(env, rec.wm, event)
        self.store.save_session(session_id, rec.chart, wm)

    def tick(self, now: Optional[float] = None, max_steps: int = 10_000) -> int:
        """Deliver every event that is due at ``now`` (default: the clock), draining
        cascades (a delivered event may enqueue more due-now events). Returns the
        number of events delivered."""
        delivered = 0
        for _ in range(max_steps):
            t = self.store.clock.now() if now is None else now
            # One transaction PER EVENT: claiming the timer, persisting the resulting
            # working memory, and any cascade <send> it enqueues all commit together, or
            # roll back together on a crash/error — so the event is never lost (bug #21)
            # and, crucially, one failing event can't revert a co-due event from another
            # session (they each commit independently).
            with self.store.atomic():
                claimed = self.store.claim_one_due(t)
                if claimed is not None:
                    session_id, event = claimed
                    self._deliver(session_id, event)
                    delivered += 1
            if claimed is None:
                break
        return delivered

    def next_due(self) -> Optional[float]:
        return self.store.next_due()
