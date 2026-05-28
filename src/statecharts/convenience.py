"""Ergonomic shorthands mirroring Fulcrologic's ``convenience`` namespace."""
from __future__ import annotations

from typing import Optional

from .chart import on_entry, on_exit, state, transition
from .elements import Cancel, Script, Send, StateNode, Transition


def on(event: str, target) -> Transition:
    """A simple event-triggered transition: ``on("submit", "processing")``."""
    return transition({"event": event, "target": target})


def handle(event: str, fn) -> Transition:
    """A targetless transition that just runs ``fn(env, data) -> ops``."""
    return transition({"event": event}, Script(fn))


def choice(opts: Optional[dict] = None, *clauses) -> StateNode:
    """A decision state: eventless guarded transitions tried in order.

    ``clauses`` is a flat sequence ``cond1, target1, cond2, target2, ..., else_target``
    where a trailing lone target (or ``None`` cond) is the unconditional fallback.
    """
    opts = opts or {}
    transitions = []
    items = list(clauses)
    i = 0
    while i < len(items):
        if i == len(items) - 1:
            # trailing lone target => unconditional else
            transitions.append(transition({"target": items[i]}))
            i += 1
        else:
            cond, tgt = items[i], items[i + 1]
            spec = {"target": tgt}
            if cond is not None:
                spec["cond"] = cond
            transitions.append(transition(spec))
            i += 2
    return state(opts, *transitions)


def send_after(opts: dict):
    """Delayed self-event scoped to a state's lifetime.

    Splat into a state's children: ``state({...}, *send_after({...}))``.  Sends the
    event on entry (after ``delay`` ms) and cancels it on exit.
    """
    sid = opts["id"]
    event = opts["event"]
    delay = opts.get("delay", 0)
    return (
        on_entry(Send(event, delay=delay, id=sid)),
        on_exit(Cancel(sid)),
    )
