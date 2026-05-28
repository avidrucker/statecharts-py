"""A durable, restart-surviving workflow on a real SQLite file.

A session enters a "waiting" state that schedules a delayed timeout, the process
"crashes" (we close the store), and a fresh process reopens the same database file
and drives the persisted timer to completion. Run:

    python3 examples/durable_workflow.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statecharts import (  # noqa: E402
    ChartRegistry, DurableRuntime, SqliteStore, ManualClock,
    statechart, state, final, on, send_after,
)


def chart():
    return statechart({"initial": "idle"},
        state({"id": "idle"}, on("submit", "awaiting_approval")),
        state({"id": "awaiting_approval"},
            # auto-escalate if no decision within (here) 1000ms of virtual time
            *send_after({"id": "sla", "event": "escalate", "delay": 1000}),
            on("approve", "approved"),
            on("escalate", "escalated"),
        ),
        final({"id": "approved"}),
        final({"id": "escalated"}),
    )


def main():
    path = tempfile.mktemp(suffix=".scdb")
    registry = ChartRegistry().register("approval", chart())
    try:
        # ---- process 1 ----
        store = SqliteStore(path, clock=ManualClock())
        rt = DurableRuntime(store, registry)
        rt.start("approval", "req-42")
        rt.enqueue("req-42", "submit")
        rt.tick()
        print(f"process 1: {sorted(rt.load('req-42').configuration)}  "
              f"(next timer due at t={store.next_due()})")
        store.close()
        print("--- process 1 exits; the SLA timer is persisted on disk ---")

        # ---- process 2: reopened later, no decision arrived ----
        clock = ManualClock(); clock.advance(2.0)  # 2s later
        store = SqliteStore(path, clock=clock)
        rt = DurableRuntime(store, registry)
        print(f"process 2: reloaded as {sorted(rt.load('req-42').configuration)}")
        rt.tick()  # the persisted SLA timer is now due
        wm = rt.load("req-42")
        print(f"process 2: {sorted(wm.configuration)}  running={wm.running}")
        store.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    main()
