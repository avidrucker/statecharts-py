"""Events and SCXML dotted-prefix event-name matching.

In SCXML an event name is a dotted token sequence (``error.network.timeout``).
A transition's ``event`` attribute holds a space-separated list of *descriptors*;
a descriptor matches an event if it equals the name, is a dot-delimited prefix of
it, or is the wildcard ``*`` (optionally written as ``foo.*``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class Event:
    """A statechart event. ``data`` is arbitrary payload; ``type`` follows SCXML
    (``platform`` | ``internal`` | ``external``)."""

    name: str
    data: Mapping[str, Any] = field(default_factory=dict)
    type: str = "external"
    sendid: Optional[str] = None
    origin: Optional[str] = None
    invokeid: Optional[str] = None

    def as_data(self) -> dict:
        """The ``_event`` view exposed to guard/action expressions."""
        return {
            "name": self.name,
            "data": dict(self.data),
            "type": self.type,
            "sendid": self.sendid,
            "origin": self.origin,
            "invokeid": self.invokeid,
        }


def coerce_event(ev: Any, data: Optional[Mapping] = None) -> Event:
    """Accept an :class:`Event`, a name string, or anything stringifiable."""
    if isinstance(ev, Event):
        if data:
            return Event(ev.name, {**ev.data, **data}, ev.type, ev.sendid, ev.origin, ev.invokeid)
        return ev
    return Event(str(ev), dict(data or {}))


def _descriptor_matches(descriptor: str, name: str) -> bool:
    if descriptor in ("*", ".*"):
        return True
    # ``foo.*`` is equivalent to the prefix ``foo`` per the SCXML spec.
    if descriptor.endswith(".*"):
        descriptor = descriptor[:-2]
    d = descriptor.split(".")
    n = name.split(".")
    if len(d) > len(n):
        return False
    return n[: len(d)] == d


def event_matches(descriptors: Optional[str], name: str) -> bool:
    """True if any space-separated descriptor in ``descriptors`` matches ``name``.

    ``None`` means an *eventless* transition and never matches a named event.
    """
    if descriptors is None:
        return False
    for token in descriptors.split():
        if _descriptor_matches(token, name):
            return True
    return False
