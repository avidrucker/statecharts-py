"""Smoke test guarding the benchmark harness (issue #10).

The benchmark's numbers are only meaningful if each chart shape actually transitions
on a ``go`` event. If a builder regressed, ``process_event`` would be a no-op and the
harness would report a huge, meaningless events/sec (and the O(N^2) scaling finding
would silently evaporate). These tests make that failure mode visible in the normal
test run, and exercise the harness's own self-check.
"""
import os
import sys

# The harness lives at repo-root/bench (outside the packaged src/); add it to the path.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "bench"))

import run_bench  # noqa: E402

from statecharts import initialize, make_chart, make_env, process_event  # noqa: E402


def _config_after_one_event(root):
    env = make_env(make_chart(root))
    wm = initialize(env)
    before = wm.configuration
    wm = process_event(env, wm, "go")
    return before, wm.configuration


def test_every_shape_transitions_on_go():
    """Each benchmark chart must change configuration on a single 'go' — otherwise
    the harness would be timing a no-op."""
    for name, build in (
        ("wide", lambda: run_bench.wide_chart(4)),
        ("deep", lambda: run_bench.deep_chart(4)),
        ("loop", run_bench.loop_chart),
    ):
        before, after = _config_after_one_event(build())
        assert after != before, f"{name} chart did not transition on 'go'"
        assert after, f"{name} chart ended with an empty configuration"


def test_wide_fires_one_transition_per_region():
    """The wide chart's premise (one transition per region per event) must hold, or
    the O(N^2) scaling claim is measuring the wrong thing."""
    before, after = _config_after_one_event(run_bench.wide_chart(4))
    # all 4 regions moved from r{i}_0 to r{i}_1
    assert after == frozenset({"p"} | {f"r{i}" for i in range(4)}
                              | {f"r{i}_1" for i in range(4)}), after


def test_drive_self_check_rejects_a_no_op_chart():
    """drive() must raise if the chart doesn't transition — the harness's guard
    against silently benchmarking nothing."""
    from statecharts import state, statechart

    # a single atomic state with no matching transition: 'go' is a no-op
    dead = statechart({"initial": "x"}, state({"id": "x"}))
    try:
        run_bench.drive(dead, events=10, alloc=False)
    except AssertionError as exc:
        assert "did not transition" in str(exc)
    else:
        raise AssertionError("drive() accepted a no-op chart — self-check is broken")
