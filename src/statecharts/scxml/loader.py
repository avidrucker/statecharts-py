"""Parse standard SCXML XML into the in-memory element tree.

Supports the constructs this engine implements.  Unsupported-but-defined SCXML
features (``<invoke>``, ``<script>``) raise :class:`UnsupportedConstruct` so the
conformance runner can *skip* (not fail) those tests, keeping the pass rate honest.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

from ..chart import (
    data_model,
    final,
    history,
    initial,
    on_entry,
    on_exit,
    parallel,
    state,
    statechart,
    transition,
)
from ..elements import Assign, Cancel, Foreach, If, Invoke, Log, Raise, Send, StateNode


class UnsupportedConstruct(Exception):
    """Raised when a chart uses an SCXML feature this engine doesn't implement."""


class InsecureDocument(Exception):
    """Raised when a document is refused on security grounds, before it is parsed.

    Distinct from :class:`UnsupportedConstruct`, which the conformance runner treats as a
    *skip*.  An insecure document is a hard failure, never a skip.
    """


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _targets(value: Optional[str]):
    if not value:
        return None
    return value.split()


# ---------------------------------------------------------------------------
# Executable content
# ---------------------------------------------------------------------------


def _parse_content(el) -> list:
    """Parse a sequence of executable-content children."""
    out = []
    for child in el:
        out.append(_parse_exec(child))
    return out


def _parse_exec(el):
    tag = _local(el.tag)
    if tag == "assign":
        return Assign(location=el.get("location"), expr=el.get("expr"))
    if tag == "raise":
        return Raise(event=el.get("event"))
    if tag == "log":
        return Log(expr=el.get("expr"), label=el.get("label"))
    if tag == "send":
        return _parse_send(el)
    if tag == "cancel":
        return Cancel(sendid=el.get("sendid"), sendid_expr=el.get("sendidexpr"))
    if tag == "if":
        return _parse_if(el)
    if tag == "foreach":
        return Foreach(
            array=el.get("array"),
            item=el.get("item"),
            index=el.get("index"),
            content=tuple(_parse_content(el)),
        )
    if tag == "script":
        raise UnsupportedConstruct("<script> (ecmascript body) not supported")
    raise UnsupportedConstruct(f"<{tag}> executable content not supported")


def _literal_value(text: str):
    """A <content> text body is a literal value (number if numeric, else string).

    Wrapped as a callable so the execution model returns it verbatim rather than
    evaluating it as a JS expression."""
    t = text.strip()
    try:
        v = int(t)
    except ValueError:
        try:
            v = float(t)
        except ValueError:
            v = t
    return lambda env, data: v


def _parse_content_value(child):
    """Resolve a <content>/<donedata> child to an expression or literal callable."""
    if child.get("expr") is not None:
        return child.get("expr")
    if child.text and child.text.strip():
        return _literal_value(child.text)
    return None


def _parse_send(el) -> Send:
    namelist = tuple((el.get("namelist") or "").split())
    params: List[Tuple[str, str]] = []
    content = None
    for child in el:
        ctag = _local(child.tag)
        if ctag == "param":
            params.append((child.get("name"), child.get("expr") or child.get("location")))
        elif ctag == "content":
            if child.get("src") is not None:
                raise UnsupportedConstruct("<content src=...> not supported")
            content = _parse_content_value(child)
    delay = el.get("delay")
    from ..algorithm import _parse_delay

    return Send(
        event=el.get("event"),
        target=el.get("target"),
        delay=_parse_delay(delay) if delay else 0,
        id=el.get("id"),
        type=el.get("type"),
        event_expr=el.get("eventexpr"),
        delay_expr=el.get("delayexpr"),
        target_expr=el.get("targetexpr"),
        type_expr=el.get("typeexpr"),
        id_location=el.get("idlocation"),
        namelist=namelist,
        params=tuple(params),
        content=content,
    )


def _parse_if(el) -> If:
    branches: List[Tuple[Optional[str], tuple]] = []
    current_cond = el.get("cond")
    current_content: list = []
    for child in el:
        ctag = _local(child.tag)
        if ctag == "elseif":
            branches.append((current_cond, tuple(current_content)))
            current_cond = child.get("cond")
            current_content = []
        elif ctag == "else":
            branches.append((current_cond, tuple(current_content)))
            current_cond = None
            current_content = []
        else:
            current_content.append(_parse_exec(child))
    branches.append((current_cond, tuple(current_content)))
    return If(branches=tuple(branches))


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def _src_loader(src: str):
    """A (env, data) callable that loads <data src=...> and evaluates its contents."""
    def load(env, data):
        path = src[len("file:"):] if src.startswith("file:") else src
        if not os.path.isabs(path):
            path = os.path.join(env.extra.get("_base_dir", ""), path)
        with open(path, "r", encoding="utf-8") as fh:
            return env.execution_model.run(env, data, fh.read().strip())
    return load


def _parse_datamodel(el):
    data = {}
    for d in el:
        if _local(d.tag) != "data":
            continue
        if d.get("src") is not None:
            data[d.get("id")] = _src_loader(d.get("src"))
            continue
        expr = d.get("expr")
        if expr is None and d.text and d.text.strip():
            expr = d.text.strip()
        data[d.get("id")] = expr
    return data_model(data)


def _make_donedata(el):
    params = []
    content = None
    for child in el:
        ctag = _local(child.tag)
        if ctag == "param":
            params.append((child.get("name"), child.get("expr") or child.get("location")))
        elif ctag == "content":
            content = _parse_content_value(child)
    if content is not None:
        return lambda env, data: env.execution_model.run(env, data, content)
    return lambda env, data: {n: env.execution_model.run(env, data, e) for n, e in params}


def _parse_transition(el):
    return transition(
        {
            "event": el.get("event"),
            "cond": el.get("cond"),
            "target": _targets(el.get("target")),
            "type": el.get("type", "external"),
            "id": el.get("id"),
        },
        *_parse_content(el),
    )


def _parse_state_children(el):
    """Common child parsing for <state>/<scxml>; returns builder children list."""
    children = []
    for child in el:
        tag = _local(child.tag)
        if tag in ("state", "parallel", "final", "history"):
            children.append(_parse_state(child))
        elif tag == "transition":
            children.append(_parse_transition(child))
        elif tag == "onentry":
            children.append(on_entry(*_parse_content(child)))
        elif tag == "onexit":
            children.append(on_exit(*_parse_content(child)))
        elif tag == "datamodel":
            children.append(_parse_datamodel(child))
        elif tag == "script":
            raise UnsupportedConstruct("<script> (ecmascript body) not supported")
        elif tag == "initial":
            t = next(c for c in child if _local(c.tag) == "transition")
            children.append(initial(_targets(t.get("target")), *_parse_content(t)))
        elif tag == "invoke":
            children.append(_parse_invoke(child))
        # <donedata> handled by caller for <final>
    return children


def _parse_invoke(el) -> Invoke:
    params: List[Tuple[str, str]] = []
    finalize: list = []
    content_chart = None
    for child in el:
        ctag = _local(child.tag)
        if ctag == "param":
            params.append((child.get("name"), child.get("expr") or child.get("location")))
        elif ctag == "finalize":
            finalize = _parse_content(child)
        elif ctag == "content":
            inner = next((c for c in child if _local(c.tag) == "scxml"), None)
            if inner is not None:
                content_chart, _meta = _build_root(inner)
    return Invoke(
        type=el.get("type"),
        type_expr=el.get("typeexpr"),
        src=el.get("src"),
        src_expr=el.get("srcexpr"),
        id=el.get("id"),
        id_location=el.get("idlocation"),
        autoforward=(el.get("autoforward", "false") == "true"),
        namelist=tuple((el.get("namelist") or "").split()),
        params=tuple(params),
        content_chart=content_chart,
        finalize=tuple(finalize),
    )


def _parse_state(el) -> StateNode:
    tag = _local(el.tag)
    if tag == "parallel":
        return parallel({"id": el.get("id")}, *_parse_state_children(el))
    if tag == "history":
        t = next((c for c in el if _local(c.tag) == "transition"), None)
        kids = [_parse_transition(t)] if t is not None else []
        return history({"id": el.get("id"), "type": el.get("type", "shallow")}, *kids)
    if tag == "final":
        donedata = next((c for c in el if _local(c.tag) == "donedata"), None)
        opts = {"id": el.get("id")}
        if donedata is not None:
            opts["donedata"] = _make_donedata(donedata)
        kids = [c for c in _parse_state_children(el)]  # onentry/onexit only
        return final(opts, *kids)
    # plain <state>
    opts = {"id": el.get("id")}
    init = el.get("initial")
    if init:
        opts["initial"] = _targets(init)
    return state(opts, *_parse_state_children(el))


def _build_root(root_el):
    if _local(root_el.tag) != "scxml":
        raise ValueError("Root element is not <scxml>")
    opts = {"id": "scxml"}
    init = root_el.get("initial")
    if init:
        opts["initial"] = _targets(init)
    root = statechart(opts, *_parse_state_children(root_el))
    meta = {"name": root_el.get("name", ""), "binding": root_el.get("binding", "early")}
    return root, meta


def _reject_doctype(xml_text: str) -> None:
    """Refuse a document carrying a DTD, *before* it is handed to the parser.

    ``ET.fromstring`` expands internal entities, so a nested-entity DTD ("billion laughs")
    amplifies its input exponentially — unbounded memory/CPU from untrusted SCXML (#43).
    ``ET.XMLParser`` exposes no expat entity hook on modern CPython, so the guard is a scan
    of the *prolog* instead: a DOCTYPE is only well-formed there, and running before the
    parse means no expansion can occur at all.  SCXML has no use for a DTD, so any DOCTYPE
    is refused rather than the expansion being bounded.
    """
    i = 0
    n = len(xml_text)
    if xml_text.startswith("﻿"):  # BOM, else it would end the scan on the first char
        i = 1
    while i < n:
        while i < n and xml_text[i].isspace():
            i += 1
        if i >= n or xml_text[i] != "<":
            return  # not markup; malformed input is the parser's error to report, not ours
        if xml_text.startswith("<!DOCTYPE", i):
            raise InsecureDocument(
                "refusing a document with a DTD: internal entities amplify exponentially "
                "(billion-laughs). SCXML does not require a DOCTYPE."
            )
        if xml_text.startswith("<?", i):  # XML declaration or processing instruction
            end = xml_text.find("?>", i + 2)
        elif xml_text.startswith("<!--", i):
            end = xml_text.find("-->", i + 4)
        else:
            return  # the root element — the prolog ended without a DOCTYPE
        if end == -1:
            return  # unterminated; again the parser's error to report
        i = end + (2 if xml_text[i + 1] == "?" else 3)


def load_string(xml_text: str):
    """Parse SCXML text. Returns ``(root_StateNode, meta)`` where ``meta`` has
    ``name`` and ``binding`` ("early"|"late").

    Raises :class:`InsecureDocument` if the document carries a DTD (see :func:`_reject_doctype`).
    """
    _reject_doctype(xml_text)
    return _build_root(ET.fromstring(xml_text))


def load_file(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return load_string(fh.read())
