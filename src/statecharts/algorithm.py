"""The SCXML processing algorithm — a faithful port of the W3C appendix pseudocode.

Adapted to a *stateless per-event* interface (like Fulcrologic): :func:`initialize`
produces the starting working memory, and :func:`process_event` maps
``(env, working_memory, event) -> working_memory``.  Within a single call a mutable
:class:`_Run` cursor mirrors the spec's imperative style; a fresh immutable
:class:`WorkingMemory` is returned at the boundary.

Reference: https://www.w3.org/TR/scxml/#AlgorithmforSCXMLInterpretation
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Iterable, List, NamedTuple, Optional, Set

from .chart import Chart
from .elements import (
    Assign,
    Cancel,
    Foreach,
    If,
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

_logger = logging.getLogger("statecharts")


class _ExecError(Exception):
    """Internal: an executable-content error that should surface as error.execution.

    May carry the ``sendid`` of a failed <send> so it appears in the error event."""

    def __init__(self, message: str, sendid: Optional[str] = None):
        super().__init__(message)
        self.sendid = sendid


def _raise_error(run, name: str = "error.execution") -> None:
    """Enqueue a platform error event (error.execution / error.communication)."""
    run.internal_queue.append(Event(name, type="platform"))


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
            self.invocations: dict = {}
        else:
            self.configuration = set(wm.configuration)
            self.datamodel = dict(wm.datamodel)
            self.history_value = {k: set(v) for k, v in wm.history_value.items()}
            self.running = wm.running
            self.dm_initialized = set(wm.configuration)
            self.invocations = dict(wm.invocations)
        self.internal_queue: deque = deque()
        self._event: Optional[Event] = None

    def data_view(self) -> dict:
        # The DataModel projects the store into the read view seen by expressions
        # (LocalDataModel returns the store as-is; richer models resolve aliases etc.).
        view = dict(self.env.data_model.as_data(self.datamodel))
        view["_event"] = self._event.as_data() if self._event else None
        view["_configuration"] = frozenset(self.configuration)
        view["_name"] = self.env.extra.get("_name", "")
        sessionid = self.env.extra.get("_sessionid", "")
        view["_sessionid"] = sessionid
        view["_ioprocessors"] = {
            "http://www.w3.org/TR/scxml/#SCXMLEventProcessor": {"location": sessionid}
        }
        return view

    def to_wm(self) -> WorkingMemory:
        return WorkingMemory(
            configuration=frozenset(self.configuration),
            datamodel=dict(self.datamodel),
            history_value={k: frozenset(v) for k, v in self.history_value.items()},
            running=self.running,
            initialized=True,
            invocations=dict(self.invocations),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def initialize(env: Environment, initial: Optional[dict] = None) -> WorkingMemory:
    """Build the starting working memory: enter the document's initial states.

    ``initial`` seeds the data-model store before binding (e.g. a normalized store
    from :func:`store.initial_store`).  Uses SCXML *early binding* (the default):
    every ``<data>`` is created and assigned at init, in document order, regardless
    of whether its owning state is entered."""
    run = _Run(env)
    run.running = True
    if initial is not None:
        run.datamodel = dict(initial)
    root = run.chart.root
    late = env.extra.get("_binding", "early") == "late"
    for sid in run.chart.in_document_order(list(run.chart.by_id.keys())):
        node = run.chart.node(sid)
        if not node.datamodel:
            continue
        if late and sid != root.id:
            # Late binding: variables exist (undefined) at init; assigned on entry.
            for loc in node.datamodel.data:
                run.datamodel.setdefault(loc, None)
        else:
            _apply_datamodel(run, node)
            run.dm_initialized.add(sid)
    # Invoked children: overlay <param>/namelist seed data, but only onto variables
    # the child actually declares (undeclared inputs are dropped, per SCXML).
    for loc, val in env.extra.get("_seed_data", {}).items():
        if loc in run.datamodel:
            run.datamodel[loc] = val
    initial_t = Transition(target=root.initial, content=root.initial_content)
    _enter_states(run, [ET(root.id, initial_t)])
    _settle(run)
    if not run.running:
        _exit_interpreter(run)
    _update_invocations(run)
    return run.to_wm()


def _exit_interpreter(run: _Run) -> None:
    """SCXML exitInterpreter: on termination, run the onexit handlers of remaining
    active states in reverse document order.

    Deviation: we keep the final configuration intact (rather than emptying it) so
    callers can still inspect which final state was reached."""
    for sid in run.chart.in_exit_order(list(run.configuration)):
        node = run.chart.node(sid)
        for blk in node.on_exit:
            _run_block(run, blk.content)


def process_event(env: Environment, wm: WorkingMemory, event) -> WorkingMemory:
    """Process one external event, run to completion, return new working memory."""
    if not wm.initialized:
        wm = initialize(env)
    run = _Run(env, wm)
    if not run.running:
        return wm
    run._event = coerce_event(event)
    ev = run._event
    # An event from an invoked child runs that invocation's <finalize> first.
    if ev.invokeid and ev.invokeid in run.invocations:
        _run_block(run, run.invocations[ev.invokeid].finalize)
    # autoforward external events to children that requested it
    from .invocations import step_child

    for invc in list(run.invocations.values()):
        if invc.autoforward and not invc.done and ev.type != "platform":
            step_child(run, invc, ev)
    enabled = _select_transitions(run, ev)
    if enabled:
        _microstep(run, enabled)
    _settle(run)
    if not run.running:
        _exit_interpreter(run)
    _update_invocations(run)
    return run.to_wm()


def _update_invocations(run: _Run) -> None:
    """After a macrostep: cancel invocations whose state exited, and start invokes
    for newly-active states (SCXML defers invocation to after the macrostep)."""
    from .invocations import start_invocation

    c = run.chart
    for invokeid in list(run.invocations):
        if run.invocations[invokeid].state_id not in run.configuration:
            del run.invocations[invokeid]  # state exited => cancel the invocation
    invoked_states = {invc.state_id for invc in run.invocations.values()}
    for sid in c.in_document_order(list(run.configuration)):
        node = c.node(sid)
        if node.invokes and sid not in invoked_states:
            for inv in node.invokes:
                invc = start_invocation(run, sid, inv)
                if invc is not None:
                    run.invocations[invc.invokeid] = invc


# ---------------------------------------------------------------------------
# Macrostep / run-to-completion
# ---------------------------------------------------------------------------


#: Safety net against a chart whose eventless/internal events never settle.
MAX_SETTLE_ITERATIONS = 100_000


def _settle(run: _Run) -> None:
    macrostep_done = False
    iterations = 0
    while run.running and not macrostep_done:
        iterations += 1
        if iterations > MAX_SETTLE_ITERATIONS:
            raise RuntimeError("run-to-completion did not settle (possible infinite loop)")
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
        _run_block(run, et.transition.content)
    _enter_states(run, transitions)


# ---------------------------------------------------------------------------
# Transition selection
# ---------------------------------------------------------------------------


def _cond_match(run: _Run, t: Transition) -> bool:
    if t.cond is None:
        return True
    try:
        return bool(run.env.execution_model.run(run.env, run.data_view(), t.cond))
    except Exception:  # noqa: BLE001  (SCXML: guard error -> false + error.execution)
        _raise_error(run)
        return False


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
                    et = ET(sid, t)
                    if et not in enabled:  # enabledTransitions is a SET
                        enabled.append(et)
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
                    et = ET(sid, t)
                    if et not in enabled:  # enabledTransitions is a SET
                        enabled.append(et)
                    found = True
                    break
    return _remove_conflicting_transitions(run, enabled)


def _remove_conflicting_transitions(run: _Run, enabled: List[ET]) -> List[ET]:
    c = run.chart
    # Compute each transition's exit set ONCE, up front. The W3C
    # removeConflictingTransitions computes exit1/exit2 once per transition; the
    # previous code recomputed exit2 for every already-kept transition on every
    # outer iteration (O(n^2) exit-set computations, each an O(configuration) scan),
    # which made parallel-heavy charts blow up super-linearly (issue #8). Caching
    # collapses the inner check to a set intersection over precomputed sets.
    # Each transition's exit set is the active states strictly below its domain.
    # Computing it via a full-configuration scan per transition is O(n * configuration)
    # — the residual quadratic term after the exit-set caching in the first #8 pass.
    # Build the active-children index once and descend from each domain instead, so
    # the precompute drops to ~O(n) for disjoint parallel regions (small exit sets).
    active_children = _active_children_index(run)

    def _exit_of(et: ET) -> Set[str]:
        # Mirror _compute_exit_set([et]) exactly: no target, or a None domain (no
        # resolvable target states), means an empty exit set. (Guarding None matters:
        # active_children[None] is the root, so descending from None would wrongly
        # return the whole configuration.)
        if not et.transition.target:
            return set()
        domain = _transition_domain(run, et)
        if domain is None:
            return set()
        return _active_descendants(domain, active_children)

    exit_sets = [_exit_of(t) for t in enabled]

    # Two transitions conflict iff their exit sets intersect. Rather than compare
    # every kept transition pairwise (O(n^2), the bulk of the parallel-region cliff
    # in #8), index the kept transitions by the states they exit: `by_state[s]` is
    # the set of kept enabled-indices whose exit set contains `s`. A new transition
    # can only conflict with kept ones that share at least one exit state, so we
    # gather exactly those candidates from the index. For disjoint parallel regions
    # (whose exit sets don't overlap) there are none, so the whole check is O(1).
    #
    # `filtered`/`kept` hold enabled-indices, always ascending == document order
    # (we iterate i1 upward and only ever append or delete), so `sorted(candidates)`
    # replays the original doc-order scan — preserving the exact preempt/removal
    # semantics (descendant source is removed; first non-descendant preempts).
    kept: set = set()
    by_state: dict = {}
    for i1, t1 in enumerate(enabled):
        ex1 = exit_sets[i1]
        candidates: set = set()
        for s in ex1:
            bucket = by_state.get(s)
            if bucket:
                candidates |= bucket  # shares an exit state -> exit sets intersect
        preempted = False
        to_remove: List[int] = []
        for i2 in sorted(candidates):  # ascending == document order
            if c.is_descendant(t1.source, enabled[i2].source):
                to_remove.append(i2)
            else:
                preempted = True
                break
        if not preempted:
            for i2 in to_remove:
                kept.discard(i2)
                for s in exit_sets[i2]:
                    b = by_state.get(s)
                    if b:
                        b.discard(i2)
            kept.add(i1)
            for s in ex1:
                by_state.setdefault(s, set()).add(i1)
    return [enabled[i] for i in sorted(kept)]


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


def _active_children_index(run: _Run) -> Dict[str, List[str]]:
    """parent id -> its active child ids. The configuration is ancestor-closed, so
    this lets us find a domain's active descendants by descending instead of scanning
    the whole configuration for each transition (see :func:`_active_descendants`)."""
    index: Dict[str, List[str]] = {}
    parent = run.chart.parent
    for s in run.configuration:
        index.setdefault(parent[s], []).append(s)
    return index


def _active_descendants(domain: str, active_children: Dict[str, List[str]]) -> Set[str]:
    """The active states strictly below ``domain`` — i.e. exactly
    ``{s in configuration if is_descendant(s, domain)}``, computed by descent."""
    out: Set[str] = set()
    stack = list(active_children.get(domain, ()))
    while stack:
        sid = stack.pop()
        out.add(sid)
        stack.extend(active_children.get(sid, ()))
    return out


def _compute_exit_set(run: _Run, transitions: Iterable[ET]) -> Set[str]:
    # Union of each transition's exit set (active states below its domain). Descending
    # via the active-children index is O(exit-set size) per transition instead of an
    # O(configuration) scan, which matters when many transitions fire at once (one per
    # region in a wide parallel chart) — see #8/#14.
    active_children = _active_children_index(run)
    to_exit: Set[str] = set()
    for et in transitions:
        if et.transition.target:
            domain = _transition_domain(run, et)
            if domain is not None:
                to_exit |= _active_descendants(domain, active_children)
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
            _run_block(run, blk.content)
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
            _run_block(run, blk.content)
        if sid in default_entry:
            _run_block(run, node.initial_content)
        if sid in default_hist:
            _run_block(run, default_hist[sid])
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
        try:
            val = run.env.execution_model.run(run.env, run.data_view(), expr)
            run.datamodel = run.env.data_model.transact(run.datamodel, [AssignOp(loc, val)])
        except Exception:  # noqa: BLE001  (illegal <data expr> -> undefined + error.execution)
            run.datamodel = run.env.data_model.transact(run.datamodel, [AssignOp(loc, None)])
            _raise_error(run)


def _eval_donedata(run: _Run, node: StateNode):
    if node.donedata is None:
        return None  # no <donedata> => done event has undefined data
    try:
        return run.env.execution_model.run(run.env, run.data_view(), node.donedata) or None
    except Exception:  # noqa: BLE001
        _raise_error(run)
        return None


def _run_block(run: _Run, items) -> None:
    """Execute a content block; an error aborts the block and raises error.execution
    (SCXML executable-content error semantics)."""
    try:
        _execute_content(run, items)
    except _ExecError as exc:
        run.internal_queue.append(Event("error.execution", type="platform", sendid=exc.sendid))
    except Exception:  # noqa: BLE001
        _raise_error(run)


def _execute_content(run: _Run, items) -> None:
    for item in items:
        _exec_one(run, item)


_SCXML_PROCESSOR = "http://www.w3.org/TR/scxml/#SCXMLEventProcessor"


def _ev(run: _Run, expr):
    """Evaluate an expression (string/callable/literal) via the execution model."""
    return run.env.execution_model.run(run.env, run.data_view(), expr)


def _send_data(run: _Run, item: Send):
    """Build the event payload for a <send>/<raise> from data/content/namelist/params."""
    if item.content is not None:
        return _ev(run, item.content)
    if item.data is not None:
        return _resolve(run, item.data)
    payload = {}
    for name in item.namelist:
        if name not in run.datamodel:
            raise _ExecError(f"<send> namelist references undeclared location {name!r}")
        payload[name] = run.datamodel.get(name)
    for name, value_expr in item.params:
        payload[name] = _ev(run, value_expr)
    return payload if (item.namelist or item.params) else None


def _exec_one(run: _Run, item) -> None:
    env = run.env
    if isinstance(item, Script):
        _apply_ops(run, _ev(run, item.expr))
    elif isinstance(item, Assign):
        root = item.location.split(".")[0].split("[")[0]
        if root.startswith("_"):
            # System variables (_event, _sessionid, _name, _ioprocessors, ...) are read-only.
            raise _ExecError(f"cannot assign to system variable {item.location!r}")
        if root not in run.datamodel:
            # SCXML: assignment to an undeclared location is an error.
            raise _ExecError(f"assign to undeclared location {item.location!r}")
        run.datamodel = env.data_model.transact(
            run.datamodel, [AssignOp(item.location, _ev(run, item.expr))]
        )
    elif isinstance(item, Raise):
        run.internal_queue.append(Event(item.event, _resolve(run, item.data), type="internal"))
    elif isinstance(item, Log):
        label = item.label or "log"
        _logger.debug("[%s] %s", label, _ev(run, item.expr))
    elif isinstance(item, If):
        for cond, content in item.branches:
            if cond is None or bool(_ev(run, cond)):
                _execute_content(run, content)
                break
    elif isinstance(item, Foreach):
        array = _ev(run, item.array)
        if not isinstance(array, (list, tuple)):
            raise _ExecError(f"<foreach> array is not iterable: {array!r}")
        if not (isinstance(item.item, str) and item.item.isidentifier()):
            raise _ExecError(f"<foreach> item is not a valid location: {item.item!r}")
        for i, elem in enumerate(list(array)):  # copy: mutating source won't affect us
            run.datamodel[item.item] = elem
            if item.index is not None:
                run.datamodel[item.index] = i
            _execute_content(run, item.content)
    elif isinstance(item, Send):
        name = item.event if item.event is not None else _ev(run, item.event_expr)
        data = _send_data(run, item)
        sendid = item.id
        if sendid is None and item.id_location is not None:
            sendid = f"sendid-{id(item)}"
            run.datamodel[item.id_location] = sendid
        delay = item.delay
        if item.delay_expr is not None:
            delay = _parse_delay(_ev(run, item.delay_expr))
        send_type = item.type if item.type_expr is None else _ev(run, item.type_expr)
        if send_type not in (None, "", "scxml", _SCXML_PROCESSOR):
            raise _ExecError(f"unsupported <send> type {send_type!r}")
        target = item.target if item.target_expr is None else _ev(run, item.target_expr)
        sid = env.extra.get("_sessionid", "")
        if target == "#_internal":
            # Only "#_internal" routes to the internal queue. A <send> with no
            # target goes to this session's *external* queue (unlike <raise>).
            run.internal_queue.append(
                Event(name, data, type="internal", sendid=sendid, origintype=_SCXML_PROCESSOR)
            )
        elif target == "#_parent":
            # Child statechart -> parent session's external queue.
            parent_q = env.extra.get("_parent_queue")
            if parent_q is None:
                _raise_error(run, "error.communication")
            elif delay and delay > 0:
                # A delayed cross-session send lives on THIS (child) session's queue,
                # tagged for parent delivery. If the child terminates first it is never
                # delivered (SCXML: delayed sends die with the sending session).
                env.event_queue.send(
                    Event(name, data, origin="#_parent", origintype=_SCXML_PROCESSOR,
                          sendid=sendid, invokeid=env.extra.get("_invokeid")),
                    delay=delay, sendid=sendid,
                )
            else:
                parent_q.send(
                    Event(name, data, origintype=_SCXML_PROCESSOR, sendid=sendid,
                          invokeid=env.extra.get("_invokeid")),
                    sendid=sendid,
                )
        elif str(target).startswith("#_") and target[2:] in run.invocations:
            # Parent -> invoked child, addressed by "#_<invokeid>".
            from .invocations import step_child

            child_ev = Event(name, data, origintype=_SCXML_PROCESSOR, sendid=sendid)
            step_child(run, run.invocations[target[2:]], child_ev)
        elif target in (None, "", f"#_scxml_{sid}") or target == sid:
            ev = Event(name, data, origintype=_SCXML_PROCESSOR, sendid=sendid)
            env.event_queue.send(ev, delay=delay if delay and delay > 0 else 0, sendid=sendid)
        elif str(target).startswith("#_scxml_"):
            # Well-formed target naming an unknown session -> cannot dispatch.
            # error.communication is async and does NOT abort the block.
            _raise_error(run, "error.communication")
        else:
            # Malformed/illegal target -> error.execution, aborting the block.
            raise _ExecError(f"illegal <send> target {target!r}", sendid=sendid)
    elif isinstance(item, Cancel):
        sendid = item.sendid if item.sendid is not None else _ev(run, item.sendid_expr)
        env.event_queue.cancel(sendid)
    elif callable(item):
        _apply_ops(run, item(env, run.data_view()))
    else:
        raise TypeError(f"Not executable content: {item!r}")


def _parse_delay(spec) -> int:
    """Parse an SCXML delay (``"1s"``, ``"1.5s"``, ``".5s"``, ``"100ms"``) to ms."""
    if spec is None:
        return 0
    if isinstance(spec, (int, float)):
        return int(spec)
    s = str(spec).strip()
    if s.endswith("ms"):
        return int(float(s[:-2]))
    if s.endswith("s"):
        return int(float(s[:-1]) * 1000)
    return int(float(s))
