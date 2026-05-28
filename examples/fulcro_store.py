"""Fulcro-style app state: a normalized store driven by a statechart.

An "edit person" flow. The chart never names a concrete entity — it works through
an *actor* ("form") and *aliases* ("form/name", "form/age"). Swapping the actor
re-points every alias. Run:

    python3 examples/fulcro_store.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statecharts import (  # noqa: E402
    Session, statechart, state, on, handle, transition,
    NormalizedDataModel, initial_store, resolve_actors, store,
)


chart = statechart({"initial": "editing"},
    state({"id": "editing"},
        handle("set-name", lambda env, data: [store.assoc_alias("form/name", data["_event"]["data"]["v"])]),
        handle("set-age", lambda env, data: [store.assoc_alias("form/age", data["_event"]["data"]["v"])]),
        # switch which person the form is editing
        handle("switch", lambda env, data: [store.set_actor("form", ("person/id", data["_event"]["data"]["id"]))]),
        transition({"event": "submit",
                    "cond": lambda env, data: bool(data["form/name"]) and data["form/age"] >= 18,
                    "target": "saved"}),
        on("submit", "invalid"),
    ),
    state({"id": "invalid"}, on("fix", "editing")),
    state({"id": "saved"}),
)


def show(s, label):
    actor = resolve_actors(s.env.data_model.as_data(s.wm.datamodel), "form")["form"]
    print(f"{label:18} state={sorted(s.configuration)}  form-entity={actor}")


def main():
    data = initial_store(
        db={"person/id": {1: {"person/name": "", "person/age": 0},
                          2: {"person/name": "Robin", "person/age": 41}}},
        actors={"form": ("person/id", 1)},
        aliases={"form/name": ("form", "person/name"),
                 "form/age": ("form", "person/age")},
    )
    s = Session(chart, data_model=NormalizedDataModel(), data=data)
    show(s, "start")
    s.send("submit"); show(s, "submit (empty)")     # invalid
    s.send("fix")
    s.send("set-name", {"v": "Sam"}); s.send("set-age", {"v": 25})
    show(s, "after typing")
    s.send("switch", {"id": 2})                       # alias now points at person 2
    show(s, "switch->person 2")
    s.send("set-name", {"v": "Robin Q."}); show(s, "edit person 2")
    s.send("submit"); show(s, "submit (valid)")       # saved
    print("\nfull normalized db:")
    for table, rows in s.wm.datamodel["db"].items():
        for pk, ent in rows.items():
            print(f"  [{table} {pk}] {ent}")


if __name__ == "__main__":
    main()
