"""Synchronous, in-process ``<invoke>`` support.

A child statechart runs as its own session whose external queue is the *parent's*
event queue, so events the child sends to ``#_parent`` (and the automatic
``done.invoke.<id>`` when the child finishes) land directly on the parent. Started
after the macrostep that enters the invoking state; cancelled when it is exited.

This is the synchronous analogue of the deferred async/distributed invocation model;
it does not spawn threads. Child sessions live in the parent's working memory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from .chart import make_chart
from .elements import Invoke, StateNode
from .environment import Environment, make_env
from .events import Event

_SCXML_PROCESSOR = "http://www.w3.org/TR/scxml/#SCXMLEventProcessor"
_SCXML_TYPES = (
    None,
    "",
    "scxml",
    "http://www.w3.org/TR/scxml",
    "http://www.w3.org/TR/scxml/",
    _SCXML_PROCESSOR,
)


@dataclass
class Invocation:
    invokeid: str
    state_id: str
    env: Environment
    wm: Any  # child WorkingMemory
    autoforward: bool = False
    finalize: tuple = ()
    done: bool = False


def _counter():
    n = 0
    while True:
        n += 1
        yield n


_ids = _counter()


def _resolve_child_chart(run, inv: Invoke):
    """Resolve the invoke target to a child Chart (from inline content or src)."""
    from .scxml import load_string  # local import to avoid a cycle

    if inv.content_chart is not None:
        return make_chart(inv.content_chart)
    src = inv.src
    if inv.src_expr is not None:
        src = run.env.execution_model.run(run.env, run.data_view(), inv.src_expr)
    if not src:
        return None
    if src.startswith("file:"):
        src = src[len("file:"):]
    base = run.env.extra.get("_base_dir", "")
    path = src if os.path.isabs(src) else os.path.join(base, src)
    with open(path, "r", encoding="utf-8") as fh:
        root, _meta = load_string(fh.read())
    return make_chart(root)


def start_invocation(run, state_id: str, inv: Invoke) -> Optional[Invocation]:
    """Start one invoke. Returns the live Invocation, or None if it finished
    immediately (its done.invoke / #_parent events are already routed)."""
    from .algorithm import initialize  # local import to avoid a cycle

    invoke_type = inv.type
    if inv.type_expr is not None:
        invoke_type = run.env.execution_model.run(run.env, run.data_view(), inv.type_expr)
    if invoke_type not in _SCXML_TYPES:
        run.internal_queue.append(Event("error.execution", type="platform"))
        return None

    invokeid = inv.id
    if invokeid is None:
        invokeid = f"{state_id}.{next(_ids)}"
    if inv.id_location is not None:
        run.datamodel[inv.id_location] = invokeid

    try:
        child_chart = _resolve_child_chart(run, inv)
    except Exception:  # noqa: BLE001 (bad src / parse error)
        run.internal_queue.append(Event("error.communication", type="platform"))
        return None
    if child_chart is None:
        run.internal_queue.append(Event("error.execution", type="platform"))
        return None

    # Seed child data from <param>/namelist (assigned over the child's own init).
    seed = {}
    for name in inv.namelist:
        if name not in run.datamodel:
            # namelist must name a location in the *parent* data model.
            run.internal_queue.append(Event("error.execution", type="platform"))
            return None
        seed[name] = run.datamodel.get(name)
    for name, value_expr in inv.params:
        seed[name] = run.env.execution_model.run(run.env, run.data_view(), value_expr)

    parent_queue = run.env.event_queue
    clock = getattr(parent_queue, "clock", None)
    from .event_queue import MemoryEventQueue

    child_env = make_env(
        child_chart,
        data_model=run.env.data_model,
        execution_model=run.env.execution_model,
        event_queue=MemoryEventQueue(clock=clock) if clock else MemoryEventQueue(),
    )
    child_env.extra.update(run.env.extra)
    child_env.extra["_parent_queue"] = parent_queue
    child_env.extra["_invokeid"] = invokeid
    child_env.extra["_sessionid"] = invokeid
    child_env.extra["_seed_data"] = seed

    child_wm = initialize(child_env)
    invc = Invocation(invokeid, state_id, child_env, child_wm, inv.autoforward, inv.finalize)
    _drain_child(run, invc)
    # Always return the invocation (even if finished) so it is recorded against its
    # state and not restarted on the next macrostep; it is dropped when the state exits.
    return invc


def _drain_child(run, invc: Invocation) -> None:
    """Pump the child's own (delayed) queue, then emit done.invoke if it finished."""
    eq = invc.env.event_queue
    while invc.wm.running:
        due = eq.tick() if hasattr(eq, "tick") else []
        if not due:
            break
        from .algorithm import process_event

        for ev in due:
            invc.wm = process_event(invc.env, invc.wm, ev)
            if not invc.wm.running:
                break
    if not invc.wm.running and not invc.done:
        invc.done = True
        run.env.event_queue.send(
            Event(f"done.invoke.{invc.invokeid}", type="external", invokeid=invc.invokeid),
        )


def step_child(run, invc: Invocation, event: Event) -> None:
    """Deliver an event to a live child (autoforward / targeted send) and pump it."""
    from .algorithm import process_event

    if invc.done or not invc.wm.running:
        return
    invc.wm = process_event(invc.env, invc.wm, event)
    _drain_child(run, invc)
