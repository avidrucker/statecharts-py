from statecharts import Session, statechart, state, history, transition, on


def test_shallow_history_restores_last_child():
    chart = statechart({"initial": "main"},
        state({"id": "main", "initial": "one"},
            history({"id": "h", "type": "shallow"}, transition({"target": "one"})),
            state({"id": "one"}, on("next", "two"), on("pause", "paused")),
            state({"id": "two"}, on("next", "three"), on("pause", "paused")),
            state({"id": "three"}, on("pause", "paused")),
        ),
        state({"id": "paused"}, on("resume", "h")),
    )
    s = Session(chart)
    s.send("next")  # one -> two
    assert s.in_state("two")
    s.send("pause")
    assert s.in_state("paused")
    s.send("resume")  # history -> two
    assert s.in_state("two")


def test_history_default_when_never_visited():
    chart = statechart({"initial": "paused"},
        state({"id": "main", "initial": "one"},
            history({"id": "h", "type": "shallow"}, transition({"target": "two"})),
            state({"id": "one"}),
            state({"id": "two"}),
        ),
        state({"id": "paused"}, on("resume", "h")),
    )
    s = Session(chart)
    assert s.in_state("paused")
    s.send("resume")  # never visited main -> use history default target "two"
    assert s.in_state("two")


def test_deep_history_restores_nested_atomic():
    chart = statechart({"initial": "main"},
        state({"id": "main", "initial": "outer"},
            history({"id": "dh", "type": "deep"}, transition({"target": "outer"})),
            state({"id": "outer", "initial": "x"},
                state({"id": "x"}, on("go", "y")),
                state({"id": "y"}, on("pause", "paused")),
            ),
        ),
        state({"id": "paused"}, on("resume", "dh")),
    )
    s = Session(chart)
    s.send("go")  # x -> y
    assert s.in_state("y")
    s.send("pause")
    assert s.in_state("paused")
    s.send("resume")  # deep history restores y, not the default x
    assert s.in_state("y")
    assert s.in_state("outer")
