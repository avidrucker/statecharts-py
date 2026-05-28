"""Data-model operations returned by action expressions.

An action expression returns an iterable of these ops; the active
:class:`~statecharts.protocols.DataModel` applies them.  Mirrors Fulcrologic's
``com.fulcrologic.statecharts.data-model.operations``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssignOp:
    location: str
    value: Any


@dataclass(frozen=True)
class DeleteOp:
    location: str


def assign(location: str, value: Any) -> AssignOp:
    return AssignOp(location, value)


def delete(location: str) -> DeleteOp:
    return DeleteOp(location)
