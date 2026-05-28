"""Default synchronous in-memory event queue with an injectable clock.

Immediate ``send``/``raise`` is handled directly by the algorithm; this queue
holds *delayed* sends until their due time, surfaced via :meth:`tick`.  An
injectable clock makes delayed behaviour testable without real time (à la Sismic).
"""
from __future__ import annotations

import itertools
import time
from typing import Callable, List, Optional

from .events import Event


class Clock:
    """Real wall-clock time in seconds."""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """Controllable clock for tests/simulation."""

    def __init__(self, start: float = 0.0):
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def set(self, seconds: float) -> None:
        self._t = seconds


class MemoryEventQueue:
    def __init__(self, clock: Optional[Clock] = None):
        self.clock = clock or Clock()
        self._seq = itertools.count()
        # entries: (due_time, seq, event, sendid)
        self._pending: List[tuple] = []

    def send(self, event: Event, *, delay: int = 0, sendid: Optional[str] = None) -> None:
        due = self.clock.now() + (delay / 1000.0)
        self._pending.append((due, next(self._seq), event, sendid))

    def cancel(self, sendid: str) -> None:
        self._pending = [e for e in self._pending if e[3] != sendid]

    def tick(self, now: Optional[float] = None) -> List[Event]:
        if now is None:
            now = self.clock.now()
        ready = [e for e in self._pending if e[0] <= now]
        ready.sort(key=lambda e: (e[0], e[1]))
        due_ids = {id(e) for e in ready}
        self._pending = [e for e in self._pending if id(e) not in due_ids]
        return [e[2] for e in ready]

    @property
    def empty(self) -> bool:
        return len(self._pending) == 0
