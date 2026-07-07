from statecharts import Session, statechart, state, parallel, final, transition, on


def make_parallel_chart():
    return statechart({"initial": "p"},
        parallel({"id": "p"},
            state({"id": "a", "initial": "a1"},
                state({"id": "a1"}, on("a-go", "a2")),
                state({"id": "a2"}),
            ),
            state({"id": "b", "initial": "b1"},
                state({"id": "b1"}, on("b-go", "b2")),
                state({"id": "b2"}),
            ),
        ),
    )


def test_parallel_enters_all_regions():
    s = Session(make_parallel_chart())
    assert s.in_state("p")
    assert s.in_state("a") and s.in_state("a1")
    assert s.in_state("b") and s.in_state("b1")


def test_parallel_regions_independent():
    s = Session(make_parallel_chart())
    s.send("a-go")
    assert s.in_state("a2")
    assert s.in_state("b1")  # b region untouched
    s.send("b-go")
    assert s.in_state("a2") and s.in_state("b2")


def test_parallel_done_event_when_all_regions_final():
    chart = statechart({"initial": "p"},
        parallel({"id": "p"},
            state({"id": "a", "initial": "a1"},
                state({"id": "a1"}, on("a-fin", "af")),
                final({"id": "af"}),
            ),
            state({"id": "b", "initial": "b1"},
                state({"id": "b1"}, on("b-fin", "bf")),
                final({"id": "bf"}),
            ),
        ),
        transition({"event": "done.state.p", "target": "alldone"}),
        state({"id": "alldone"}),
    )
    s = Session(chart)
    s.send("a-fin")
    assert not s.in_state("alldone")  # only one region final
    s.send("b-fin")
    assert s.in_state("alldone")  # done.state.p fired -> moved on


def test_conflicting_transitions_resolved_by_document_order():
    """When two parallel regions each have a transition on the same event that BOTH
    exit the whole parallel, their exit sets overlap -> the transitions conflict.
    removeConflictingTransitions must keep exactly one: the first in document order.

    This is the only scenario that reaches _remove_conflicting_transitions with a
    real conflict (parent/child conflicts are resolved earlier, in selection), so it
    is the direct guard for that function — including the exit-set caching in #8.
    """
    chart = statechart({"initial": "p"},
        parallel({"id": "p"},
            state({"id": "r1", "initial": "r1a"},
                state({"id": "r1a"}, on("e", "out1")),  # exits the whole parallel
            ),
            state({"id": "r2", "initial": "r2a"},
                state({"id": "r2a"}, on("e", "out2")),  # also exits the whole parallel
            ),
        ),
        state({"id": "out1"}),
        state({"id": "out2"}),
    )
    s = Session(chart)
    s.send("e")
    # document order: r1's transition precedes r2's, so r1 wins and r2 is preempted.
    assert s.configuration == frozenset({"out1"}), s.configuration


def test_exit_set_clears_all_active_descendants_of_domain():
    """Exiting a nested compound must remove *every* active descendant of the
    transition's domain (exercises the active-children descent in _compute_exit_set,
    #14) while leaving states outside the domain untouched."""
    chart = statechart({"initial": "app"},
        state({"id": "app", "initial": "work"},
            state({"id": "work", "initial": "w_outer"},
                state({"id": "w_outer", "initial": "w_inner"},
                    state({"id": "w_inner"}, on("reset", "app")),  # exits `work` subtree
                ),
            ),
            on("bg", "done"),
        ),
        state({"id": "done"}),
    )
    s = Session(chart)
    assert s.configuration == frozenset({"app", "work", "w_outer", "w_inner"})
    s.send("reset")  # domain is `app`; every descendant exits, re-enter app->work->...
    assert s.configuration == frozenset({"app", "work", "w_outer", "w_inner"})
    s.send("bg")
    assert s.configuration == frozenset({"done"})
