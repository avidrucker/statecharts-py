from statecharts import Session, statechart, state, on_entry, transition, on, handle, Script
from statecharts import ops


def test_initial_configuration():
    chart = statechart({"initial": "idle"},
        state({"id": "idle"}, on("start", "working")),
        state({"id": "working"}, on("done", "idle")),
    )
    s = Session(chart)
    assert s.in_state("idle")
    assert not s.in_state("working")
    assert s.running


def test_default_initial_is_first_child():
    chart = statechart({},
        state({"id": "a"}, on("go", "b")),
        state({"id": "b"}),
    )
    s = Session(chart)
    assert s.in_state("a")


def test_simple_transition():
    chart = statechart({"initial": "idle"},
        state({"id": "idle"}, on("start", "working")),
        state({"id": "working"}, on("done", "idle")),
    )
    s = Session(chart)
    s.send("start")
    assert s.in_state("working")
    s.send("done")
    assert s.in_state("idle")


def test_unmatched_event_is_noop():
    chart = statechart({"initial": "idle"}, state({"id": "idle"}, on("start", "go")), state({"id": "go"}))
    s = Session(chart)
    s.send("nope")
    assert s.in_state("idle")


def test_compound_entry_enters_child():
    chart = statechart({"initial": "outer"},
        state({"id": "outer", "initial": "inner2"},
            state({"id": "inner1"}),
            state({"id": "inner2"}),
        ),
    )
    s = Session(chart)
    assert s.in_state("outer")
    assert s.in_state("inner2")
    assert not s.in_state("inner1")


def test_parent_internal_transition_applies_to_children():
    toggled = {}
    def flip(env, data):
        return [ops.assign("help", not data.get("help", False))]

    chart = statechart({"initial": "editing"},
        state({"id": "editing", "initial": "form"},
            transition({"event": "toggle", "type": "internal"}, Script(flip)),
            state({"id": "form"}, on("validate", "validating")),
            state({"id": "validating"}, on("ok", "form")),
        ),
    )
    s = Session(chart)
    assert s.in_state("form")
    s.send("toggle")
    assert s.data["help"] is True
    # internal transition did not exit the child
    assert s.in_state("form") and s.in_state("editing")
    s.send("validate")
    assert s.in_state("validating")
    s.send("toggle")  # still works from a different child
    assert s.data["help"] is False
    assert s.in_state("validating")


def test_assign_and_handle_mutate_data():
    chart = statechart({"initial": "c"},
        state({"id": "c"},
            handle("inc", lambda env, data: [ops.assign("n", data.get("n", 0) + 1)]),
        ),
    )
    s = Session(chart)
    s.send("inc").send("inc").send("inc")
    assert s.data["n"] == 3
    assert s.in_state("c")  # targetless: never left


def test_datamodel_initialized_on_entry():
    chart = statechart({"initial": "a"},
        state({"id": "a"}, on("go", "b")),
        state({"id": "b"}),
    )
    from statecharts import data_model
    chart = statechart({"initial": "a"},
        data_model({"count": 0}),
        state({"id": "a"}, on("go", "b")),
        state({"id": "b"}),
    )
    s = Session(chart)
    assert s.data["count"] == 0
