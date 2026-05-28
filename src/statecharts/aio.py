"""Asyncio runtime: drive a session over real time.

The functional core stays synchronous; :class:`AsyncSession` owns an event loop
task that pulls external events from an :class:`asyncio.Queue` and wakes up exactly
when the next delayed ``<send>`` is due (no busy polling).  Delays fire in real
wall-clock time.
"""
from __future__ import annotations

import asyncio
from typing import FrozenSet, Optional

from .algorithm import initialize, process_event
from .chart import Chart, make_chart
from .elements import StateNode
from .environment import Environment, make_env
from .event_queue import Clock, MemoryEventQueue
from .events import coerce_event
from .working_memory import WorkingMemory


class AsyncSession:
    def __init__(self, chart, env: Optional[Environment] = None, data: Optional[dict] = None, **env_kwargs):
        if isinstance(chart, StateNode):
            chart = make_chart(chart)
        if not isinstance(chart, Chart):
            raise TypeError("chart must be a StateNode or Chart")
        if env is None:
            env_kwargs.setdefault("event_queue", MemoryEventQueue(clock=Clock()))
            env = make_env(chart, **env_kwargs)
        self.env = env
        self.wm: WorkingMemory = initialize(self.env, data)
        self._external: asyncio.Queue = asyncio.Queue()
        self._stopped = asyncio.Event()

    # -- inspection ---------------------------------------------------------
    @property
    def configuration(self) -> FrozenSet[str]:
        return self.wm.configuration

    @property
    def data(self) -> dict:
        return dict(self.wm.datamodel)

    @property
    def running(self) -> bool:
        return self.wm.running

    def in_state(self, sid: str) -> bool:
        return sid in self.wm.configuration

    # -- driving ------------------------------------------------------------
    async def send(self, event, data: Optional[dict] = None) -> None:
        await self._external.put(coerce_event(event, data))

    def _next_delay(self) -> Optional[float]:
        eq = self.env.event_queue
        pending = getattr(eq, "_pending", None)
        if not pending:
            return None
        now = eq.clock.now()
        return max(0.0, min(e[0] for e in pending) - now)

    def _drain_due(self) -> None:
        eq = self.env.event_queue
        tick = getattr(eq, "tick", None)
        if tick is None:
            return
        while self.wm.running:
            due = eq.tick()
            if not due:
                break
            for ev in due:
                self.wm = process_event(self.env, self.wm, ev)
                if not self.wm.running:
                    return

    async def run(self) -> WorkingMemory:
        """Run until the machine reaches a top-level final state (or is stopped)."""
        try:
            while self.wm.running:
                self._drain_due()
                if not self.wm.running:
                    break
                timeout = self._next_delay()
                try:
                    ev = await asyncio.wait_for(self._external.get(), timeout)
                except asyncio.TimeoutError:
                    continue  # a delayed send came due; loop to drain it
                self.wm = process_event(self.env, self.wm, ev)
        finally:
            self._stopped.set()
        return self.wm

    async def wait_stopped(self) -> None:
        await self._stopped.wait()
