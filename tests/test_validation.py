"""Chart construction validates that all targets resolve (fail fast, clear message)."""
from statecharts import statechart, state, parallel, final, history, transition, on, make_chart


def _expect_value_error(build, needle):
    try:
        build()
    except ValueError as e:
        assert needle in str(e), f"message {str(e)!r} missing {needle!r}"
        return
    raise AssertionError(f"expected ValueError mentioning {needle!r}")


def test_unknown_transition_target_rejected():
    _expect_value_error(
        lambda: make_chart(statechart({"initial": "a"},
            state({"id": "a"}, on("go", "NOPE")))),
        "NOPE",
    )


def test_unknown_initial_target_rejected():
    _expect_value_error(
        lambda: make_chart(statechart({"initial": "ghost"},
            state({"id": "a"}))),
        "ghost",
    )


def test_unknown_history_default_rejected():
    _expect_value_error(
        lambda: make_chart(statechart({"initial": "m"},
            state({"id": "m", "initial": "one"},
                history({"id": "h", "type": "shallow"}, transition({"target": "missing"})),
                state({"id": "one"})))),
        "missing",
    )


def test_duplicate_id_still_rejected():
    _expect_value_error(
        lambda: make_chart(statechart({"initial": "a"},
            state({"id": "a"}), state({"id": "a"}))),
        "Duplicate",
    )


def test_valid_chart_builds_fine():
    # a well-formed chart with every construct must NOT raise
    chart = make_chart(statechart({"initial": "p"},
        parallel({"id": "p"},
            state({"id": "r1", "initial": "x"},
                history({"id": "h"}, transition({"target": "x"})),
                state({"id": "x"}, on("go", "y")),
                state({"id": "y"}, on("back", "h"))),
            state({"id": "r2", "initial": "m"},
                state({"id": "m"}, on("done", "f"))),
        ),
        final({"id": "f"}),
    ))
    assert chart.node("x") is not None
