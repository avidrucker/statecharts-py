"""A stateful :class:`Session` facade over the functional core.

The core is intentionally functional (``process_event(env, wm, ev) -> wm``).
``Session`` wraps it for the common in-process case, holding the current working
memory and draining any due delayed events from the queue after each step.
"""
from __future__ import annotations

from typing import Any, FrozenSet, Optional

from .algorithm import initialize, process_event
from .chart import Chart, make_chart
from .elements import StateNode
from .environment import Environment, make_env
from .working_memory import WorkingMemory


class Session:
    def __init__(self, chart, env: Optional[Environment] = None, **env_kwargs):
        if isinstance(chart, StateNode):
            chart = make_chart(chart)
        if not isinstance(chart, Chart):
            raise TypeError("chart must be a StateNode or Chart")
        self.env = env or make_env(chart, **env_kwargs)
        self.wm: WorkingMemory = initialize(self.env)

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

    def send(self, event, data: Optional[dict] = None) -> "Session":
        from .events import coerce_event

        self.wm = process_event(self.env, self.wm, coerce_event(event, data))
        self._drain_delayed()
        return self

    def _drain_delayed(self) -> None:
        # Deliver any delayed sends that are now due (uses the queue's clock).
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
