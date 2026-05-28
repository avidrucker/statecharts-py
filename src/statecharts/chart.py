"""Chart construction DSL and the indexed :class:`Chart`.

The builder functions (:func:`statechart`, :func:`state`, :func:`parallel`,
:func:`final`, :func:`history`, :func:`transition`, ...) produce the frozen
:mod:`elements` tree.  :class:`Chart` walks that tree once to assign ids and
build the lookups the algorithm needs (id->node, parent map, document order).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Tuple, Union

from .elements import (
    DataModel,
    Invoke,
    OnEntry,
    OnExit,
    StateNode,
    Transition,
    SCXML,
    STATE,
    PARALLEL,
    FINAL,
    HISTORY,
)

# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _as_targets(target: Union[None, str, Tuple, List]) -> Tuple[str, ...]:
    if target is None:
        return ()
    if isinstance(target, (list, tuple)):
        return tuple(str(t) for t in target)
    return (str(target),)


def transition(opts: Optional[dict] = None, *content) -> Transition:
    opts = opts or {}
    return Transition(
        target=_as_targets(opts.get("target")),
        event=opts.get("event"),
        cond=opts.get("cond"),
        type=opts.get("type", "external"),
        content=tuple(content),
        id=opts.get("id"),
    )


def on_entry(*content) -> OnEntry:
    return OnEntry(tuple(content))


def on_exit(*content) -> OnExit:
    return OnExit(tuple(content))


def data_model(data: Optional[dict] = None, **kwargs) -> DataModel:
    merged = dict(data or {})
    merged.update(kwargs)
    return DataModel(merged)


class _Initial:
    """Marker for an explicit ``<initial>`` element: carries a transition."""

    __slots__ = ("transition",)

    def __init__(self, t: Transition):
        self.transition = t


def initial(target, *content) -> _Initial:
    return _Initial(transition({"target": target}, *content))


def _partition(children) -> dict:
    sub: List[StateNode] = []
    trans: List[Transition] = []
    entries: List[OnEntry] = []
    exits: List[OnExit] = []
    invokes: List[Invoke] = []
    dm: Optional[DataModel] = None
    init: Optional[_Initial] = None
    for c in children:
        if isinstance(c, StateNode):
            sub.append(c)
        elif isinstance(c, Transition):
            trans.append(c)
        elif isinstance(c, OnEntry):
            entries.append(c)
        elif isinstance(c, OnExit):
            exits.append(c)
        elif isinstance(c, Invoke):
            invokes.append(c)
        elif isinstance(c, DataModel):
            dm = c
        elif isinstance(c, _Initial):
            init = c
        else:
            raise TypeError(f"Unexpected child element: {c!r}")
    return {
        "sub": tuple(sub),
        "trans": tuple(trans),
        "entries": tuple(entries),
        "exits": tuple(exits),
        "invokes": tuple(invokes),
        "dm": dm,
        "init": init,
    }


def _initial_targets(opts: dict, part: dict) -> Tuple[Tuple[str, ...], Tuple]:
    """Resolve a compound state's initial targets + initial executable content."""
    if part["init"] is not None:
        t = part["init"].transition
        return t.target, t.content
    if opts.get("initial") is not None:
        return _as_targets(opts["initial"]), ()
    return (), ()


def state(opts: Optional[dict] = None, *children) -> StateNode:
    opts = opts or {}
    part = _partition(children)
    init_targets, init_content = _initial_targets(opts, part)
    return StateNode(
        id=opts.get("id"),
        kind=STATE,
        children=part["sub"],
        transitions=part["trans"],
        on_entry=part["entries"],
        on_exit=part["exits"],
        datamodel=part["dm"],
        initial=init_targets,
        initial_content=init_content,
        invokes=part["invokes"],
    )


def parallel(opts: Optional[dict] = None, *children) -> StateNode:
    opts = opts or {}
    part = _partition(children)
    return StateNode(
        id=opts.get("id"),
        kind=PARALLEL,
        children=part["sub"],
        transitions=part["trans"],
        on_entry=part["entries"],
        on_exit=part["exits"],
        datamodel=part["dm"],
        invokes=part["invokes"],
    )


def final(opts: Optional[dict] = None, *children) -> StateNode:
    opts = opts or {}
    part = _partition(children)
    return StateNode(
        id=opts.get("id"),
        kind=FINAL,
        on_entry=part["entries"],
        on_exit=part["exits"],
        donedata=opts.get("donedata"),
    )


def history(opts: Optional[dict] = None, *children) -> StateNode:
    opts = opts or {}
    part = _partition(children)
    default = part["trans"][0] if part["trans"] else None
    return StateNode(
        id=opts.get("id"),
        kind=HISTORY,
        history_type=opts.get("type", "shallow"),
        history_default=default,
    )


def statechart(opts: Optional[dict] = None, *children) -> StateNode:
    opts = opts or {}
    part = _partition(children)
    init_targets, init_content = _initial_targets(opts, part)
    return StateNode(
        id=opts.get("id", SCXML),
        kind=SCXML,
        children=part["sub"],
        transitions=part["trans"],
        datamodel=part["dm"],
        initial=init_targets,
        initial_content=init_content,
    )


# ---------------------------------------------------------------------------
# Indexed chart
# ---------------------------------------------------------------------------


class Chart:
    """An indexed, validated statechart ready for execution."""

    def __init__(self, root: StateNode):
        self.by_id: Dict[str, StateNode] = {}
        self.parent: Dict[str, Optional[str]] = {}
        self.doc_order: Dict[str, int] = {}
        self._order = 0
        self.root = self._index(root, None, ["s"])
        # resolve default initial targets now that ids exist (updates by_id)
        self._resolve_defaults(self.root)
        # re-point root/children at the resolved nodes in by_id
        self.root = self.by_id[self.root.id]

    # -- indexing -----------------------------------------------------------
    def _index(self, node: StateNode, parent_id: Optional[str], path: List[str]) -> StateNode:
        nid = node.id
        if nid is None:
            nid = ".".join(path)
        if nid in self.by_id:
            raise ValueError(f"Duplicate state id: {nid!r}")
        # Assign document order in PRE-ORDER (parent before its children), matching
        # SCXML's "document order". Recurse into children afterward.
        order = self._order
        self._order += 1
        new_children = []
        for i, child in enumerate(node.children):
            child_path = path + [child.id or f"{child.kind}{i}"]
            new_children.append(self._index(child, nid, child_path))
        node = dataclasses.replace(node, id=nid, children=tuple(new_children))
        self.by_id[nid] = node
        self.parent[nid] = parent_id
        self.doc_order[nid] = order
        return node

    def _resolve_defaults(self, node: StateNode) -> None:
        # Re-point by_id entries to the (possibly) updated nodes after default fill.
        if node.kind in (STATE, SCXML) and node.child_states and not node.initial:
            first = node.child_states[0].id
            updated = dataclasses.replace(node, initial=(first,))
            self.by_id[node.id] = updated
            node = updated
        for child in node.children:
            self._resolve_defaults(child)

    # -- predicates / queries ----------------------------------------------
    def node(self, sid: str) -> StateNode:
        return self.by_id[sid]

    def is_atomic(self, sid: str) -> bool:
        return len(self.node(sid).child_states) == 0 and self.node(sid).kind != HISTORY

    def is_compound(self, sid: str) -> bool:
        n = self.node(sid)
        return n.kind in (STATE, SCXML) and len(n.child_states) > 0

    def is_parallel(self, sid: str) -> bool:
        return self.node(sid).kind == PARALLEL

    def is_final(self, sid: str) -> bool:
        return self.node(sid).kind == FINAL

    def is_history(self, sid: str) -> bool:
        return self.node(sid).kind == HISTORY

    def is_scxml(self, sid: str) -> bool:
        return self.node(sid).kind == SCXML

    def child_state_ids(self, sid: str) -> List[str]:
        return [c.id for c in self.node(sid).child_states]

    def parent_id(self, sid: str) -> Optional[str]:
        return self.parent[sid]

    def proper_ancestors(self, sid: str, stop: Optional[str] = None) -> List[str]:
        """Ancestors of ``sid``, nearest first, excluding ``sid`` and ``stop``.

        If ``stop`` is None, includes all ancestors up to and including the root."""
        out: List[str] = []
        cur = self.parent[sid]
        while cur is not None and cur != stop:
            out.append(cur)
            cur = self.parent[cur]
        return out

    def is_descendant(self, sid: str, ancestor: str) -> bool:
        cur = self.parent[sid]
        while cur is not None:
            if cur == ancestor:
                return True
            cur = self.parent[cur]
        return False

    def in_document_order(self, ids) -> List[str]:
        return sorted(ids, key=lambda s: self.doc_order[s])

    def in_exit_order(self, ids) -> List[str]:
        return sorted(ids, key=lambda s: self.doc_order[s], reverse=True)


def make_chart(root: StateNode) -> Chart:
    return Chart(root)
