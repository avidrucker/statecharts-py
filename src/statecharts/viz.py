"""Render a chart to Mermaid or Graphviz DOT for visualization.

Charts are data, so a renderer is just a tree walk.  ``to_mermaid`` emits a
``stateDiagram-v2``; ``to_dot`` emits a Graphviz digraph with nested clusters.
"""
from __future__ import annotations

import re
from typing import Optional

from .chart import Chart, make_chart
from .elements import FINAL, HISTORY, PARALLEL, SCXML, StateNode, Transition


def _as_chart(chart) -> Chart:
    if isinstance(chart, StateNode):
        return make_chart(chart)
    if isinstance(chart, Chart):
        return chart
    raise TypeError("expected a Chart or StateNode")


def _safe(sid: str) -> str:
    """A mermaid/dot-safe identifier derived from a state id."""
    s = re.sub(r"[^0-9A-Za-z_]", "_", sid)
    if s and s[0].isdigit():
        s = "s_" + s
    return s or "s"


def _cond_label(t: Transition) -> Optional[str]:
    if t.cond is None:
        return None
    if isinstance(t.cond, str):
        return t.cond
    name = getattr(t.cond, "__name__", None)
    return name if name and name != "<lambda>" else "guard"


def _edge_label(t: Transition) -> str:
    parts = []
    if t.event is not None:
        parts.append(t.event)
    cond = _cond_label(t)
    if cond is not None:
        parts.append(f"[{cond}]")
    if not parts:
        return "ε"  # eventless, unconditional
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Mermaid
# ---------------------------------------------------------------------------


def to_mermaid(chart) -> str:
    c = _as_chart(chart)
    lines = ["stateDiagram-v2"]
    _mermaid_state(c, c.root, lines, indent=1, top=True)
    _mermaid_transitions(c, c.root, lines)
    return "\n".join(lines)


def _mermaid_state(c: Chart, node: StateNode, lines, indent: int, top=False) -> None:
    pad = "    " * indent
    child_states = node.child_states
    if top:
        # initial pointer for the document
        if node.initial:
            lines.append(f"{pad}[*] --> {_safe(node.initial[0])}")
        for child in child_states:
            _mermaid_state(c, child, lines, indent)
        return

    if node.kind == FINAL:
        # finals render via [*] transitions from their parent; nothing to declare
        return
    if node.kind == HISTORY:
        lines.append(f'{pad}state "{node.id} (history)" as {_safe(node.id)}')
        return

    if not child_states:  # atomic
        lines.append(f"{pad}{_safe(node.id)}")
        return

    # composite or parallel
    lines.append(f"{pad}state {_safe(node.id)} {{")
    inner = "    " * (indent + 1)
    if node.kind == PARALLEL:
        for i, region in enumerate(child_states):
            if i:
                lines.append(f"{inner}--")
            _mermaid_region(c, region, lines, indent + 1)
    else:
        if node.initial:
            lines.append(f"{inner}[*] --> {_safe(node.initial[0])}")
        for child in node.children:  # node.children includes history pseudo-states
            _mermaid_state(c, child, lines, indent + 1)
    lines.append(f"{pad}}}")


def _mermaid_region(c: Chart, node: StateNode, lines, indent: int) -> None:
    inner = "    " * indent
    if node.child_states:
        if node.initial:
            lines.append(f"{inner}[*] --> {_safe(node.initial[0])}")
        for child in node.child_states:
            _mermaid_state(c, child, lines, indent)
    else:
        lines.append(f"{inner}{_safe(node.id)}")


def _mermaid_transitions(c: Chart, node: StateNode, lines) -> None:
    pad = "    "
    for sid, n in c.by_id.items():
        for t in n.transitions:
            for tgt in (t.target or ()):
                dst = _safe(tgt) if not c.is_final(tgt) else _safe(tgt)
                src = _safe(sid) if not c.is_scxml(sid) else "[*]"
                if c.is_final(tgt):
                    lines.append(f"{pad}{src} --> {dst} : {_edge_label(t)}")
                    lines.append(f"{pad}{dst} --> [*]")
                else:
                    lines.append(f"{pad}{src} --> {dst} : {_edge_label(t)}")


# ---------------------------------------------------------------------------
# Graphviz DOT
# ---------------------------------------------------------------------------


def to_dot(chart) -> str:
    c = _as_chart(chart)
    lines = ["digraph statechart {", '    rankdir=LR;', '    node [shape=rounded];']
    for child in c.root.child_states:
        _dot_node(c, child, lines, indent=1)
    for sid, n in c.by_id.items():
        if c.is_scxml(sid):
            continue
        for t in n.transitions:
            for tgt in (t.target or ()):
                label = _edge_label(t).replace('"', '\\"')
                lines.append(f'    {_safe(sid)} -> {_safe(tgt)} [label="{label}"];')
    lines.append("}")
    return "\n".join(lines)


def _dot_node(c: Chart, node: StateNode, lines, indent: int) -> None:
    pad = "    " * indent
    if node.child_states:
        lines.append(f"{pad}subgraph cluster_{_safe(node.id)} {{")
        lines.append(f'{pad}    label="{node.id}";')
        for child in node.child_states:
            _dot_node(c, child, lines, indent + 1)
        lines.append(f"{pad}}}")
    else:
        shape = "doublecircle" if node.kind == FINAL else "box"
        lines.append(f'{pad}{_safe(node.id)} [label="{node.id}", shape={shape}];')
