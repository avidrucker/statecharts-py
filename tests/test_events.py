from statecharts import event_matches
from statecharts import Session, statechart, state, transition, on


def test_event_prefix_matching():
    assert event_matches("error", "error.network.timeout")
    assert event_matches("error.network", "error.network.timeout")
    assert event_matches("error.network.timeout", "error.network.timeout")
    assert not event_matches("error.network", "error.disk")
    assert event_matches("*", "anything.at.all")
    assert event_matches("error.*", "error.network")
    assert not event_matches("error.network.timeout", "error.network")  # descriptor longer
    assert not event_matches(None, "error")


def test_multiple_descriptors():
    assert event_matches("foo bar", "bar.baz")
    assert not event_matches("foo qux", "bar.baz")


def test_prefix_routing_specific_wins_by_document_order():
    chart = statechart({"initial": "h"},
        state({"id": "h"},
            transition({"event": "error.network", "target": "net"}),
            transition({"event": "error", "target": "generic"}),
        ),
        state({"id": "net"}),
        state({"id": "generic"}),
    )
    s = Session(chart)
    s.send("error.network.timeout")
    assert s.in_state("net")

    s2 = Session(chart)
    s2.send("error.disk")
    assert s2.in_state("generic")
