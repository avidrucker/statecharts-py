"""statecharts — a faithful Python port of fulcrologic/statecharts.

W3C SCXML semantics (compound/parallel/atomic/final states, history, eventless
transitions, guards, executable content) expressed as plain data, with swappable
DataModel / ExecutionModel / EventQueue seams.
"""
from __future__ import annotations

from . import ops
from .algorithm import initialize, process_event
from .chart import (
    Chart,
    data_model,
    final,
    history,
    initial,
    make_chart,
    on_entry,
    on_exit,
    parallel,
    state,
    statechart,
    transition,
)
from .convenience import choice, handle, on, send_after
from .elements import (
    Assign,
    Cancel,
    DataModel,
    Log,
    OnEntry,
    OnExit,
    Raise,
    Script,
    Send,
    StateNode,
    Transition,
)
from .environment import Environment, make_env
from .event_queue import Clock, ManualClock, MemoryEventQueue
from .events import Event, coerce_event, event_matches
from . import store
from .aio import AsyncSession
from .durable import ChartRegistry, DurableRuntime, SqliteEventQueue, SqliteStore
from .simple import Session
from .store import NormalizedDataModel, initial_store, resolve_actors, resolve_aliases
from .viz import to_dot, to_mermaid
from .working_memory import WorkingMemory

__version__ = "0.1.0"

__all__ = [
    "ops",
    "initialize",
    "process_event",
    "Chart",
    "make_chart",
    "statechart",
    "state",
    "parallel",
    "final",
    "history",
    "transition",
    "initial",
    "on_entry",
    "on_exit",
    "data_model",
    "on",
    "handle",
    "choice",
    "send_after",
    "Script",
    "Assign",
    "Raise",
    "Log",
    "Send",
    "Cancel",
    "OnEntry",
    "OnExit",
    "DataModel",
    "StateNode",
    "Transition",
    "Environment",
    "make_env",
    "Event",
    "coerce_event",
    "event_matches",
    "Clock",
    "ManualClock",
    "MemoryEventQueue",
    "Session",
    "AsyncSession",
    "WorkingMemory",
    "to_mermaid",
    "to_dot",
    "store",
    "NormalizedDataModel",
    "initial_store",
    "resolve_actors",
    "resolve_aliases",
    "SqliteStore",
    "SqliteEventQueue",
    "ChartRegistry",
    "DurableRuntime",
]
