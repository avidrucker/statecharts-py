"""A small but realistic chart: a payment that retries on failure (max 3),
times out while awaiting confirmation, and ends in a final state.

Run:  python3 examples/payment_flow.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statecharts import (  # noqa: E402
    Session,
    statechart,
    state,
    final,
    transition,
    on,
    on_entry,
    data_model,
    send_after,
    Script,
    ManualClock,
    MemoryEventQueue,
    make_chart,
    make_env,
    ops,
)


def bump_attempts(env, data):
    return [ops.assign("attempts", data.get("attempts", 0) + 1)]


chart = make_chart(statechart({"initial": "idle"},
    data_model({"attempts": 0}),

    state({"id": "idle"}, on("pay", "charging")),

    state({"id": "charging"},
        on_entry(Script(bump_attempts)),
        on("charge.ok", "confirming"),
        # retry while under the cap...
        transition({"event": "charge.error",
                    "cond": lambda env, data: data["attempts"] < 3,
                    "target": "charging"}),
        # ...otherwise give up
        transition({"event": "charge.error", "target": "failed"}),
    ),

    state({"id": "confirming"},
        *send_after({"id": "timeout", "event": "confirm.timeout", "delay": 5000}),
        on("confirm.ok", "succeeded"),
        on("confirm.timeout", "failed"),
    ),

    final({"id": "succeeded"}),
    final({"id": "failed"}),
))


def show(s, label):
    print(f"{label:24} config={sorted(s.configuration)}  attempts={s.data['attempts']}  running={s.running}")


def scenario_retry_then_succeed():
    print("\n--- scenario: two failures, then success ---")
    s = Session(chart, env=make_env(chart, event_queue=MemoryEventQueue(clock=ManualClock())))
    show(s, "start")
    s.send("pay");          show(s, "pay")
    s.send("charge.error"); show(s, "charge.error #1")
    s.send("charge.error"); show(s, "charge.error #2")
    s.send("charge.ok");    show(s, "charge.ok")
    s.send("confirm.ok");   show(s, "confirm.ok")


def scenario_timeout():
    print("\n--- scenario: confirmation times out ---")
    clock = ManualClock()
    s = Session(chart, env=make_env(chart, event_queue=MemoryEventQueue(clock=clock)))
    s.send("pay")
    s.send("charge.ok");    show(s, "charge.ok")
    clock.advance(6.0)      # past the 5s timeout
    s.send("tick");         show(s, "after timeout")


def scenario_exhaust_retries():
    print("\n--- scenario: three failures exhaust retries ---")
    s = Session(chart, env=make_env(chart, event_queue=MemoryEventQueue(clock=ManualClock())))
    s.send("pay")
    for i in range(3):
        s.send("charge.error")
    show(s, "after 3 errors")


if __name__ == "__main__":
    scenario_retry_then_succeed()
    scenario_timeout()
    scenario_exhaust_retries()
