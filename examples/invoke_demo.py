"""<invoke>: a parent statechart runs a child statechart and reacts to its result.

The parent invokes an inline child "worker". The child reports back to the parent via
#_parent, then finishes — which delivers done.invoke.<id> to the parent. Run:

    python3 examples/invoke_demo.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statecharts import (  # noqa: E402
    Session, statechart, state, final, on_entry, transition, invoke, Send, Script,
)


def log(msg):
    return Script(lambda env, data: print(f"  {msg}") or [])


# Child: tell the parent it started, then finish immediately.
worker = statechart({"initial": "work"},
    state({"id": "work"},
        on_entry(Send("childStarted", target="#_parent")),
        transition({"target": "done"})),   # eventless -> finishes at once
    final({"id": "done"}),
)

# Parent: invoke the worker and react to its signals.
parent = statechart({"initial": "running"},
    state({"id": "running"},
        invoke({"id": "w", "content": worker}),
        on_entry(log("parent: invoked child 'w'")),
        transition({"event": "childStarted"}, log("parent: received childStarted")),
        transition({"event": "done.invoke", "target": "finished"}),
    ),
    final({"id": "finished"}),
)


def main():
    s = Session(parent)  # child runs, signals, finishes during startup
    print(f"  parent ended in {sorted(s.configuration)}  running={s.running}")
    assert "finished" in s.configuration


if __name__ == "__main__":
    main()
