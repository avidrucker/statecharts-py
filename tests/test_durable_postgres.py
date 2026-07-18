"""PostgresStore — durable backend with FOR UPDATE SKIP LOCKED lease delivery (#19).

These tests need a running Postgres. They **skip cleanly** when it is unavailable, so the
default zero-dependency suite (`python3 run_tests.py`) stays green on a machine without the
`postgres` extra or without a server:

* skipped if `psycopg` is not importable (the `postgres` extra isn't installed);
* skipped if no Postgres answers at `DATABASE_URL`
  (default `postgres://postgres:postgres@localhost:5432/statecharts_dev`; bring one up with
  `pgdev up`).

The module follows the repo's fixture-free style (self-contained `test_*` functions, no pytest
fixtures) so both `pytest` and the custom `run_tests.py` runner drive it. Skipping is an early
return: a no-op PASS under `run_tests.py`, a real `pytest.skip` under pytest. Each test runs in
its own throwaway schema, dropped in a `finally`.

Delivery here is **at-least-once** (lease/visibility-timeout), not SQLite's exactly-once, so the
SQLite dead-letter/poison tests do *not* transfer (ruling on #19). The delivery-semantics tests
below are Postgres-specific: disjoint multi-worker claims, crash-between-claim-and-persist
redelivery, lease-expiry re-claim, and the idempotency contract those imply.
"""
import json
import os
import threading
import uuid

try:  # optional: pytest gives a real skip; run_tests.py runs without it
    import pytest
except ModuleNotFoundError:  # pragma: no cover
    pytest = None

try:  # optional: the 'postgres' extra
    import psycopg
except ModuleNotFoundError:  # pragma: no cover
    psycopg = None

from statecharts import (
    ChartRegistry, ManualClock, Script, statechart, state, final, on,
    send_after, handle, ops, transition,
)
from statecharts.durable import event_from_jsonable
from statecharts.events import coerce_event

DSN = os.environ.get(
    "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/statecharts_dev"
)


def _pg_available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2) as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False


# Evaluated once. Short-circuits so `psycopg is None` (system python, no extra) never touches it.
_PG_OK = psycopg is not None and _pg_available()

# Importing the PG module hard-requires psycopg, so only import it when the extra is present.
if psycopg is not None:
    from statecharts.durable_postgres import PostgresStore, PostgresRuntime


def _skip(reason: str):
    """Skip under pytest; a silent no-op (PASS) under the fixture-free run_tests.py runner."""
    if pytest is not None:
        pytest.skip(reason)


def _new_schema() -> str:
    return "t_" + uuid.uuid4().hex


def _drop(schema: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _decode(row):
    return event_from_jsonable(json.loads(row["event"]))


# --------------------------------------------------------------------------- charts
def flow_chart():
    # idle --begin--> waiting (1s timeout) --timeout--> done; cancel returns to idle
    return statechart({"initial": "idle"},
        state({"id": "idle"}, on("begin", "waiting")),
        state({"id": "waiting"},
            *send_after({"id": "to", "event": "timeout", "delay": 1000}),
            on("timeout", "done"),
            on("cancel", "idle"),
        ),
        final({"id": "done"}),
    )


def counter_chart():
    return statechart({"initial": "c"},
        state({"id": "c"}, handle("inc", lambda env, data: [ops.assign("n", data.get("n", 0) + 1)])),
    )


def loop_chart(sink):
    # A self-transition that runs a side-effecting Script every time "ping" is delivered.
    return statechart({"initial": "s"},
        state({"id": "s"},
            transition({"event": "ping", "target": "s"},
                       Script(lambda env, data: (sink.append(1), [])[1])),
        ),
    )


# ============================================================ core store-interface behaviours
# (the same behaviours test_durable.py's roundtrip / survives_restart / cancel_before_due
#  exercise for SQLite — only these transfer; delivery semantics do not.)

def test_pg_basic_roundtrip():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    store = PostgresStore(DSN, clock=ManualClock(), schema=schema)
    try:
        reg = ChartRegistry().register("flow", flow_chart())
        rt = PostgresRuntime(store, reg)
        rt.start("flow", "s1")
        assert "idle" in rt.load("s1").configuration
        rt.enqueue("s1", "begin")
        assert rt.tick() == 1
        assert "waiting" in rt.load("s1").configuration
    finally:
        store.close()
        _drop(schema)


def test_pg_survives_restart():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    reg = ChartRegistry().register("flow", flow_chart())
    try:
        # --- process 1: start, begin -> waiting, schedule the 1s timeout, "crash" ---
        store1 = PostgresStore(DSN, clock=ManualClock(), schema=schema)
        rt1 = PostgresRuntime(store1, reg)
        rt1.start("flow", "job")
        rt1.enqueue("job", "begin")
        rt1.tick()
        assert "waiting" in rt1.load("job").configuration
        assert store1.next_due() is not None  # the 1s timeout is pending in the db
        store1.close()  # simulate shutdown with the timer still pending

        # --- process 2: reopen the SAME schema, 2s later; the timer is now due ---
        clock2 = ManualClock()
        clock2.advance(2.0)
        store2 = PostgresStore(DSN, clock=clock2, schema=schema)
        rt2 = PostgresRuntime(store2, reg)
        assert "waiting" in rt2.load("job").configuration  # state persisted across restart
        assert rt2.tick() == 1  # the delayed timeout (persisted) is delivered
        assert "done" in rt2.load("job").configuration
        assert not rt2.load("job").running
        store2.close()
    finally:
        _drop(schema)


def test_pg_cancel_before_due():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    store = PostgresStore(DSN, clock=ManualClock(), schema=schema)
    try:
        reg = ChartRegistry().register("flow", flow_chart())
        rt = PostgresRuntime(store, reg)
        rt.start("flow", "s1")
        rt.enqueue("s1", "begin")
        rt.tick()  # -> waiting, timer scheduled
        rt.enqueue("s1", "cancel")
        rt.tick()  # cancel exits waiting -> idle, and cancels the timer
        assert "idle" in rt.load("s1").configuration
        assert store.next_due() is None  # timer was cancelled
        store.clock.advance(5.0)
        assert rt.tick() == 0  # nothing due
        assert "idle" in rt.load("s1").configuration
    finally:
        store.close()
        _drop(schema)


def test_pg_enqueue_cancel_next_due():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    store = PostgresStore(DSN, clock=ManualClock(), schema=schema)
    try:
        store.enqueue("s", coerce_event("go"), 5.0, "t1")
        assert store.next_due() == 5.0
        store.cancel("s", "t1")
        assert store.next_due() is None
    finally:
        store.close()
        _drop(schema)


def test_pg_datamodel_persists():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    reg = ChartRegistry().register("counter", counter_chart())
    try:
        store1 = PostgresStore(DSN, clock=ManualClock(), schema=schema)
        rt1 = PostgresRuntime(store1, reg)
        rt1.start("counter", "c1", data={"n": 0})
        rt1.enqueue("c1", "inc"); rt1.tick()
        rt1.enqueue("c1", "inc"); rt1.tick()
        store1.close()
        # reopen the same schema and confirm the counter survived
        store2 = PostgresStore(DSN, clock=ManualClock(), schema=schema)
        rt2 = PostgresRuntime(store2, reg)
        assert rt2.load("c1").datamodel["n"] == 2
        store2.close()
    finally:
        _drop(schema)


# ============================================================ Postgres-specific delivery semantics

def test_pg_claim_two_workers_take_disjoint_rows():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    # Two 'ready' timers, two independent connections: each claim takes a distinct row, and a
    # third claim finds nothing — SKIP LOCKED + the in-flight flag give disjoint work.
    a = PostgresStore(DSN, schema=schema)
    b = PostgresStore(DSN, schema=schema)
    try:
        a.enqueue("s1", coerce_event("go"), 0.0)
        a.enqueue("s2", coerce_event("go"), 0.0)
        r1 = a.claim(1.0, lease=60.0)
        r2 = b.claim(1.0, lease=60.0)
        assert r1 is not None and r2 is not None
        assert r1["id"] != r2["id"]
        assert a.claim(1.0, lease=60.0) is None  # both rows are now in-flight
    finally:
        a.close(); b.close()
        _drop(schema)


def test_pg_concurrent_claims_are_disjoint():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    # N due timers, M worker threads each looping claim-until-empty: every row is claimed exactly
    # once across all workers (no double-claim, none lost). Exercises the real FOR UPDATE SKIP
    # LOCKED contention path, not just sequential claims.
    admin = PostgresStore(DSN, schema=schema)
    n = 30
    try:
        for i in range(n):
            admin.enqueue(f"s{i}", coerce_event("go"), 0.0)

        claimed: list[int] = []
        lock = threading.Lock()

        def worker():
            s = PostgresStore(DSN, schema=schema)  # psycopg connections aren't shared across threads
            got = []
            while True:
                row = s.claim(1000.0, lease=60.0)
                if row is None:
                    break
                got.append(row["id"])
            s.close()
            with lock:
                claimed.extend(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(claimed) == n           # every row claimed
        assert len(set(claimed)) == n      # none claimed twice
    finally:
        admin.close()
        _drop(schema)


def test_pg_lease_not_expired_is_not_reclaimed():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    store = PostgresStore(DSN, clock=ManualClock(), schema=schema)
    try:
        store.enqueue("s", coerce_event("go"), 0.0)
        r1 = store.claim(10.0, lease=5.0)
        assert r1 is not None
        # A second claim at the same instant sees the row in-flight with a live lease -> nothing.
        assert store.claim(10.0, lease=5.0) is None
    finally:
        store.close()
        _drop(schema)


def test_pg_lease_expiry_allows_reclaim():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    store = PostgresStore(DSN, clock=ManualClock(), schema=schema)
    try:
        store.enqueue("s", coerce_event("go"), 0.0)
        r1 = store.claim(10.0, lease=5.0)
        assert r1 is not None
        assert store.claim(12.0, lease=5.0) is None      # 12 - 5 = 7 < claimed_at(10): still leased
        r2 = store.claim(16.0, lease=5.0)                 # 16 - 5 = 11 >= claimed_at(10): expired
        assert r2 is not None and r2["id"] == r1["id"]    # the SAME row is re-claimed (at-least-once)
    finally:
        store.close()
        _drop(schema)


def test_pg_crash_between_claim_and_persist_redelivers():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    # A worker claims 'begin' then CRASHES before delivering (never deletes the row). The event
    # is not lost: once the lease expires another worker re-claims and delivers it.
    reg = ChartRegistry().register("flow", flow_chart())
    clock = ManualClock()
    store = PostgresStore(DSN, clock=clock, schema=schema)
    try:
        rt = PostgresRuntime(store, reg)
        rt.start("flow", "job")
        rt.enqueue("job", "begin")

        row = store.claim(clock.now(), lease=5.0)   # worker A claims...
        assert row is not None
        assert "idle" in rt.load("job").configuration  # ...then dies before delivering

        assert rt.tick(lease=5.0) == 0                  # worker B: lease still live -> nothing yet
        assert "idle" in rt.load("job").configuration

        clock.advance(6.0)                              # lease expires
        assert rt.tick(lease=5.0) == 1                  # re-claimed and delivered (at-least-once)
        assert "waiting" in rt.load("job").configuration
    finally:
        store.close()
        _drop(schema)


def test_pg_at_least_once_can_duplicate_side_effects():
    if not _PG_OK:
        return _skip("Postgres unavailable")
    schema = _new_schema()
    # The idempotency contract, made concrete: a redelivered event RE-RUNS its handler's side
    # effect. A worker delivers 'ping' (side effect runs once) but crashes before deleting the
    # timer; after the lease expires the event is redelivered and the side effect runs AGAIN.
    # Handlers must therefore be idempotent or externally deduplicated (SCP-C-015).
    sink: list[int] = []
    reg = ChartRegistry().register("loop", loop_chart(sink))
    clock = ManualClock()
    store = PostgresStore(DSN, clock=clock, schema=schema)
    try:
        rt = PostgresRuntime(store, reg)
        rt.start("loop", "s")
        rt.enqueue("s", "ping")

        row = store.claim(clock.now(), lease=5.0)
        rt._deliver("s", _decode(row))                  # side effect runs once...
        assert sink == [1]                              # ...WM saved, but worker crashes before delete

        clock.advance(6.0)                              # lease expires -> redelivery
        row2 = store.claim(clock.now(), lease=5.0)
        assert row2["id"] == row["id"]
        rt._deliver("s", _decode(row2))                 # side effect runs a SECOND time
        store.delete_timer(row2["id"])
        assert sink == [1, 1]                           # at-least-once duplicated the side effect
    finally:
        store.close()
        _drop(schema)
