"""Durable SQLite-backed event queue + session store."""
import json
import logging
import os
import sqlite3
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

    real = rt._run
    def run(rec, session_id, event):
        if session_id == "poison":
            raise RuntimeError("simulated delivery failure")
        return real(rec, session_id, event)
    rt._run = run

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

    real = rt._run
    def run(rec, session_id, event):
        if session_id == "poison":
            raise RuntimeError("poison")
        return real(rec, session_id, event)
    rt._run = run

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
    rt._run = lambda rec, sid, ev: (_ for _ in ()).throw(RuntimeError("always fails"))

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
    """SCP-C-001: a failed delivery must be rescheduled into the future (a real retry
    window), not left immediately re-eligible. Without backoff a fixed-clock drain burns
    every attempt back-to-back in microseconds; with backoff one tick records exactly one
    attempt and the timer's next due time moves past `now`."""
    store = SqliteStore(":memory:", clock=ManualClock())
    reg = ChartRegistry().register("c", _poison_chart())
    rt = DurableRuntime(store, reg)
    rt.start("c", "s1")
    rt.enqueue("s1", "go")
    rt._run = lambda rec, sid, ev: (_ for _ in ()).throw(RuntimeError("down for now"))

    t0 = store.clock.now()
    rt.tick()  # one failed attempt at t0

    # The event is backed off: still live, but its next due time is in the future.
    assert store.next_due() is not None, "event must not be lost"
    assert store.next_due() > t0, "failed delivery must be rescheduled into the future"

    # A fixed-clock drain does NOT keep burning attempts — nothing is due yet.
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
    rt._run = lambda rec, sid, ev: (_ for _ in ()).throw(RuntimeError("transient blip"))

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

    def crash(rec, sid, ev):
        raise KeyboardInterrupt("operator hit Ctrl-C mid-delivery")
    rt._run = crash

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
    """SCP-C-013 / SCP-Q-002: a store/infrastructure error (sqlite3.OperationalError from the
    persistence layer — disk full / db locked / I/O) is NOT the event's fault. It must be
    treated like a crash: propagate, roll back, burn no attempt, and be retried indefinitely —
    never counted toward the poison cap and never dead-lettered."""
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
            raise sqlite3.OperationalError("database is locked")
        return real_save(*a, **k)
    store.save_session = flaky_save

    for _ in range(3):
        try:
            rt.tick()
        except sqlite3.OperationalError:
            pass  # infra error propagates (retry indefinitely), like a crash
        row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
        assert row is not None, "healthy event survives the outage"
        assert row["attempts"] == 0, "an infra error must not burn a delivery attempt"
        assert store.dead_letters() == [], "an infra error must never dead-letter a healthy event"

    rt.tick()  # DB recovered -> delivered normally
    assert "b" in rt.load("s1").configuration, rt.load("s1").configuration
    assert store.dead_letters() == []
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

    real = rt._run
    def run(rec, sid, ev):
        if sid == "poison":
            raise sqlite3.OperationalError("handler's own resource is locked")
        return real(rec, sid, ev)
    rt._run = run

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
    rt._run = lambda rec, sid, ev: (_ for _ in ()).throw(RuntimeError("always fails"))

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
    rt._run = lambda rec, sid, ev: (_ for _ in ()).throw(RuntimeError("always fails"))

    rt.tick()  # a single tick at a fixed clock

    row = store.conn.execute("SELECT attempts FROM timers WHERE session_id=?", ("s1",)).fetchone()
    assert row is not None and row["attempts"] == 1, "only one attempt burned per fixed-clock tick"
    assert store.dead_letters() == [], "zero backoff must not collapse all attempts into one tick"
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
