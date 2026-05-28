"""Default in-memory data model.

The store is a flat ``dict`` keyed by location string.  Treated
immutable-by-convention: the algorithm copies it at step boundaries, so applying
ops in place here is safe.
"""
from __future__ import annotations

from typing import Any, Iterable

from .ops import AssignOp, DeleteOp


class LocalDataModel:
    def get(self, store: dict, location: str, default: Any = None) -> Any:
        return store.get(location, default)

    def as_data(self, store: dict) -> dict:
        return store

    def transact(self, store: dict, ops: Iterable[Any]) -> dict:
        if ops is None:
            return store
        for op in ops:
            if isinstance(op, AssignOp):
                store[op.location] = op.value
            elif isinstance(op, DeleteOp):
                store.pop(op.location, None)
            else:
                raise TypeError(f"Unknown data-model op: {op!r}")
        return store
