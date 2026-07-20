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
    # States whose <data> has already been applied. Persisted so a datamodel is applied at
    # most once (early binding: at document start; late binding: on first entry) and survives
    # across events — NOT reconstructed from the active configuration, which would re-apply
    # <data> to any state re-entered after being inactive (#38).
    dm_initialized: FrozenSet[str] = frozenset()
    # Active child invocations (invokeid -> runtime Invocation). NOTE: with live
    # invocations the working memory holds child sessions and is no longer a plain
    # serializable value; see invoke.py.
    invocations: dict = field(default_factory=dict)

    def replace(self, **kw) -> "WorkingMemory":
        return replace(self, **kw)
