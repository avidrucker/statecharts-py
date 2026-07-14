"""Durable SQLite-backed event queue + session store."""
import json
import logging
import os
import sqlite3
import tempfile

from statecharts import (
    ChartRegistry, DurableRuntime, SqliteStore, StoreError, ManualClock,
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
    atomic, so a crash rolls both back and the event is redelivered on the next tick.
    A crash is modelled with a BaseException (SCP-Q-001): it propagates, rolls back, and —
    unlike a poison delivery Exception — burns no attempt (exactly-once, not retry-toward-cap)."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("flow", flow_chart())
    rt = DurableRuntime(store, reg)
    rt.start("flow", "s1")
    rt.enqueue("s1", "begin")  # due now

    # Simulate a crash: persisting the new working memory dies the first time (a BaseException,
    # i.e. a genuine interrupt/crash — not an application-level delivery error), after the
    # timer has been claimed but before its effect is committed.
    real_save = store.save_session
    calls = {"n": 0}
    def flaky_save(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt("simulated crash before WM persisted")
        return real_save(*a, **k)
    store.save_session = flaky_save
    try:
        rt.tick()
    except KeyboardInterrupt:
        pass  # the "crash" propagated out of tick()

    # Atomic rollback: the claim (delete) AND the partial WM write were both undone.
    assert "idle" in rt.load("s1").configuration     # WM did not advance
    assert store.next_due() is not None              # the "begin" timer survived
    surviving = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
    assert surviving["attempts"] == 0                 # a crash burns no attempt (not poison)

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

    # A delivery error is caught per-event (not propagated), so one poison event can't
    # abort the tick — tick() returns normally (SCP-C-008: assert this directly rather than
    # via a now-unreachable `except RuntimeError`).
    delivered = rt.tick()

    # the innocent session committed its progress; it was NOT rolled back with the poison
    assert "b" in rt.load("good").configuration, rt.load("good").configuration
    assert delivered == 1, "exactly the healthy sibling was delivered"
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
    error), removed from timers, and no longer blocks the queue. With retry backoff
    (SCP-C-001) each attempt only becomes due after time passes, so the drain advances the
    clock between attempts rather than burning all five back-to-back."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("always fails"))

    for _ in range(5):
        store.clock.advance(10_000.0)  # clear any backoff so the timer is due again
        rt.tick()  # attempt 1..5; the 5th moves it to dead_letters

    assert store.next_due() is None, "timer should be gone from the live queue"
    dl = store.dead_letters()
    assert len(dl) == 1
    assert dl[0]["session_id"] == "s1"
    assert "always fails" in dl[0]["last_error"]
    assert dl[0]["attempts"] == 5
    store.close()


def test_durable_failed_delivery_is_backed_off_to_the_future():
    """SCP-C-001 (mechanism updated for #26): a failed delivery must get a real spaced retry
    window, not be left immediately re-eligible. #26 spaces the retry by gating the *session*
    until a future `retry_at` (rather than pushing the timer's `due` — which would let a sibling
    overtake it). So after one failure at a fixed clock the event is still live at its original
    due time, but is no longer *deliverable* (its session is gated) and a fixed-clock drain burns
    no further attempts."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("down for now"))

    t0 = store.clock.now()
    rt.tick()  # one failed attempt at t0

    # The event is not lost, but it is gated: nothing is deliverable at the fixed clock, and
    # the session's retry_at was pushed into the future (the spaced retry window).
    assert store.next_due() is not None, "event must not be lost"
    assert store.peek_one_due(t0) is None, "the failed session is gated — not re-eligible at t0"
    retry_at = store.conn.execute(
        "SELECT retry_at FROM session_gates WHERE session_id=?", ("s1",)).fetchone()["retry_at"]
    assert retry_at > t0, "the session's retry gate must be set into the future"

    # A fixed-clock drain does NOT keep burning attempts — the session is gated.
    rt.tick(); rt.tick(); rt.tick()
    row = store.conn.execute("SELECT attempts FROM timers").fetchone()
    assert row is not None and row["attempts"] == 1, "no back-to-back attempt burn at fixed clock"
    assert store.dead_letters() == [], "one transient failure must not dead-letter"
    store.close()


def test_durable_corrupt_row_is_dead_lettered_not_wedged():
    """SCP-C-002: an un-decodable oldest timer row (corrupt/legacy event JSON) must be
    dead-lettered, not re-peeked-and-re-raised forever. A healthy newer event queued behind
    it must still be delivered in the same tick."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    # Corrupt row goes in first -> it is the OLDEST due timer (lowest id, same due).
    store.conn.execute(
        "INSERT INTO timers(session_id, due, event, sendid) VALUES(?,?,?,?)",
        ("ghost", 0.0, "{ this is not valid json", None),
    )
    store.conn.commit()
    rt.start("c", "good")
    rt.enqueue("good", "go")  # healthy event, queued behind the corrupt row

    rt.tick()  # must NOT raise out of tick()

    assert "b" in rt.load("good").configuration, "healthy event delivered past the corrupt row"
    dl = store.dead_letters()
    assert len(dl) == 1 and dl[0]["session_id"] == "ghost", "corrupt row parked, not wedged"
    assert store.next_due() is None, "queue drained; nothing left blocking"
    store.close()


def test_durable_under_cap_failure_is_logged():
    """SCP-C-007: every failed delivery attempt — not only the terminal dead-letter — must
    emit a warning, so an intermittent fault that recovers before the cap is still visible
    to operators."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("transient blip"))

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("statecharts.durable")
    logger.addHandler(handler)
    try:
        rt.tick()  # a single under-cap (attempt 1 of 5) failure
    finally:
        logger.removeHandler(handler)

    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert warnings, "an under-cap failed attempt must be logged"
    assert "s1" in warnings[0].getMessage(), "the log names the affected session"
    store.close()


def test_durable_baseexception_propagates_without_burning_an_attempt():
    """SCP-C-004 / SCP-Q-001: a BaseException (e.g. KeyboardInterrupt / SystemExit) during
    delivery is a *crash*, not a poison payload. It must propagate out of tick(), roll the
    transaction back, and burn NO attempt — preserving bug #21's exactly-once contract."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")

    def crash(sid, ev):
        raise KeyboardInterrupt("operator hit Ctrl-C mid-delivery")
    rt._deliver = crash

    raised = False
    try:
        rt.tick()
    except KeyboardInterrupt:
        raised = True
    assert raised, "a BaseException must propagate out of tick() (crash, not poison)"

    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
    assert row is not None, "the event survived the crash (atomic rollback)"
    assert row["attempts"] == 0, "a crash must not burn a delivery attempt"
    assert store.dead_letters() == [], "a crash must never dead-letter"
    store.close()


def test_durable_store_error_retries_forever_never_dead_letters():
    """SCP-C-013 / SCP-C-034: a store/infrastructure error (StoreError from the persistence
    layer — disk full / db locked / I/O) is NOT the event's fault. tick() rolls it back, leaves
    the event queued (no attempt burned), and does NOT raise — the caller's next poll retries it,
    indefinitely, never counting toward the poison cap and never dead-lettering."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")

    real_save = store.save_session
    outage = {"n": 3}  # persistence unavailable for the first 3 ticks, then recovers
    def flaky_save(*a, **k):
        if outage["n"] > 0:
            outage["n"] -= 1
            raise StoreError("database is locked")  # what SqliteStore raises on infra failure
        return real_save(*a, **k)
    store.save_session = flaky_save

    for _ in range(3):
        rt.tick()  # must NOT raise (SCP-C-034) — a poller loop survives the outage
        row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
        assert row is not None, "healthy event survives the outage"
        assert row["attempts"] == 0, "an infra error must not burn a delivery attempt"
        assert store.dead_letters() == [], "an infra error must never dead-letter a healthy event"

    rt.tick()  # DB recovered -> delivered normally
    assert "b" in rt.load("s1").configuration, rt.load("s1").configuration
    assert store.dead_letters() == []
    store.close()


def test_store_error_is_a_catchable_exception():
    """SCP-C-038: StoreError must be a normal Exception so idiomatic `except Exception` callers
    (a poller that also calls enqueue()/start()) catch a transient store outage instead of
    crashing."""
    assert issubclass(StoreError, Exception)


def test_durable_commit_failure_does_not_wedge_or_overcount():
    """SCP-C-037 / SCP-C-039: if COMMIT fails at the atomic() boundary, tick() must (a) not leave
    the connection mid-transaction — which would make every later BEGIN fail, a permanent silent
    wedge — and (b) not count the un-committed event as delivered. The event rolls back, is
    retried, and delivered exactly once when the store recovers."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")

    # sqlite3.Connection.commit is read-only, so wrap the connection to fail COMMIT once.
    fail = {"n": 1}
    class _FlakyConn:
        def __init__(self, real):
            self._real = real
        def commit(self):
            if fail["n"] > 0:
                fail["n"] -= 1
                raise sqlite3.OperationalError("disk I/O error at commit")
            return self._real.commit()
        def __getattr__(self, name):
            return getattr(self._real, name)
    store.conn = _FlakyConn(store.conn)

    n1 = rt.tick()   # COMMIT fails -> rolled back, not counted, connection left clean
    assert n1 == 0, "an un-committed event must not be counted as delivered"

    n2 = rt.tick()   # store recovered -> delivered exactly once (not permanently wedged)
    assert n2 == 1, "the event is redelivered after the store recovers (no wedge)"
    assert "b" in rt.load("s1").configuration, rt.load("s1").configuration
    assert store.dead_letters() == []
    store.close()


def test_durable_tick_inside_a_caller_transaction_keeps_its_writes():
    """SCP-C-044: tick()'s entry recover() must only clear a STALE leftover transaction (no
    active atomic()), never a transaction a caller legitimately holds. A caller batching
    enqueue+deliver in one atomic() must keep its enqueued event."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")

    with store.atomic():
        rt.enqueue("s1", "go")   # pending inside the caller's transaction
        rt.tick()                # recover() at entry must NOT roll this back

    assert "b" in rt.load("s1").configuration, rt.load("s1").configuration
    assert store.next_due() is None
    store.close()


def test_durable_enqueue_commit_failure_does_not_phantom_commit_a_later_write():
    """SCP-C-045: if a standalone write's COMMIT fails, the store must roll back so the failed
    INSERT does not linger in an open transaction and get phantom-committed by the next write."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")

    fail = {"n": 1}
    class _FlakyConn:
        def __init__(self, real):
            self._real = real
        def commit(self):
            if fail["n"] > 0:
                fail["n"] -= 1
                raise sqlite3.OperationalError("commit failed")
            return self._real.commit()
        def __getattr__(self, name):
            return getattr(self._real, name)
    store.conn = _FlakyConn(store.conn)

    try:
        rt.enqueue("s1", "a-event")   # its commit fails -> caller is told it failed
    except StoreError:
        pass
    rt.enqueue("s1", "b-event")       # commits normally; must NOT drag the failed A along

    names = [json.loads(r["event"])["name"]
             for r in store.conn.execute("SELECT event FROM timers ORDER BY id")]
    assert names == ["b-event"], f"the failed enqueue must not be phantom-committed; got {names}"
    store.close()


def test_durable_self_heals_after_a_commit_and_rollback_double_failure():
    """SCP-C-041: if COMMIT and its compensating ROLLBACK BOTH fail (a badly degraded store),
    the connection is left mid-transaction. A later tick must clear that stale transaction and
    resume delivering once the store is healthy, rather than wedging forever on 'cannot start a
    transaction within a transaction' — and the un-committed partial delivery is rolled back, so
    the event is delivered exactly once."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")

    outage = {"commit": 1, "rollback": 1}
    class _FlakyConn:
        def __init__(self, real):
            self._real = real
        def commit(self):
            if outage["commit"] > 0:
                outage["commit"] -= 1
                raise sqlite3.OperationalError("commit failed")
            return self._real.commit()
        def rollback(self):
            if outage["rollback"] > 0:
                outage["rollback"] -= 1
                raise sqlite3.OperationalError("rollback failed")
            return self._real.rollback()
        def __getattr__(self, name):
            return getattr(self._real, name)
    store.conn = _FlakyConn(store.conn)

    rt.tick()  # commit fails, and the compensating rollback ALSO fails -> transaction left open
    assert store.conn.in_transaction, "precondition: connection was left mid-transaction"

    n = rt.tick()  # store healthy now: must self-heal (roll back the stale txn) and deliver
    assert n == 1, "the queue recovered after the double failure, not wedged"
    assert "b" in rt.load("s1").configuration, rt.load("s1").configuration
    assert store.dead_letters() == []
    store.close()


def test_durable_tick_does_not_let_a_store_error_escape_and_crash_a_poller():
    """SCP-C-034: a StoreError reaching tick() never escapes it. tick() catches it internally
    and returns, so an idiomatic supervisor loop is not killed by a transient store outage — the
    event stays queued for the next poll, no attempt burned, nothing dead-lettered."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")

    def boom(*a, **k):
        raise StoreError("disk I/O error")
    store.peek_one_due = boom

    n = rt.tick()  # must return, not raise (not even a BaseException)
    assert n == 0
    assert store.dead_letters() == []
    store.close()


def test_store_exec_wraps_sqlite_error_as_store_error():
    """SCP-C-030 plumbing: SqliteStore surfaces its own DB failures as StoreError (not a bare
    sqlite3.Error), which is what lets tick() classify infrastructure by authority."""
    store = SqliteStore(":memory:", clock=ManualClock())
    raised = None
    try:
        store._exec("SELECT * FROM no_such_table")
    except StoreError as exc:
        raised = exc
    assert raised is not None, "a store DB failure must surface as StoreError"
    assert isinstance(raised.__cause__, sqlite3.Error), "the original sqlite error is chained"
    store.close()


def test_durable_cascade_send_store_failure_follows_executable_content_semantics():
    """SCP-C-035 (documented limitation): a store failure during a cascade <send> INSIDE
    process_event is caught by the engine's executable-content handling (StoreError is a normal
    Exception) and becomes error.execution — the same semantics as any other bad executable
    content. So the event is still delivered and the failed send is simply not scheduled; it is
    NOT dead-lettered and does NOT wedge the queue. (Strict cascade-send atomicity under a store
    outage is out of scope for #24 — the store errors that reach tick() directly, e.g. save/
    delete, ARE classified as infrastructure and retried; see the other durable tests.)"""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("flow", flow_chart())
    rt = DurableRuntime(store, reg)
    rt.start("flow", "s1")
    rt.enqueue("s1", "begin")   # delivering this enters `waiting`, which cascades a <send>

    real_enqueue = store.enqueue
    def flaky_enqueue(*a, **k):
        raise StoreError("database is locked")
    store.enqueue = flaky_enqueue

    delivered = rt.tick()  # engine turns the failed cascade send into error.execution
    assert delivered == 1, "the event itself is still delivered"
    assert "waiting" in rt.load("s1").configuration, rt.load("s1").configuration
    assert store.dead_letters() == [], "a cascade-send failure is not dead-lettered"
    store.close()


def test_durable_handler_sqlite_error_is_poison_not_a_queue_wedge():
    """SCP-C-019: a guard/action that raises sqlite3.Error (touching its OWN db) must be treated
    as poison (backoff / dead-letter), NOT misread as store-infrastructure and propagated —
    which would re-peek the same oldest row every tick and wedge every session behind it. The
    infra/poison line is drawn by *where* the error is raised (handler vs persistence), not by
    exception type."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "poison")
    rt.start("c", "good")
    rt.enqueue("poison", "go")   # enqueued first -> oldest due
    rt.enqueue("good", "go")

    real = rt._deliver
    def deliver(sid, ev):
        if sid == "poison":
            raise sqlite3.OperationalError("handler's own resource is locked")
        return real(sid, ev)
    rt._deliver = deliver

    rt.tick()  # must NOT wedge on the poison (handler-sqlite) event
    assert "b" in rt.load("good").configuration, rt.load("good").configuration
    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("poison",)).fetchone()
    assert row is not None and row["attempts"] == 1, "handler sqlite error is poison: backed off"
    store.close()


def test_durable_backoff_advances_due_even_at_far_future_epoch():
    """SCP-C-024: the backoff floor must keep `due` strictly advancing even at large float
    timestamps where a 1e-6 nudge rounds away, so BACKOFF_BASE_S=0 can't collapse every attempt
    into one tick at some future epoch."""
    store = SqliteStore(":memory:", clock=ManualClock(start=1e12))  # far-future epoch
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.BACKOFF_BASE_S = 0.0
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("always fails"))

    rt.tick()  # a single fixed-clock tick

    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
    assert row is not None and row["attempts"] == 1, "one attempt per fixed-clock tick at any epoch"
    assert store.dead_letters() == [], "must not burn every attempt in one tick"
    store.close()


def test_durable_zero_backoff_base_still_spaces_retries_within_a_tick():
    """SCP-C-018: BACKOFF_BASE_S is advertised tunable; even set to 0 a failed timer must not
    be re-peeked at a fixed clock and burn every attempt in a single tick — a floor keeps its
    next due time strictly in the future."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.BACKOFF_BASE_S = 0.0
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("always fails"))

    rt.tick()  # a single tick at a fixed clock

    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
    assert row is not None and row["attempts"] == 1, "only one attempt burned per fixed-clock tick"
    assert store.dead_letters() == [], "zero backoff must not collapse all attempts into one tick"
    store.close()


def test_durable_unserializable_working_memory_is_poison_not_a_queue_wedge():
    """SCP-C-027: a chart that writes a non-JSON value into the datamodel makes save_session's
    json.dumps raise — a data/handler fault, so it must be poison (backed off, dead-lettered
    after the cap), not an infra-zone failure that wedges the queue. A healthy session behind it
    must still be delivered."""
    from statecharts import handle, ops
    bad = statechart({"initial": "c"},
        state({"id": "c"}, handle("boom", lambda env, data: [ops.assign("x", set([1, 2, 3]))])))
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("bad", bad).register("good", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("bad", "b1")
    rt.start("good", "g1")
    rt.enqueue("b1", "boom")   # enqueued first -> oldest due
    rt.enqueue("g1", "go")

    rt.tick()  # the unserializable WM must NOT wedge the queue

    assert "b" in rt.load("g1").configuration, "healthy session delivered past the bad one"
    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("b1",)).fetchone()
    assert row is not None and row["attempts"] == 1, "unserializable WM is poison: backed off"
    store.close()


def test_durable_undecodable_session_blob_is_poison_not_a_queue_wedge():
    """SCP-C-028: a session whose stored working-memory blob can't be decoded (corrupt / schema
    drift) must be poison — dead-lettered after retries — not an infra-zone load failure that
    re-raises every tick and wedges the whole queue behind it."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.start("c", "good")
    store.conn.execute("UPDATE sessions SET wm=? WHERE session_id=?", ("{ not valid json", "s1"))
    store.conn.commit()
    rt.enqueue("s1", "go")     # oldest, but its session blob is corrupt
    rt.enqueue("good", "go")

    rt.tick()  # must not wedge on the undecodable session

    assert "b" in rt.load("good").configuration, "healthy session delivered past the corrupt one"
    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
    assert row is not None and row["attempts"] == 1, "undecodable session blob is poison: backed off"
    store.close()


def test_durable_event_to_unknown_session_is_traced_not_silently_dropped():
    """SCP-C-029: an event enqueued to a session that was never started (typo / enqueued before
    start) must not be silently deleted and counted as delivered. It is retried (a late start
    could make it deliverable) and, if the session stays unknown, dead-lettered — a queryable
    trace, not a silent drop."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.enqueue("ghost", "go")   # never started

    for _ in range(5):
        store.clock.advance(10_000.0)
        rt.tick()

    dl = store.dead_letters()
    assert len(dl) == 1 and dl[0]["session_id"] == "ghost", dl
    store.close()


# ---------------------------------------------------------------------------
# #26 — strict per-session FIFO under failure (session retry-gate)
# ---------------------------------------------------------------------------


def _ordered_chart():
    """a --P--> b --Q--> c(final). Reaches the terminal state c ONLY if P is delivered
    before Q. If Q arrives first (while in a) it is dropped (no handler), so a later P
    lands in b and Q is gone — the session wedges in b. So 'config reaches c' is an exact
    witness that the session's two events were delivered in schedule order."""
    return statechart({"initial": "a"},
        state({"id": "a"}, on("P", "b")),
        state({"id": "b"}, on("Q", "c")),
        final({"id": "c"}),
    )


def _fail_once(rt, event_name):
    """Wrap rt._deliver so the first delivery of `event_name` raises (a transient failure),
    and every later delivery (of any event) succeeds normally."""
    real = rt._deliver
    budget = {event_name: 1}
    def deliver(session_id, event):
        if budget.get(event.name, 0) > 0:
            budget[event.name] -= 1
            raise RuntimeError(f"transient failure delivering {event.name!r}")
        return real(session_id, event)
    rt._deliver = deliver


def test_durable_same_session_later_event_waits_for_a_failing_earlier_one():
    """#26 / SCP-C-021 sibling case: two co-due same-session events [P, Q] (P older). If P
    transiently fails, Q must NOT be delivered before P succeeds — the session's order is
    preserved under failure. Witness: the chart reaches terminal `c`, which is reachable only
    if P is delivered before Q. RED on main (Q overtakes the backed-off P, so the session
    wedges in `b`); GREEN once the session is gated behind its failing head."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _ordered_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "P")   # enqueued first -> oldest due (lowest id)
    rt.enqueue("s1", "Q")   # co-due, queued behind P
    _fail_once(rt, "P")

    for _ in range(6):      # drain, clearing any retry backoff between ticks
        store.clock.advance(10_000.0)
        rt.tick()

    assert "c" in rt.load("s1").configuration, (
        "later same-session event must wait for the failing earlier one; "
        f"got {sorted(rt.load('s1').configuration)}"
    )
    assert store.next_due() is None, "both events eventually drained"
    store.close()


def test_durable_sibling_inside_backoff_window_does_not_overtake():
    """SCP-C-020: a same-session sibling scheduled strictly INSIDE the failing head's backoff
    window must not overtake it. P (due now) transiently fails; Q is due 0.5s later — after P
    but before P's ~1s backoff. Without a session gate, Q becomes deliverable while P is parked
    and overtakes it (the exact due-mutation gap that was reverted). With the gate, P's whole
    session is held until it succeeds, so schedule order holds and the chart reaches `c`."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _ordered_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "P")               # due at t0 (oldest)
    rt.enqueue("s1", "Q", delay=500)    # due at t0+0.5s — inside P's ~1s backoff window
    _fail_once(rt, "P")

    store.clock.advance(0.0); rt.tick()   # t0: P fails, is gated/backed off
    store.clock.advance(0.5); rt.tick()   # t0+0.5: Q is due but must not overtake P
    for _ in range(6):                    # let the backoff expire; P retries, then Q
        store.clock.advance(10_000.0)
        rt.tick()

    assert "c" in rt.load("s1").configuration, (
        "a sibling due inside the backoff window must not overtake the failing head; "
        f"got {sorted(rt.load('s1').configuration)}"
    )
    assert store.next_due() is None
    store.close()


def test_durable_due_order_beats_id_order_across_a_failure():
    """SCP-C-021: same-session delivery order is (due, id) even under failure, and the `due`
    key dominates the `id` tiebreak. Here Q is enqueued first (lower id) but scheduled LATER
    (due t0+0.5); P is enqueued second (higher id) but due now. Schedule order is P then Q,
    the reverse of id order. P transiently fails. The session must still deliver P before Q
    (reaching `c`), proving the gate preserves (due, id) order and does not fall back to raw id
    order. RED on main (P is parked, Q — lower id — is delivered early and dropped, wedging b)."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _ordered_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "Q", delay=500)    # id=1, but due LATER (t0+0.5)
    rt.enqueue("s1", "P")               # id=2, but due NOW (t0) -> earliest by (due, id)
    _fail_once(rt, "P")

    store.clock.advance(0.0); rt.tick()   # t0: P (higher id, earlier due) fails, gated
    store.clock.advance(0.5); rt.tick()   # t0+0.5: Q (lower id) due but must not jump ahead
    for _ in range(6):
        store.clock.advance(10_000.0)
        rt.tick()

    assert "c" in rt.load("s1").configuration, (
        "delivery must follow (due, id) schedule order across a failure, not raw id order; "
        f"got {sorted(rt.load('s1').configuration)}"
    )
    assert store.next_due() is None
    store.close()


def test_durable_unknown_session_event_gets_one_attempt_per_tick_not_all_at_once():
    """#26 review Finding 1: an event to a not-yet-started (sessionless) session must still be
    retried ONE attempt per fixed-clock tick — the retry gate must apply to sessionless timers
    too. Otherwise a session started even one tick late can never catch an event enqueued before
    it existed, because the orphan burns all DEAD_LETTER_CAP attempts back-to-back in one tick."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.enqueue("ghost", "go")   # never started -> UnknownSessionError (poison) on delivery

    rt.tick()   # a single fixed-clock tick

    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("ghost",)).fetchone()
    assert row is not None, "the orphan event must survive one tick (not dead-lettered at once)"
    assert row["attempts"] == 1, f"one attempt per fixed-clock tick, got {row and row['attempts']}"
    assert store.dead_letters() == [], "must not burn all attempts back-to-back in a single tick"
    store.close()


def test_durable_unknown_session_event_is_delivered_by_a_late_start():
    """#26 review Finding 1: the spaced retry must give a late start() a chance to deliver an
    event enqueued before the session existed (the documented 'a late start may still deliver
    it', SCP-C-029) — not dead-letter it inside the first tick."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.enqueue("late", "go")   # enqueued before the session is started

    rt.tick()                  # attempt 1: unknown session, spaced-retried (not dead-lettered)
    assert store.dead_letters() == [], "must not dead-letter a late-startable event in one tick"
    rt.start("c", "late")      # the session starts (a little late)
    store.clock.advance(10_000.0)
    rt.tick()                  # now deliverable

    assert "b" in rt.load("late").configuration, rt.load("late").configuration
    assert store.dead_letters() == []
    store.close()


def test_durable_next_due_reflects_the_retry_gate_not_the_past_due():
    """#26 review Finding 2: while a session is gated after a failure, next_due() must report the
    future retry time — not the failing head's original (now past) due — so a poller that sleeps
    until next_due() waits for the backoff instead of busy-spinning the whole window."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("down"))

    t0 = store.clock.now()
    rt.tick()   # one failure -> session gated into the future

    assert store.peek_one_due(t0) is None, "nothing is deliverable at t0 (the session is gated)"
    nd = store.next_due()
    assert nd is not None and nd > t0, f"next_due must reflect the gate's future retry time, got {nd}"
    store.close()


def test_durable_restart_preserves_a_failing_events_backoff():
    """#26 review round 2, Finding 2: a (re)start must NOT discard the retry backoff of a
    still-queued failing event. The failing head timer survives a restart (start() re-inits
    working memory but keeps pending timers), so its gate must survive too — otherwise the event
    is retried back-to-back after every restart and dead-lettered prematurely."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("down"))
    rt.tick()   # fail once -> gated, attempts=1

    rt.start("c", "s1")   # restart at the fixed clock -> must NOT clear the backoff gate

    # The failing event is still gated at the fixed clock: not re-attempted after the restart.
    assert store.peek_one_due(store.clock.now()) is None, "restart must preserve the backoff gate"
    rt.tick()  # fixed clock; still gated -> no new attempt burned
    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id='s1'").fetchone()
    assert row is not None and row["attempts"] == 1, (
        f"restart must not burn an extra attempt on a backed-off event; got {row and row['attempts']}")
    assert store.dead_letters() == []
    store.close()


def test_durable_head_cancel_clears_the_retry_gate():
    """#26: cancelling the gated *head* timer must clear the retry gate, so a subsequently-
    enqueued event is not stranded behind a now-gone head."""
    from statecharts import coerce_event
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    store.enqueue("s1", coerce_event("go"), store.clock.now(), sendid="h")  # the only/head timer
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("down"))
    rt.tick()   # head fails -> s1 gated
    assert store.peek_one_due(store.clock.now()) is None, "precondition: s1 is gated"

    store.cancel("s1", "h")   # cancel the gated HEAD -> must clear the gate
    del rt._deliver           # restore the real delivery path

    store.enqueue("s1", coerce_event("go"), store.clock.now())
    rt.tick()   # fixed clock; delivers only if cancelling the head cleared the gate
    assert "b" in rt.load("s1").configuration, rt.load("s1").configuration
    store.close()


def test_durable_unrelated_cancel_does_not_clear_a_failing_heads_gate():
    """#26 review round 2, Finding 1: cancelling an UNRELATED timer (or a no-match sendid) must
    NOT clear a still-failing head's backoff gate — otherwise repeated unrelated cancels retry
    the head back-to-back and dead-letter it prematurely. Only cancelling the head clears it."""
    from statecharts import coerce_event
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    store.enqueue("s1", coerce_event("go"), store.clock.now(), sendid="head")  # the failing head
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("down"))
    rt.tick()   # head fails -> gated, attempts=1

    store.cancel("s1", "nonexistent-sendid")   # unrelated cancel: matches no timer
    assert store.peek_one_due(store.clock.now()) is None, "an unrelated cancel must not un-gate the head"
    rt.tick()   # fixed clock; head still gated -> no extra attempt
    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id='s1'").fetchone()
    assert row is not None and row["attempts"] == 1, (
        f"unrelated cancel must not accelerate the failing head; attempts={row and row['attempts']}")
    assert store.dead_letters() == []
    store.close()


def test_durable_delivers_at_a_negative_clock_epoch():
    """#26 review round 4, Finding 1: the gate filter must not use 0 as a no-gate sentinel — an
    ungated timer must remain deliverable even when the clock reports a negative epoch (a
    ManualClock simulation), which `COALESCE(retry_at,0) <= now` wrongly filtered out."""
    store = SqliteStore(":memory:", clock=ManualClock(start=-10.0))
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")   # due at now = -10 (ungated)
    n = rt.tick()
    assert n == 1, "an ungated event must deliver at a negative clock epoch"
    assert "b" in rt.load("s1").configuration, rt.load("s1").configuration
    store.close()


def test_durable_cancelling_a_non_head_sibling_keeps_the_gate():
    """#26 review round 3, Finding 2 (coverage): cancelling a real NON-head sibling (a genuine
    timer, not a no-match sendid) while the gated head still fails must leave the gate intact."""
    from statecharts import coerce_event
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    store.enqueue("s1", coerce_event("go"), store.clock.now(), sendid="head")            # head
    store.enqueue("s1", coerce_event("later"), store.clock.now() + 100, sendid="sib")    # sibling
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("down"))
    rt.tick()   # head fails -> gated

    store.cancel("s1", "sib")   # cancel the NON-head sibling -> gate must remain
    assert store.conn.execute(
        "SELECT 1 FROM session_gates WHERE session_id='s1'").fetchone() is not None, \
        "cancelling a non-head sibling must not clear the failing head's gate"
    assert store.peek_one_due(store.clock.now()) is None, "head still gated"
    store.close()


def test_durable_cancelling_an_earlier_due_sibling_does_not_ungate_the_failing_head():
    """#26 review round 3, Finding 1: the gate must be owned by the timer that failed, not
    re-derived as the (due,id)-min head. If a newer event sorts AHEAD of the failing head (a
    clock skew / backward clock adjustment can make due(new) < due(head)), cancelling that newer
    event must NOT clear the still-failing head's gate."""
    from statecharts import coerce_event
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    store.enqueue("s1", coerce_event("go"), store.clock.now(), sendid="head")   # head, due=0
    rt._deliver = lambda sid, ev: (_ for _ in ()).throw(RuntimeError("down"))
    rt.tick()   # head fails -> gated, attempts=1
    head_id = store.conn.execute("SELECT id FROM timers WHERE session_id='s1'").fetchone()["id"]

    # A newer event that (via clock skew) sorts AHEAD of the failing head.
    store.enqueue("s1", coerce_event("later"), -5.0, sendid="skewed")
    store.cancel("s1", "skewed")   # cancel the skewed sibling, NOT the failing head

    assert store.conn.execute(
        "SELECT 1 FROM session_gates WHERE session_id='s1'").fetchone() is not None, \
        "cancelling an earlier-due sibling must not clear the failing head's gate"
    assert store.peek_one_due(store.clock.now()) is None, "the failing head is still gated"
    row = store.conn.execute("SELECT attempts FROM timers WHERE id=?", (head_id,)).fetchone()
    assert row is not None and row["attempts"] == 1
    store.close()


def test_durable_duplicate_attempts_alter_is_the_guarded_error():
    """SCP-C-016: pin the exact error the concurrent-migration guard swallows — a second
    `ALTER TABLE ... ADD COLUMN attempts` (the losing worker in a start-up race) raises
    sqlite3.OperationalError('duplicate column name'), which SqliteStore.__init__ tolerates
    when the column is already present."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE timers(id INTEGER PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0)")
    try:
        conn.execute("ALTER TABLE timers ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        assert False, "expected a duplicate-column error"
    except sqlite3.OperationalError as exc:
        assert "duplicate column" in str(exc).lower()
    conn.close()

    # And opening an old-schema DB twice (winner then loser view) must not crash.
    path = tempfile.mktemp(suffix=".scdb")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE timers(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
              "due REAL NOT NULL, event TEXT NOT NULL, sendid TEXT)")
    c.commit(); c.close()
    try:
        s1 = SqliteStore(path, clock=ManualClock())
        s2 = SqliteStore(path, clock=ManualClock())  # second opener: column already added
        cols = {r["name"] for r in s2.conn.execute("PRAGMA table_info(timers)")}
        assert "attempts" in cols
        s1.close(); s2.close()
    finally:
        if os.path.exists(path):
            os.remove(path)
