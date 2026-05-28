"""Statechart elements as immutable data (frozen dataclasses).

Mirrors the Fulcrologic philosophy: a chart and all of its executable content is
*data*, not opaque code. Guards/actions are stored as plain Python callables
``(env, data) -> ...`` rather than eval'd strings.

State-like nodes (``state``, ``parallel``, ``final``, ``history``) share one
:class:`StateNode` carrying a ``kind`` discriminator, matching how SCXML treats
``<state>``/``<parallel>``/``<final>`` uniformly in its algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence, Tuple, Union

# An "expression" is a callable evaluated with (env, data). Guards return truthy;
# action expressions return an iterable of ops (see data_model) or None.
Expr = Callable[[Any, dict], Any]

# ---------------------------------------------------------------------------
# Executable content
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Script:
    """Run ``expr(env, data)``; its return value (iterable of ops or None) is
    applied to the data model."""

    expr: Expr


@dataclass(frozen=True)
class Assign:
    """``<assign location=... expr=.../>`` — set ``location`` in the data model.

    ``expr`` may be a callable ``(env, data)`` or a literal value."""

    location: str
    expr: Union[Expr, Any]


@dataclass(frozen=True)
class Raise:
    """``<raise event=.../>`` — enqueue an *internal* event."""

    event: str
    data: Optional[Union[Expr, dict]] = None


@dataclass(frozen=True)
class Log:
    """``<log label=... expr=.../>``."""

    expr: Union[Expr, Any]
    label: Optional[str] = None


@dataclass(frozen=True)
class Send:
    """``<send>`` — deliver an event, optionally after ``delay`` ms. ``target``
    of ``None`` / ``"#_internal"`` routes to this session.

    Delayed delivery is realised by the event queue (see event_queue).  The
    ``*_expr`` fields and ``namelist``/``params``/``content`` support the SCXML
    attribute forms where values are computed at send time."""

    event: Optional[str] = None
    target: Optional[str] = None
    delay: int = 0
    id: Optional[str] = None
    data: Optional[Union[Expr, dict]] = None
    type: Optional[str] = None
    event_expr: Optional[Union[Expr, str]] = None
    delay_expr: Optional[Union[Expr, str]] = None
    target_expr: Optional[Union[Expr, str]] = None
    type_expr: Optional[Union[Expr, str]] = None
    id_location: Optional[str] = None
    namelist: Tuple[str, ...] = ()
    params: Tuple[Tuple[str, Union[Expr, str]], ...] = ()  # (name, value-expr)
    content: Optional[Union[Expr, str]] = None


@dataclass(frozen=True)
class Cancel:
    """``<cancel sendid=.../>`` — cancel a pending delayed send."""

    sendid: Optional[str] = None
    sendid_expr: Optional[Union[Expr, str]] = None


@dataclass(frozen=True)
class If:
    """``<if>/<elseif>/<else>`` — conditional executable content.

    ``branches`` is an ordered list of ``(cond, content)``; the first branch whose
    ``cond`` is truthy (or ``None``, the ``<else>``) runs."""

    branches: Tuple[Tuple[Optional[Union[Expr, str]], Tuple], ...] = ()


@dataclass(frozen=True)
class Foreach:
    """``<foreach>`` — iterate ``array`` binding each element to ``item`` (and the
    index to ``index``), running ``content`` each time."""

    array: Union[Expr, str]
    item: str
    index: Optional[str] = None
    content: Tuple = ()


# A unit of executable content is one of the above, or a bare callable.
# (If/Foreach are defined below and are also valid content.)
Content = Union[Script, Assign, Raise, Log, Send, Cancel, Expr]


@dataclass(frozen=True)
class OnEntry:
    content: Tuple[Content, ...] = ()


@dataclass(frozen=True)
class OnExit:
    content: Tuple[Content, ...] = ()


@dataclass(frozen=True)
class DataModel:
    """Initial data bindings for a state (applied when the state is entered,
    i.e. late binding). ``data`` maps location -> literal or ``(env, data)`` expr."""

    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    target: Tuple[str, ...] = ()
    event: Optional[str] = None  # None => eventless ("automatic") transition
    cond: Optional[Expr] = None
    type: str = "external"  # "external" | "internal"
    content: Tuple[Content, ...] = ()
    id: Optional[str] = None

    @property
    def is_eventless(self) -> bool:
        return self.event is None

    @property
    def is_targetless(self) -> bool:
        return len(self.target) == 0


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

# kinds
SCXML = "scxml"
STATE = "state"
PARALLEL = "parallel"
FINAL = "final"
HISTORY = "history"


@dataclass(frozen=True)
class StateNode:
    id: str
    kind: str = STATE
    children: Tuple["StateNode", ...] = ()
    transitions: Tuple[Transition, ...] = ()
    on_entry: Tuple[OnEntry, ...] = ()
    on_exit: Tuple[OnExit, ...] = ()
    datamodel: Optional[DataModel] = None
    # initial-state target(s) for a compound state; empty => default to first child
    initial: Tuple[str, ...] = ()
    # optional executable content attached to the implicit <initial> transition
    initial_content: Tuple[Content, ...] = ()
    # history nodes only:
    history_type: Optional[str] = None  # "shallow" | "deep"
    history_default: Optional[Transition] = None
    # done-data for <final> (evaluated to populate done.state.* event data)
    donedata: Optional[Expr] = None

    @property
    def child_states(self) -> Tuple["StateNode", ...]:
        """SCXML getChildStates: <state>/<parallel>/<final> children (not history)."""
        return tuple(c for c in self.children if c.kind != HISTORY)
