"""The execution environment: bundles the chart with the four protocol impls.

Passed (as ``env``) to every guard/action expression alongside ``data``.  Extra
host objects (services, app handles) can be attached via ``extra`` and read by
expressions, mirroring Fulcrologic's ``:extra-env``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .chart import Chart
from .data_model import LocalDataModel
from .event_queue import MemoryEventQueue
from .execution_model import CallableExecutionModel


@dataclass
class Environment:
    chart: Chart
    data_model: Any = field(default_factory=LocalDataModel)
    execution_model: Any = field(default_factory=CallableExecutionModel)
    event_queue: Any = field(default_factory=MemoryEventQueue)
    extra: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key):  # convenience for expressions: env["my_service"]
        return self.extra[key]

    def get(self, key, default=None):
        return self.extra.get(key, default)


def make_env(chart: Chart, **kwargs) -> Environment:
    return Environment(chart=chart, **kwargs)
