"""The SCXML processing algorithm — a faithful port of the W3C appendix pseudocode.

Adapted to a *stateless per-event* interface (like Fulcrologic): :func:`initialize`
produces the starting working memory, and :func:`process_event` maps
``(env, working_memory, event) -> working_memory``.  Within a single call a mutable
:class:`_Run` cursor mirrors the spec's imperative style; a fresh immutable
:class:`WorkingMemory` is returned at the boundary.

Reference: https://www.w3.org/TR/scxml/#AlgorithmforSCXMLInterpretation
"""
from __future__ import annotations

from collections import deque
from typing import Iterable, List, NamedTuple, Optional, Set

from .chart import Chart
from .elements import (
    Assign,
    Cancel,
    Log,
    Raise,
    Script,
    Send,
    StateNode,
    Transition,
)
from .environment import Environment
from .events import Event, coerce_event, event_matches
from .ops import AssignOp, DeleteOp
from .working_memory import WorkingMemory


class ET(NamedTuple):
    """An enabled transition paired with its source state id."""

    source: str
    transition: Transition


class _Run:
    """Mutable per-call cursor over a session's state."""

    def __init__(self, env: Environment, wm: Optional[WorkingMemory] = None):
        self.env = env
        self.chart: Chart = env.chart
        if wm is None:
            self.configuration: Set[str] = set()
            self.datamodel: dict = {}
            self.history_value = {}
            self.running = False
            self.dm_initialized: Set[str] = set()
        else:
            self.configuration = set(wm.configuration)
            self.datamodel = dict(wm.datamodel)
            self.history_value = {k: set(v) for k, v in wm.history_value.items()}
            self.running = wm.running
            self.dm_initialized = set(wm.configuration)
        self.internal_queue: deque = deque()
        self._event: Optional[Event] = None

    def data_view(self) -> dict:
        view = dict(self.datamodel)
        view["_event"] = self._event.as_data() if self._event else None
        return view

    def to_wm(self) -> WorkingMemory:
        return WorkingMemory(
            configuration=frozenset(self.configuration),
            datamodel=dict(self.datamodel),
            history_value={k: frozenset(v) for k, v in self.history_value.items()},
            running=self.running,
            initialized=True,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def initialize(env: Environment) -> WorkingMemory:
    """Build the starting working memory: enter the document's initial states."""
    run = _Run(env)
    run.running = True
    root = run.chart.root
    if root.datamodel:
        _apply_datamodel(run, root)
        run.dm_initialized.add(root.id)
    initial_t = Transition(target=root.initial, content=root.initial_content)
    _enter_states(run, [ET(root.id, initial_t)])
    _settle(run)
    return run.to_wm()


def process_event(env: Environment, wm: WorkingMemory, event) -> WorkingMemory:
    """Process one external event, run to completion, return new working memory."""
    if not wm.initialized:
        wm = initialize(env)
    run = _Run(env, wm)
    if not run.running:
        return wm
    run._event = coerce_event(event)
    enabled = _select_transitions(run, run._event)
    if enabled:
        _microstep(run, enabled)
    _settle(run)
    return run.to_wm()


# ---------------------------------------------------------------------------
# Macrostep / run-to-completion
# ---------------------------------------------------------------------------


def _settle(run: _Run) -> None:
    macrostep_done = False
    while run.running and not macrostep_done:
        enabled = _select_eventless_transitions(run)
        if not enabled:
            if not run.internal_queue:
                macrostep_done = True
            else:
                ev = run.internal_queue.popleft()
                run._event = ev
                enabled = _select_transitions(run, ev)
        if enabled:
            _microstep(run, enabled)


def _microstep(run: _Run, transitions: List[ET]) -> None:
    _exit_states(run, transitions)
    for et in transitions:
        _execute_content(run, et.transition.content)
    _enter_states(run, transitions)


# ---------------------------------------------------------------------------
# Transition selection
# ---------------------------------------------------------------------------


def _cond_match(run: _Run, t: Transition) -> bool:
    if t.cond is None:
        return True
    return bool(run.env.execution_model.run(run.env, run.data_view(), t.cond))


def _select_transitions(run: _Run, event: Event) -> List[ET]:
    enabled: List[ET] = []
    c = run.chart
    atomic = c.in_document_order([s for s in run.configuration if c.is_atomic(s)])
    for astate in atomic:
        found = False
        for sid in [astate] + c.proper_ancestors(astate, None):
            if found:
                break
            for t in c.node(sid).transitions:
                if t.event is not None and event_matches(t.event, event.name) and _cond_match(run, t):
                    enabled.append(ET(sid, t))
                    found = True
                    break
    return _remove_conflicting_transitions(run, enabled)


def _select_eventless_transitions(run: _Run) -> List[ET]:
    enabled: List[ET] = []
    c = run.chart
    atomic = c.in_document_order([s for s in run.configuration if c.is_atomic(s)])
    for astate in atomic:
        found = False
        for sid in [astate] + c.proper_ancestors(astate, None):
            if found:
                break
            for t in c.node(sid).transitions:
                if t.event is None and _cond_match(run, t):
                    enabled.append(ET(sid, t))
                    found = True
                    break
    return _remove_conflicting_transitions(run, enabled)


def _remove_conflicting_transitions(run: _Run, enabled: List[ET]) -> List[ET]:
    filtered: List[ET] = []
    c = run.chart
    for t1 in enabled:
        preempted = False
        to_remove: List[ET] = []
        ex1 = _compute_exit_set(run, [t1])
        for t2 in filtered:
            ex2 = _compute_exit_set(run, [t2])
            if ex1 & ex2:
                if c.is_descendant(t1.source, t2.source):
                    to_remove.append(t2)
                else:
                    preempted = True
                    break
        if not preempted:
            filtered = [t for t in filtered if t not in to_remove]
            filtered.append(t1)
    return filtered


# ---------------------------------------------------------------------------
# Transition domain / LCCA / effective targets
# ---------------------------------------------------------------------------


def _effective_target_states(run: _Run, t: Transition) -> List[str]:
    c = run.chart
    out: List[str] = []
    seen: Set[str] = set()

    def add(s: str) -> None:
        if s not in seen:
            seen.add(s)
            out.append(s)

    for tid in t.target:
        if c.is_history(tid):
            hv = run.history_value.get(tid)
            if hv:
                for s in hv:
                    add(s)
            else:
                default = c.node(tid).history_default
                if default:
                    for s in _effective_target_states(run, default):
                        add(s)
        else:
            add(tid)
    return out


def _find_lcca(run: _Run, state_list: List[str]) -> Optional[str]:
    c = run.chart
    head = state_list[0]
    rest = state_list[1:]
    for anc in c.proper_ancestors(head, None):
        if (c.is_compound(anc) or c.is_scxml(anc)) and all(c.is_descendant(s, anc) for s in rest):
            return anc
    # Fallback: the scxml root is the common ancestor of everything.
    return c.root.id


def _transition_domain(run: _Run, et: ET) -> Optional[str]:
    c = run.chart
    t = et.transition
    tstates = _effective_target_states(run, t)
    if not tstates:
        return None
    if t.type == "internal" and c.is_compound(et.source) and all(
        c.is_descendant(s, et.source) for s in tstates
    ):
        return et.source
    return _find_lcca(run, [et.source] + tstates)


# ---------------------------------------------------------------------------
# Exiting states
# ---------------------------------------------------------------------------


def _compute_exit_set(run: _Run, transitions: Iterable[ET]) -> Set[str]:
    c = run.chart
    to_exit: Set[str] = set()
    for et in transitions:
        if et.transition.target:
            domain = _transition_domain(run, et)
            if domain is None:
                continue
            for s in run.configuration:
                if c.is_descendant(s, domain):
                    to_exit.add(s)
    return to_exit


def _exit_states(run: _Run, transitions: List[ET]) -> None:
    c = run.chart
    to_exit = _compute_exit_set(run, transitions)
    # Record history before removing states from the configuration.
    for sid in to_exit:
        node = c.node(sid)
        for child in node.children:
            if c.is_history(child.id):
                if child.history_type == "deep":
                    run.history_value[child.id] = {
                        s for s in run.configuration if c.is_atomic(s) and c.is_descendant(s, sid)
                    }
                else:
                    run.history_value[child.id] = {
                        s for s in run.configuration if c.parent_id(s) == sid
                    }
    for sid in c.in_exit_order(to_exit):
        node = c.node(sid)
        for blk in node.on_exit:
            _execute_content(run, blk.content)
        run.configuration.discard(sid)


# ---------------------------------------------------------------------------
# Entering states
# ---------------------------------------------------------------------------


def _compute_entry_set(run: _Run, transitions: List[ET]):
    to_enter: Set[str] = set()
    default_entry: Set[str] = set()
    default_hist: dict = {}
    for et in transitions:
        for tid in et.transition.target:
            _add_descendant_states(run, tid, to_enter, default_entry, default_hist)
        ancestor = _transition_domain(run, et)
        for s in _effective_target_states(run, et.transition):
            _add_ancestor_states(run, s, ancestor, to_enter, default_entry, default_hist)
    return to_enter, default_entry, default_hist


def _add_descendant_states(run, sid, to_enter, default_entry, default_hist):
    c = run.chart
    if c.is_history(sid):
        parent = c.parent_id(sid)
        hv = run.history_value.get(sid)
        if hv is not None:
            for s in hv:
                _add_descendant_states(run, s, to_enter, default_entry, default_hist)
            for s in hv:
                _add_ancestor_states(run, s, parent, to_enter, default_entry, default_hist)
        else:
            default = c.node(sid).history_default
            default_hist[parent] = default.content if default else ()
            targets = default.target if default else ()
            for s in targets:
                _add_descendant_states(run, s, to_enter, default_entry, default_hist)
            for s in targets:
                _add_ancestor_states(run, s, parent, to_enter, default_entry, default_hist)
        return
    to_enter.add(sid)
    if c.is_compound(sid):
        default_entry.add(sid)
        for s in c.node(sid).initial:
            _add_descendant_states(run, s, to_enter, default_entry, default_hist)
        for s in c.node(sid).initial:
            _add_ancestor_states(run, s, sid, to_enter, default_entry, default_hist)
    elif c.is_parallel(sid):
        for child in c.child_state_ids(sid):
            if not any(c.is_descendant(s, child) for s in to_enter):
                _add_descendant_states(run, child, to_enter, default_entry, default_hist)


def _add_ancestor_states(run, sid, ancestor, to_enter, default_entry, default_hist):
    c = run.chart
    for anc in c.proper_ancestors(sid, ancestor):
        to_enter.add(anc)
        if c.is_parallel(anc):
            for child in c.child_state_ids(anc):
                if not any(c.is_descendant(s, child) for s in to_enter):
                    _add_descendant_states(run, child, to_enter, default_entry, default_hist)


def _is_in_final_state(run: _Run, sid: str) -> bool:
    c = run.chart
    if c.is_compound(sid):
        return any(
            ch in run.configuration and c.is_final(ch) for ch in c.child_state_ids(sid)
        )
    if c.is_parallel(sid):
        return all(_is_in_final_state(run, ch) for ch in c.child_state_ids(sid))
    return False


def _enter_states(run: _Run, transitions: List[ET]) -> None:
    c = run.chart
    to_enter, default_entry, default_hist = _compute_entry_set(run, transitions)
    for sid in c.in_document_order(to_enter):
        run.configuration.add(sid)
        node = c.node(sid)
        if node.datamodel and sid not in run.dm_initialized:
            _apply_datamodel(run, node)
            run.dm_initialized.add(sid)
        for blk in node.on_entry:
            _execute_content(run, blk.content)
        if sid in default_entry:
            _execute_content(run, node.initial_content)
        if sid in default_hist:
            _execute_content(run, default_hist[sid])
        if c.is_final(sid):
            parent = c.parent_id(sid)
            if parent is None or c.is_scxml(parent):
                run.running = False
            else:
                grandparent = c.parent_id(parent)
                donedata = _eval_donedata(run, node)
                run.internal_queue.append(
                    Event(f"done.state.{parent}", donedata, type="internal")
                )
                if grandparent is not None and c.is_parallel(grandparent):
                    if all(
                        _is_in_final_state(run, ch) for ch in c.child_state_ids(grandparent)
                    ):
                        run.internal_queue.append(
                            Event(f"done.state.{grandparent}", type="internal")
                        )


# ---------------------------------------------------------------------------
# Executable content
# ---------------------------------------------------------------------------


def _resolve(run: _Run, spec):
    if spec is None:
        return {}
    if callable(spec):
        return run.env.execution_model.run(run.env, run.data_view(), spec) or {}
    return spec


def _apply_ops(run: _Run, ops) -> None:
    if ops is None:
        return
    if isinstance(ops, (AssignOp, DeleteOp)):
        ops = [ops]
    run.datamodel = run.env.data_model.transact(run.datamodel, list(ops))


def _apply_datamodel(run: _Run, node: StateNode) -> None:
    if not node.datamodel:
        return
    for loc, expr in node.datamodel.data.items():
        val = run.env.execution_model.run(run.env, run.data_view(), expr) if callable(expr) else expr
        run.datamodel[loc] = val


def _eval_donedata(run: _Run, node: StateNode) -> dict:
    if node.donedata is None:
        return {}
    return run.env.execution_model.run(run.env, run.data_view(), node.donedata) or {}


def _execute_content(run: _Run, items) -> None:
    for item in items:
        _exec_one(run, item)


def _exec_one(run: _Run, item) -> None:
    env = run.env
    if isinstance(item, Script):
        _apply_ops(run, env.execution_model.run(env, run.data_view(), item.expr))
    elif isinstance(item, Assign):
        val = item.expr(env, run.data_view()) if callable(item.expr) else item.expr
        run.datamodel = env.data_model.transact(run.datamodel, [AssignOp(item.location, val)])
    elif isinstance(item, Raise):
        run.internal_queue.append(Event(item.event, _resolve(run, item.data), type="internal"))
    elif isinstance(item, Log):
        label = item.label or "log"
        val = item.expr(env, run.data_view()) if callable(item.expr) else item.expr
        print(f"[{label}] {val}")
    elif isinstance(item, Send):
        data = _resolve(run, item.data)
        if item.delay and item.delay > 0:
            env.event_queue.send(Event(item.event, data), delay=item.delay, sendid=item.id)
        elif item.target in (None, "#_internal"):
            run.internal_queue.append(Event(item.event, data, type="internal"))
        else:
            env.event_queue.send(Event(item.event, data), sendid=item.id)
    elif isinstance(item, Cancel):
        env.event_queue.cancel(item.sendid)
    elif callable(item):
        _apply_ops(run, item(env, run.data_view()))
    else:
        raise TypeError(f"Not executable content: {item!r}")
