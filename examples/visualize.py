"""Render a chart to Mermaid and Graphviz DOT.

Paste the Mermaid output into https://mermaid.live, or pipe the DOT into
``dot -Tpng`` (Graphviz). Run:

    python3 examples/visualize.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statecharts import (  # noqa: E402
    statechart, state, parallel, final, history, transition, on, to_mermaid, to_dot,
)

chart = statechart({"initial": "idle"},
    state({"id": "idle"}, on("start", "active")),
    state({"id": "active", "initial": "work"},
        history({"id": "h", "type": "shallow"}, transition({"target": "work"})),
        parallel({"id": "work"},
            state({"id": "io", "initial": "reading"},
                state({"id": "reading"}, on("wrote", "writing")),
                state({"id": "writing"}, on("read", "reading"))),
            state({"id": "ui", "initial": "shown"},
                state({"id": "shown"}, on("hide", "hidden")),
                state({"id": "hidden"}, on("show", "shown"))),
        ),
        on("pause", "paused"),
        on("finish", "done")),
    state({"id": "paused"}, on("resume", "h")),
    final({"id": "done"}),
)


def main():
    print("=" * 60, "\nMERMAID (paste into https://mermaid.live):\n", "=" * 60, sep="")
    print(to_mermaid(chart))
    print("\n" + "=" * 60, "\nGRAPHVIZ DOT (pipe into: dot -Tpng -o chart.png):\n", "=" * 60, sep="")
    print(to_dot(chart))


if __name__ == "__main__":
    main()
