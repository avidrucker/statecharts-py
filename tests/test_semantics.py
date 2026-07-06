"""Edge-case semantics the W3C IRP suite leaves *implicit* (issue #3).

Each test pins a behavior that is intended and defensible but not obviously covered
elsewhere — including two features this port supports that upstream
``fulcrologic/statecharts`` deliberately does not (system-variable enforcement, late
binding). See docs/research/improvement-areas.md (RQ2/RQ3).

Observation trick: a ``Script`` whose lambda appends to a closure list and returns no
ops lets us record *which* executable-content items ran, and in what order, without
touching the data model (the ``Assign`` element requires declared locations; the
``ops``/``Script`` path does not).
"""
from statecharts import (
    Session,
    statechart,
    state,
    parallel,
    history,
    final,
    transition,
    on,
    on_entry,
    handle,
    Script,
    Assign,
    Send,
    data_model,
)


def _boom(env, data):
    raise RuntimeError("boom")


def _note(log, tag):
    """A Script that records `tag` into closure list `log` and emits no ops."""
    return Script(lambda env, data, _l=log, _t=tag: (_l.append(_t), [])[1])


def _recorder(log, tag):
    """A targetless `handle` fn that records `tag` when its event is processed."""
    return lambda env, data, _l=log, _t=tag: (_l.append(_t), [])[1]


# ---------------------------------------------------------------------------
# Gap 1 — deep history INSIDE a parallel region, crossed by a full exit/re-entry
# ---------------------------------------------------------------------------


def test_deep_history_inside_parallel_region_crossing():
    """Exiting a whole parallel then re-entering via one region's deep history
    restores that region's nested atomic, leaves the *other* region at its default,
    and re-activates both regions."""
    chart = statechart({"initial": "p"},
        parallel({"id": "p"},
            state({"id": "a", "initial": "a1"},
                history({"id": "ha", "type": "deep"}, transition({"target": "a1"})),
                state({"id": "a1", "initial": "a1x"},
                    state({"id": "a1x"}, on("deep", "a1y")),
                    state({"id": "a1y"}),
                ),
                state({"id": "a2"}),
            ),
            state({"id": "b", "initial": "b1"},
                state({"id": "b1"}, on("b-go", "b2")),
                state({"id": "b2"}),
            ),
        ),
        transition({"event": "leave", "target": "paused"}),  # exits the whole parallel
        state({"id": "paused"}, on("resume", "ha")),          # re-enter via region a history
    )
    s = Session(chart)
    s.send("deep")    # region a: a1x -> a1y (deep)
    s.send("b-go")    # region b: b1 -> b2
    assert s.in_state("a1y") and s.in_state("b2")

    s.send("leave")
    assert s.configuration == frozenset({"paused"})

    s.send("resume")  # re-enter targeting region a's deep history
    # region a is restored to its historical *deep* atomic
    assert s.in_state("a1y")
    # region b was NOT covered by history -> back to its default, not b2
    assert s.in_state("b1") and not s.in_state("b2")
    # both regions of the parallel are active again
    assert s.in_state("a") and s.in_state("b") and s.in_state("p")


# ---------------------------------------------------------------------------
# Gap 2 — error.execution is delivered BEFORE done.state.* (W3C §3.13 order)
# ---------------------------------------------------------------------------


def test_error_execution_precedes_done_state_event():
    """When executable content on the transition into a compound state's final child
    raises, the resulting error.execution must be queued *before* the done.state.*
    event that the final entry generates."""
    order = []
    chart = statechart({"initial": "outer"},
        state({"id": "outer", "initial": "work"},
            state({"id": "work"},
                transition({"event": "complete", "target": "fin"}, Script(_boom)),
            ),
            final({"id": "fin"}),
        ),
        handle("error.execution", _recorder(order, "error.execution")),
        handle("done.state.outer", _recorder(order, "done.state.outer")),
    )
    s = Session(chart)
    s.send("complete")
    assert "error.execution" in order and "done.state.outer" in order
    assert order.index("error.execution") < order.index("done.state.outer"), order


# ---------------------------------------------------------------------------
# Gap 3 — assigning to a system variable raises error.execution and aborts the block
# ---------------------------------------------------------------------------


def test_system_variable_write_raises_and_aborts_block():
    """A `<assign>` to a system variable (`_sessionid`) raises error.execution
    (upstream does NOT enforce this) and, under the strict-by-default block
    semantics, the sibling content after it does not run."""
    log = []
    chart = statechart({"initial": "a"},
        state({"id": "a"},
            on_entry(
                Assign("_sessionid", "hacked"),  # illegal write -> error.execution
                _note(log, "after-bad-assign"),  # must be skipped (block aborts)
            ),
        ),
        handle("error.execution", _recorder(log, "error.execution")),
    )
    s = Session(chart)
    assert "error.execution" in log
    assert "after-bad-assign" not in log, log
    # the system variable was not corrupted
    assert s.data.get("_sessionid") in (None, "")


# ---------------------------------------------------------------------------
# Gap 4 — late binding: a <data> is undefined until its owning state is entered
# ---------------------------------------------------------------------------


def test_late_binding_defers_data_until_entry():
    """With `_binding="late"`, a declared variable exists but is None until its
    owning state is entered; early binding (the default) assigns it at init."""
    chart = statechart({"initial": "s1"},
        state({"id": "s1"}, on("go", "s2")),
        state({"id": "s2"}, data_model({"late_var": 42})),
    )
    # late binding
    s = Session(chart, extra={"_binding": "late"})
    assert "late_var" in s.data and s.data["late_var"] is None
    s.send("go")
    assert s.data["late_var"] == 42

    # early binding (default) assigns immediately, before s2 is ever entered
    early = Session(chart)
    assert early.data.get("late_var") == 42


# ---------------------------------------------------------------------------
# Gap 5 — error.communication is async and does NOT abort the surrounding block
# ---------------------------------------------------------------------------


def test_error_communication_is_async_and_non_aborting():
    """A `<send>` to a well-formed but unknown session raises error.communication
    (unlike error.execution) *without* aborting the block: sibling content after the
    failing send still runs, and the error event is delivered."""
    log = []
    chart = statechart({"initial": "a"},
        state({"id": "a"},
            on_entry(
                Send(event="hello", target="#_scxml_nonexistent"),  # unknown session
                _note(log, "sibling-ran"),                          # must still run
            ),
        ),
        handle("error.communication", _recorder(log, "error.communication")),
    )
    s = Session(chart)
    assert "sibling-ran" in log, log            # block was NOT aborted
    assert "error.communication" in log, log    # error still delivered
    # ordering: the sibling runs during the block; the error is delivered afterward
    assert log.index("sibling-ran") < log.index("error.communication"), log
