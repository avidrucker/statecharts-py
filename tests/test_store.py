"""Normalized-store + actors/aliases (Fulcro-style app state) integration tests."""
from statecharts import (
    Session, statechart, state, on, handle, transition, Script,
    NormalizedDataModel, initial_store, resolve_actors, resolve_aliases, store,
)


def make_session():
    chart = statechart({"initial": "editing"},
        state({"id": "editing"},
            # write the form's name field through the alias
            handle("type", lambda env, data: [store.assoc_alias("form-name", "Bo")]),
            on("submit", "review"),
        ),
        state({"id": "review"},
            on("edit", "editing"),
        ),
    )
    data = initial_store(
        db={"person/id": {1: {"person/name": "", "person/age": 30}}},
        actors={"form": ("person/id", 1)},
        aliases={"form-name": ("form", "person/name"),
                 "form-age": ("form", "person/age")},
    )
    return Session(chart, data_model=NormalizedDataModel(), data=data)


def test_alias_resolves_in_view():
    s = make_session()
    # aliases appear as top-level keys in the expression view
    captured = {}
    chart = statechart({"initial": "s"},
        state({"id": "s"}, handle("go", lambda env, data: captured.update(data) or [])),
    )
    s2 = Session(chart, data_model=NormalizedDataModel(),
                 data=initial_store(db={"t": {1: {"a/x": 7}}},
                                    actors={"act": ("t", 1)},
                                    aliases={"x": ("act", "a/x")}))
    s2.send("go")
    assert captured["x"] == 7


def test_assoc_alias_writes_through_to_db():
    s = make_session()
    s.send("type")
    # the write landed on the normalized entity
    assert s.wm.datamodel["db"]["person/id"][1]["person/name"] == "Bo"


def test_set_actor_repoints_alias():
    chart = statechart({"initial": "s"},
        state({"id": "s"},
            handle("swap", lambda env, data: [store.set_actor("cur", ("person/id", 2))]),
            handle("name", lambda env, data: [store.assoc_alias("cur-name", "X")]),
        ),
    )
    data = initial_store(
        db={"person/id": {1: {"person/name": "one"}, 2: {"person/name": "two"}}},
        actors={"cur": ("person/id", 1)},
        aliases={"cur-name": ("cur", "person/name")},
    )
    s = Session(chart, data_model=NormalizedDataModel(), data=data)
    s.send("swap")           # cur now -> person 2
    s.send("name")           # writes through the alias to person 2
    assert s.wm.datamodel["db"]["person/id"][2]["person/name"] == "X"
    assert s.wm.datamodel["db"]["person/id"][1]["person/name"] == "one"  # untouched


def test_resolve_actors_helper():
    captured = {}

    def grab(env, data):
        captured["form"] = resolve_actors(data, "form")["form"]
        captured["aliases"] = resolve_aliases(data, "form-name")
        return []

    chart = statechart({"initial": "s"}, state({"id": "s"}, handle("go", grab)))
    data = initial_store(
        db={"person/id": {1: {"person/name": "Ann", "person/age": 41}}},
        actors={"form": ("person/id", 1)},
        aliases={"form-name": ("form", "person/name")},
    )
    Session(chart, data_model=NormalizedDataModel(), data=data).send("go")
    assert captured["form"] == {"person/name": "Ann", "person/age": 41}
    assert captured["aliases"] == {"form-name": "Ann"}


def test_guard_reads_alias_value():
    chart = statechart({"initial": "gate"},
        state({"id": "gate"},
            transition({"event": "check",
                        "cond": lambda env, data: data["form-age"] >= 18,
                        "target": "adult"}),
            transition({"event": "check", "target": "minor"}),
        ),
        state({"id": "adult"}),
        state({"id": "minor"}),
    )
    data = initial_store(
        db={"person/id": {1: {"person/age": 21}}},
        actors={"p": ("person/id", 1)},
        aliases={"form-age": ("p", "person/age")},
    )
    s = Session(chart, data_model=NormalizedDataModel(), data=data)
    s.send("check")
    assert s.in_state("adult")
