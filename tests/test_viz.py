from statecharts import (
    statechart, state, parallel, final, history, transition, on, to_mermaid, to_dot,
)


def sample():
    return statechart({"initial": "idle"},
        state({"id": "idle"}, on("start", "work")),
        state({"id": "work", "initial": "a"},
            state({"id": "a"}, on("next", "b")),
            state({"id": "b"}, on("finish", "done")),
        ),
        final({"id": "done"}),
    )


def test_mermaid_basic_structure():
    out = to_mermaid(sample())
    assert out.startswith("stateDiagram-v2")
    assert "[*] --> idle" in out
    assert "idle --> work : start" in out
    assert "state work {" in out
    assert "a --> b : next" in out
    # transition into a final also points the final to [*]
    assert "done --> [*]" in out


def test_mermaid_parallel_uses_divider():
    chart = statechart({"initial": "p"},
        parallel({"id": "p"},
            state({"id": "r1", "initial": "x"}, state({"id": "x"})),
            state({"id": "r2", "initial": "y"}, state({"id": "y"})),
        ),
    )
    out = to_mermaid(chart)
    assert "state p {" in out
    assert "--" in out  # region divider


def test_mermaid_history_rendered():
    chart = statechart({"initial": "m"},
        state({"id": "m", "initial": "one"},
            history({"id": "h", "type": "shallow"}, transition({"target": "one"})),
            state({"id": "one"}),
        ),
    )
    out = to_mermaid(chart)
    assert "history" in out


def test_dot_basic_structure():
    out = to_dot(sample())
    assert out.startswith("digraph statechart {")
    assert "subgraph cluster_work {" in out
    assert "idle -> work" in out
    assert "shape=doublecircle" in out  # the final state
    assert out.rstrip().endswith("}")


def test_safe_ids_for_dotted_names():
    # auto-generated / dotted ids must be sanitized to valid identifiers
    chart = statechart({},
        state({"id": "a.b.c"}, on("go", "d")),
        state({"id": "d"}),
    )
    out = to_mermaid(chart)
    assert "a_b_c" in out
    assert "a.b.c -->" not in out  # raw dotted id should not leak into an edge
