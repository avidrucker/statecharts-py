"""AsyncSession driving a self-cycling, timer-based statechart in real time.

A traffic light advances on delayed self-sends (no external input). The asyncio
runtime sleeps until each timer is due. Run:

    python3 examples/async_traffic_light.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statecharts import (  # noqa: E402
    AsyncSession, statechart, state, on, on_entry, send_after, Script,
)


def announce(name):
    return Script(lambda env, data: print(f"  light is now {name.upper()}") or [])


# green --(2s)--> yellow --(1s)--> red --(2s)--> green ...
light = statechart({"initial": "green"},
    state({"id": "green"},
        on_entry(announce("green")),
        *send_after({"id": "t", "event": "next", "delay": 200}),
        on("next", "yellow")),
    state({"id": "yellow"},
        on_entry(announce("yellow")),
        *send_after({"id": "t", "event": "next", "delay": 100}),
        on("next", "red")),
    state({"id": "red"},
        on_entry(announce("red")),
        *send_after({"id": "t", "event": "next", "delay": 200}),
        on("next", "green")),
)


async def main():
    print("running the light for ~0.7s of real time:")
    s = AsyncSession(light)
    task = asyncio.create_task(s.run())
    await asyncio.sleep(0.7)
    task.cancel()
    print(f"stopped while {sorted(s.configuration)}")


if __name__ == "__main__":
    asyncio.run(main())
