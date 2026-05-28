"""Working memory: the serializable value that *is* a running session.

A step takes (env, working_memory, event) and returns a *new* working_memory.
Everything needed to resume a session lives here, so it can be persisted between
events (the durable-session story).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet


@dataclass(frozen=True)
class WorkingMemory:
    configuration: FrozenSet[str] = frozenset()
    datamodel: dict = field(default_factory=dict)
    history_value: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    running: bool = False
    initialized: bool = False

    def replace(self, **kw) -> "WorkingMemory":
        return replace(self, **kw)
