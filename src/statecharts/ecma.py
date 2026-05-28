"""A pragmatic ECMAScript-*subset* execution model for the W3C conformance suite.

The W3C ecmascript tests embed small JS expressions in ``cond``/``expr`` attributes.
Rather than a full JS engine, we translate the common subset to Python and evaluate
it in a restricted namespace.  This is the "ExecutionModel" seam in action: native
charts use Python callables; conformance charts use this string evaluator.

Known unsupported constructs (tests using them are reported, not silently passed):
inline ``function(){...}`` IIFEs, ternaries, full ``Array``/``String`` prototypes.
"""
from __future__ import annotations

import math
import re
from typing import Any


class JSObject(dict):
    """A dict with JS-style attribute access; missing keys read as ``undefined``."""

    def __getattr__(self, name: str) -> Any:
        if name in self:
            return to_js(dict.__getitem__(self, name))
        return None  # JS: undefined

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __getitem__(self, key: Any) -> Any:
        return to_js(dict.__getitem__(self, key))


def to_js(value: Any) -> Any:
    if isinstance(value, JSObject):
        return value
    if isinstance(value, dict):
        return JSObject(value)
    return value


def _typeof(x: Any) -> str:
    if x is None:
        return "undefined"
    if isinstance(x, bool):
        return "boolean"
    if isinstance(x, (int, float)):
        return "number"
    if isinstance(x, str):
        return "string"
    if callable(x):
        return "function"
    return "object"


# --- JS -> Python source translation ---------------------------------------

_TYPEOF_RE = re.compile(r"typeof\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\[[^\]]*\])*)")


def js_to_py(src: str) -> str:
    s = src
    s = s.replace("===", "==").replace("!==", "!=")
    s = _TYPEOF_RE.sub(r"__typeof__(\1)", s)
    s = s.replace("&&", " and ").replace("||", " or ")
    # unary ! (not part of !=) -> Python "not"
    s = re.sub(r"!(?!=)", " not ", s)
    return s


def _base_namespace() -> dict:
    return {
        "true": True,
        "false": False,
        "null": None,
        "undefined": None,
        "Math": math,
        "Infinity": math.inf,
        "NaN": math.nan,
        "__typeof__": _typeof,
        "String": lambda x="": "" if x is None else str(x),
        "Number": lambda x=0: float(x),
        "Boolean": bool,
        "parseInt": lambda x, *a: int(x),
        "parseFloat": float,
    }


class EcmaError(Exception):
    pass


def js_eval(src: str, variables: dict, In=None) -> Any:
    ns = _base_namespace()
    ns.update(variables)
    if In is not None:
        ns["In"] = In
    code = js_to_py(src)
    try:
        return eval(code, {"__builtins__": {}}, ns)  # noqa: S307 (restricted)
    except EcmaError:
        raise
    except Exception as exc:  # surfaces as SCXML error.execution in faithful impls
        raise EcmaError(f"{src!r} -> {code!r}: {exc}") from exc


class EcmaExecutionModel:
    """Evaluates string expressions as JS-subset; passes callables through."""

    def run(self, env: Any, data: dict, expr: Any) -> Any:
        if callable(expr):
            return expr(env, data)
        if isinstance(expr, str):
            variables = {}
            allowed_underscore = ("_event", "_name", "_sessionid", "_ioprocessors")
            for k, v in data.items():
                if k.startswith("_") and k not in allowed_underscore:
                    continue
                variables[k] = to_js(v)
            config = data.get("_configuration", frozenset())
            return js_eval(expr, variables, In=lambda sid: sid in config)
        return expr
