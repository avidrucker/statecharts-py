"""Default execution model: expressions are plain Python callables.

A guard/action expression is either a callable ``(env, data) -> value`` or a
literal value.  This keeps charts as data structures of *callables* (inspectable,
not eval'd strings).
"""
from __future__ import annotations

from typing import Any


class CallableExecutionModel:
    def run(self, env: Any, data: dict, expr: Any) -> Any:
        if callable(expr):
            return expr(env, data)
        return expr
