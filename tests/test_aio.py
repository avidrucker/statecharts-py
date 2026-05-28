import asyncio

from statecharts import (
    AsyncSession, statechart, state, on, on_entry, transition, send_after, Send, OnEntry,
)


def run(coro):
    return asyncio.run(coro)


def test_async_processes_external_events():
    chart = statechart({"initial": "idle"},
        state({"id": "idle"}, on("start", "working")),
        state({"id": "working"}, on("stop", "idle")),
    )

    async def scenario():
        s = AsyncSession(chart)
        task = asyncio.create_task(s.run())
        await s.send("start")
        await asyncio.sleep(0.02)
        assert s.in_state("working")
        await s.send("stop")
        await asyncio.sleep(0.02)
        assert s.in_state("idle")
        task.cancel()

    run(scenario())


def test_async_delayed_send_fires_in_real_time():
    # 60ms delayed self-event drives idle -> done
    chart = statechart({"initial": "idle"},
        state({"id": "idle"},
            on_entry(Send("tick", delay=60)),
            on("tick", "done"),
        ),
        state({"id": "done"}),
    )

    async def scenario():
        s = AsyncSession(chart)
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.02)
        assert s.in_state("idle")  # not yet
        await asyncio.sleep(0.08)  # past 60ms
        assert s.in_state("done")
        task.cancel()

    run(scenario())


def test_async_run_returns_when_machine_finishes():
    from statecharts import final
    chart = statechart({"initial": "go"},
        state({"id": "go"}, on_entry(Send("end", delay=30)), on("end", "fin")),
        final({"id": "fin"}),
    )

    async def scenario():
        s = AsyncSession(chart)
        wm = await asyncio.wait_for(s.run(), timeout=1.0)
        assert not wm.running
        assert "fin" in wm.configuration

    run(scenario())
