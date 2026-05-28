"""Native (non-XML) <invoke> via the invoke() builder + Session startup drain."""
from statecharts import (
    Session, statechart, state, final, on_entry, transition, invoke, data_model,
    Send, ops, Script,
)


def test_native_invoke_child_completes_at_startup():
    child = statechart({"initial": "go"},
        state({"id": "go"},
            on_entry(Send("hello", target="#_parent")),
            transition({"target": "fin"})),
        final({"id": "fin"}),
    )
    parent = statechart({"initial": "running"},
        state({"id": "running"},
            invoke({"id": "c", "content": child}),
            transition({"event": "hello"},
                       Script(lambda env, data: [ops.assign("heard", True)])),
            transition({"event": "done.invoke", "target": "finished"})),
        final({"id": "finished"}),
    )
    # Session.__init__ must drain the events the child emitted during initialize().
    s = Session(parent)
    assert s.in_state("finished")
    assert s.data.get("heard") is True


def test_native_invoke_param_seeds_declared_child_var():
    # child declares `threshold`, so the invoke <param> value (42) is applied
    child = statechart({"initial": "check"},
        data_model({"threshold": 0}),
        state({"id": "check"},
            transition({"cond": lambda env, data: data.get("threshold", 0) >= 10,
                        "target": "fin"},
                       Send("over", target="#_parent")),
            transition({"target": "fin"}, Send("under", target="#_parent"))),
        final({"id": "fin"}),
    )
    parent = statechart({"initial": "run"},
        state({"id": "run"},
            invoke({"content": child, "params": [("threshold", 42)]}),
            transition({"event": "over", "target": "high"}),
            transition({"event": "under", "target": "low"})),
        state({"id": "high"}),
        state({"id": "low"}),
    )
    s = Session(parent)
    assert s.in_state("high")  # 42 >= 10
