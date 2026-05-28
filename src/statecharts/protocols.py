"""The four swappable seams, as structural :class:`typing.Protocol` types.

These are the heart of the Fulcrologic design: the processing algorithm depends
only on these abstractions, so storage, expression evaluation, and event delivery
can each be replaced (in-memory now; durable/distributed later).
"""
from __future__ import annotations

from typing import Any, Iterable, List, Protocol, runtime_checkable

from .events import Event


@runtime_checkable
class DataModel(Protocol):
    """How session data is stored, read, and mutated by ops."""

    def get(self, store: dict, location: str, default: Any = None) -> Any: ...

    def as_data(self, store: dict) -> dict:
        """A read view passed to expressions (may be the store itself)."""

    def transact(self, store: dict, ops: Iterable[Any]) -> dict:
        """Apply ops, returning the new store (may mutate-and-return ``store``)."""


@runtime_checkable
class ExecutionModel(Protocol):
    """How guard/action *expressions* are interpreted."""

    def run(self, env: Any, data: dict, expr: Any) -> Any: ...


@runtime_checkable
class EventQueue(Protocol):
    """How (possibly delayed) events are queued and delivered to a session."""

    def send(self, event: Event, *, delay: int = 0, sendid: str = None) -> None: ...

    def cancel(self, sendid: str) -> None: ...

    def tick(self, now: float) -> List[Event]:
        """Return (and remove) events whose delay has elapsed by ``now``."""
