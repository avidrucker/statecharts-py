"""Durable SQLite-backed event queue + session store."""
import os
import tempfile

from statecharts import (
    ChartRegistry, DurableRuntime, SqliteStore, ManualClock,
    statechart, state, final, on, on_entry, send_after,
)


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


def test_durable_basic_roundtrip():
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("flow", flow_chart())
    rt = DurableRuntime(store, reg)
    rt.start("flow", "s1")
    assert "idle" in rt.load("s1").configuration
    rt.enqueue("s1", "begin")
    rt.tick()
    assert "waiting" in rt.load("s1").configuration
    store.close()


def test_durable_survives_restart():
    path = tempfile.mktemp(suffix=".scdb")
    reg = ChartRegistry().register("flow", flow_chart())
    try:
        # --- process 1: start, begin -> waiting, schedule the 1s timeout, "crash" ---
        store1 = SqliteStore(path, clock=ManualClock())
        DurableRuntime(store1, reg).start("flow", "job")
        rt1 = DurableRuntime(store1, reg)
        rt1.enqueue("job", "begin")
        rt1.tick()
        assert "waiting" in rt1.load("job").configuration
        assert store1.next_due() is not None  # a timer is pending in the db
        store1.close()  # simulate shutdown with the timer still pending

        # --- process 2: reopen the SAME db file, 2s later; timer is now due ---
        clock2 = ManualClock()
        clock2.advance(2.0)
        store2 = SqliteStore(path, clock=clock2)
        rt2 = DurableRuntime(store2, reg)
        assert "waiting" in rt2.load("job").configuration  # state persisted across restart
        rt2.tick()  # the delayed timeout (persisted) is delivered
        assert "done" in rt2.load("job").configuration
        assert not rt2.load("job").running
        store2.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_durable_cancel_before_due():
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("flow", flow_chart())
    rt = DurableRuntime(store, reg)
    rt.start("flow", "s1")
    rt.enqueue("s1", "begin")
    rt.tick()  # -> waiting, timer scheduled
    rt.enqueue("s1", "cancel")
    rt.tick()  # cancel exits waiting -> idle, and cancels the timer
    assert "idle" in rt.load("s1").configuration
    assert store.next_due() is None  # timer was cancelled
    store.clock.advance(5.0)
    rt.tick()  # nothing due
    assert "idle" in rt.load("s1").configuration
    store.close()


def test_durable_datamodel_persists():
    from statecharts import handle, ops
    chart = statechart({"initial": "c"},
        state({"id": "c"}, handle("inc", lambda env, data: [ops.assign("n", data.get("n", 0) + 1)])),
    )
    path = tempfile.mktemp(suffix=".scdb")
    reg = ChartRegistry().register("counter", chart)
    try:
        store = SqliteStore(path, clock=ManualClock())
        rt = DurableRuntime(store, reg)
        rt.start("counter", "c1", data={"n": 0})
        rt.enqueue("c1", "inc"); rt.tick()
        rt.enqueue("c1", "inc"); rt.tick()
        store.close()
        # reopen and confirm the counter value survived
        store2 = SqliteStore(path, clock=ManualClock())
        rt2 = DurableRuntime(store2, reg)
        assert rt2.load("c1").datamodel["n"] == 2
        store2.close()
    finally:
        if os.path.exists(path):
            os.remove(path)
