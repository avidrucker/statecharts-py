from statecharts import (
    Session,
    statechart,
    state,
    transition,
    on,
    send_after,
    ManualClock,
    MemoryEventQueue,
    make_chart,
    make_env,
)


def build():
    chart = make_chart(statechart({"initial": "idle"},
        state({"id": "idle"}, on("begin", "waiting")),
        state({"id": "waiting"},
            *send_after({"id": "to", "event": "expired", "delay": 1000}),
            on("expired", "done"),
            on("cancel", "idle"),
        ),
        state({"id": "done"}),
    ))
    clock = ManualClock()
    env = make_env(chart, event_queue=MemoryEventQueue(clock=clock))
    return Session(chart, env=env), clock


def test_delayed_event_fires_after_delay():
    s, clock = build()
    s.send("begin")
    assert s.in_state("waiting")
    clock.advance(0.5)  # not yet due
    s.send("noop")
    assert s.in_state("waiting")
    clock.advance(0.6)  # now past 1000ms
    s.send("noop")  # any step drains due delayed events
    assert s.in_state("done")


def test_delayed_event_cancelled_on_exit():
    s, clock = build()
    s.send("begin")
    s.send("cancel")  # exits "waiting" -> cancels the pending send
    assert s.in_state("idle")
    clock.advance(2.0)
    s.send("noop")
    assert s.in_state("idle")  # expired never delivered
