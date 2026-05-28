"""Normalized store + actors + aliases — the Fulcro-style app-state pattern.

This is the path toward porting Fulcro-style statechart-driven apps to Python. The
data model is a *normalized* store: entities live once in tables keyed by ident
``(table, id)``; everything else refers to them by ident. Two indirections sit on top:

* **actors** — logical names bound to an ident (e.g. ``"form" -> ("person/id", 1)``),
  so a chart can talk about "the form" without hard-coding which entity it is.
* **aliases** — logical names for one attribute of an actor's entity
  (e.g. ``"form-name" -> ("form", "person/name")``), so expressions read/write a
  field through a stable name even as the actor is re-pointed.

Charts read resolved alias values straight from ``data``; they write through the
``assoc_alias`` / ``set_actor`` / ``assoc_ident`` ops.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple

Ident = Tuple[str, Any]  # (table, id)


def ident(table: str, id_: Any) -> Ident:
    return (table, id_)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssocAliasOp:
    alias: str
    value: Any


@dataclass(frozen=True)
class SetActorOp:
    actor: str
    ident: Ident


@dataclass(frozen=True)
class AssocIdentOp:
    ident: Ident
    attr: str
    value: Any


def assoc_alias(alias: str, value: Any) -> AssocAliasOp:
    return AssocAliasOp(alias, value)


def set_actor(actor: str, ident_: Ident) -> SetActorOp:
    return SetActorOp(actor, ident_)


def assoc_ident(ident_: Ident, attr: str, value: Any) -> AssocIdentOp:
    return AssocIdentOp(ident_, attr, value)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Store layout (the working-memory datamodel dict):
#   {"db": {table: {id: {attr: value}}},
#    "actors": {name: (table, id)},
#    "aliases": {name: (actor, attr)},
#    "local": {scalar session vars}}


class NormalizedDataModel:
    """A DataModel (see protocols.DataModel) backed by a normalized store."""

    @staticmethod
    def _ensure(store: dict) -> dict:
        store.setdefault("db", {})
        store.setdefault("actors", {})
        store.setdefault("aliases", {})
        store.setdefault("local", {})
        return store

    def _alias_target(self, store: dict, alias: str):
        spec = store["aliases"].get(alias)
        if spec is None:
            return None
        actor_name, attr = spec
        ident_ = store["actors"].get(actor_name)
        if ident_ is None:
            return None
        return ident_, attr

    def _entity(self, store: dict, ident_: Ident) -> dict:
        return store["db"].setdefault(ident_[0], {}).setdefault(ident_[1], {})

    # -- DataModel protocol -------------------------------------------------
    def get(self, store: dict, location: str, default: Any = None) -> Any:
        self._ensure(store)
        if location in store["local"]:
            return store["local"][location]
        target = self._alias_target(store, location)
        if target is not None:
            ident_, attr = target
            return self._entity(store, ident_).get(attr, default)
        return default

    def as_data(self, store: dict) -> dict:
        """Flat read view: scalar locals + every alias resolved to its value, plus
        ``__db__``/``__actors__`` for :func:`resolve_actors`."""
        self._ensure(store)
        view = dict(store["local"])
        for alias in store["aliases"]:
            target = self._alias_target(store, alias)
            if target is not None:
                ident_, attr = target
                view[alias] = self._entity(store, ident_).get(attr)
        view["__db__"] = store["db"]
        view["__actors__"] = store["actors"]
        return view

    def transact(self, store: dict, ops) -> dict:
        from .ops import AssignOp, DeleteOp

        self._ensure(store)
        if ops is None:
            return store
        for op in ops:
            if isinstance(op, AssocAliasOp):
                target = self._alias_target(store, op.alias)
                if target is None:
                    raise KeyError(f"unknown alias or unbound actor: {op.alias!r}")
                ident_, attr = target
                self._entity(store, ident_)[attr] = op.value
            elif isinstance(op, SetActorOp):
                store["actors"][op.actor] = op.ident
            elif isinstance(op, AssocIdentOp):
                self._entity(store, op.ident)[op.attr] = op.value
            elif isinstance(op, AssignOp):
                store["local"][op.location] = op.value
            elif isinstance(op, DeleteOp):
                store["local"].pop(op.location, None)
            else:
                raise TypeError(f"Unknown op for NormalizedDataModel: {op!r}")
        return store


# ---------------------------------------------------------------------------
# Expression helpers
# ---------------------------------------------------------------------------


def resolve_actors(data: dict, *names: str) -> dict:
    """In an expression: ``{actor_name: entity_props_dict}`` for the given actors."""
    db = data.get("__db__", {})
    actors = data.get("__actors__", {})
    out = {}
    for name in names:
        ident_ = actors.get(name)
        out[name] = db.get(ident_[0], {}).get(ident_[1], {}) if ident_ else {}
    return out


def resolve_aliases(data: dict, *names: str) -> dict:
    """In an expression: ``{alias: value}`` (all aliases if none named)."""
    if names:
        return {n: data.get(n) for n in names}
    return {k: v for k, v in data.items() if not k.startswith("__") and not k.startswith("_")}


def initial_store(*, db=None, actors=None, aliases=None, local=None) -> dict:
    """Build a starting store for a session's data model."""
    return {
        "db": dict(db or {}),
        "actors": dict(actors or {}),
        "aliases": dict(aliases or {}),
        "local": dict(local or {}),
    }
