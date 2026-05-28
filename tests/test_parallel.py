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
