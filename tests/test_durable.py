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


def test_durable_crash_between_claim_and_persist_does_not_lose_event():
    """Bug #21: if the process dies after a timer is claimed but before the resulting
    working memory is persisted, the event must NOT be lost — claim + persist must be
    atomic, so a crash rolls both back and the event is redelivered on the next tick."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("flow", flow_chart())
    rt = DurableRuntime(store, reg)
    rt.start("flow", "s1")
    rt.enqueue("s1", "begin")  # due now

    # Simulate a crash: persisting the new working memory raises the first time,
    # i.e. after the timer has been claimed but before its effect is committed.
    real_save = store.save_session
    calls = {"n": 0}
    def flaky_save(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash before WM persisted")
        return real_save(*a, **k)
    store.save_session = flaky_save
    try:
        rt.tick()
    except RuntimeError:
        pass  # the "crash"

    # Atomic rollback: the claim (delete) AND the partial WM write were both undone.
    assert "idle" in rt.load("s1").configuration     # WM did not advance
    assert store.next_due() is not None              # the "begin" timer survived

    # And the event is delivered exactly once on a clean retry (not lost, not doubled).
    store.save_session = real_save
    rt.tick()
    assert "waiting" in rt.load("s1").configuration, rt.load("s1").configuration
    store.close()


def test_durable_one_failing_event_does_not_revert_a_co_due_sibling():
    """Per-event atomicity (bug #21 review): when two independent sessions have events
    due in the same tick and one delivery fails, the other session's delivery must still
    commit — sessions are isolated, one poison event can't roll back its batch-mates."""
    chart = statechart({"initial": "a"}, state({"id": "a"}, on("go", "b")), state({"id": "b"}))
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", chart)
    rt = DurableRuntime(store, reg)
    rt.start("c", "good")
    rt.start("c", "poison")
    rt.enqueue("good", "go")     # enqueued first -> claimed/delivered first
    rt.enqueue("poison", "go")

    real = rt._deliver
    def deliver(session_id, event):
        if session_id == "poison":
            raise RuntimeError("simulated delivery failure")
        return real(session_id, event)
    rt._deliver = deliver

    try:
        rt.tick()
    except RuntimeError:
        pass  # poison fails loud

    # the innocent session committed its progress; it was NOT rolled back with the poison
    assert "b" in rt.load("good").configuration, rt.load("good").configuration
    # the poison event is preserved (not lost), for retry / operator attention
    assert store.next_due() is not None
    store.close()


def _poison_chart():
    return statechart({"initial": "a"}, state({"id": "a"}, on("go", "b")), state({"id": "b"}))


def test_durable_poison_oldest_does_not_block_newer_event():
    """#24: a permanently-failing OLDEST event must not block a newer event queued behind
    it — the queue skips past the failing one and delivers the newer session's event."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "poison")
    rt.start("c", "good")
    rt.enqueue("poison", "go")   # enqueued first -> OLDEST due
    rt.enqueue("good", "go")

    real = rt._deliver
    def deliver(session_id, event):
        if session_id == "poison":
            raise RuntimeError("poison")
        return real(session_id, event)
    rt._deliver = deliver

    rt.tick()  # must NOT wedge on the oldest (poison) event
    assert "b" in rt.load("good").configuration, rt.load("good").configuration
    store.close()


def test_durable_event_dead_lettered_after_cap():
    """#24: an event that fails to deliver 5x is moved to a dead_letters table (with its
    error), removed from timers, and no longer blocks the queue."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("always fails"))

    for _ in range(5):
        rt.tick()  # attempt 1..5; the 5th moves it to dead_letters

    assert store.next_due() is None, "timer should be gone from the live queue"
    dl = store.dead_letters()
    assert len(dl) == 1
    assert dl[0]["session_id"] == "s1"
    assert "always fails" in dl[0]["last_error"]
    assert dl[0]["attempts"] == 5
    store.close()
