from statecharts import (
    Session,
    statechart,
    state,
    final,
    transition,
    on,
    on_entry,
    handle,
    choice,
    Script,
)
from statecharts import ops


def test_eventless_transition_settles_in_macrostep():
    chart = statechart({"initial": "a"},
        state({"id": "a"}, on("go", "b")),
        state({"id": "b"},
            on_entry(Script(lambda env, data: [ops.assign("via", "b")])),
            transition({"target": "c"}),  # eventless: immediately move on
        ),
        state({"id": "c"}),
    )
    s = Session(chart)
    s.send("go")
    assert s.in_state("c")
    assert s.data["via"] == "b"


def test_choice_decision_state():
    chart = statechart({"initial": "start"},
        state({"id": "start"},
            on_entry(Script(lambda env, data: [ops.assign("score", data.get("score", 0))])),
            on("submit", "check"),
        ),
        choice({"id": "check"},
            lambda env, data: data.get("score", 0) >= 50, "approved",
            lambda env, data: data.get("score", 0) > 0, "review",
            "rejected",
        ),
        state({"id": "approved"}),
        state({"id": "review"}),
        state({"id": "rejected"}),
    )
    s = Session(chart)
    s.send("submit")
    assert s.in_state("rejected")  # score 0 -> else

    s2 = Session(chart)
    s2.send("submit", {"score": 75})  # event data not stored, so still rejected here
    # Drive via assign instead:
    s3 = Session(chart)
    s3.wm = s3.wm.replace(datamodel={"score": 75})
    s3.send("submit")
    assert s3.in_state("approved")


def test_top_level_final_stops_machine():
    chart = statechart({"initial": "run"},
        state({"id": "run"}, on("finish", "done")),
        final({"id": "done"}),
    )
    s = Session(chart)
    assert s.running
    s.send("finish")
    assert s.in_state("done")
    assert not s.running
    # further events are ignored
    s.send("anything")
    assert s.in_state("done")


def test_done_state_event_for_compound():
    chart = statechart({"initial": "outer"},
        state({"id": "outer", "initial": "work"},
            state({"id": "work"}, on("complete", "fin")),
            final({"id": "fin"}),
            transition({"event": "done.state.outer", "target": "after"}),
        ),
        state({"id": "after"}),
    )
    s = Session(chart)
    s.send("complete")
    assert s.in_state("after")
