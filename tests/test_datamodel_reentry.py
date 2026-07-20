"""Regression test for #38 — early-binding datamodel must not be re-applied on state re-entry.

Graduated from the SCP-C-061 Claim test (code-review finding §1.1). Keeps its Claim ID in the
docstring for verify-claims ledger parity.
"""


def test_scp_c_061_datamodel_not_reapplied_on_entry():
    """SCP-C-061: statecharts' engine re-applies a state's `<data>` on entry after the initial
    macrostep, because `dm_initialized` is rebuilt as `set(wm.configuration)` in `_Run.__init__`
    (`algorithm.py`) and never persisted in `WorkingMemory`, so under early binding a value
    changed while a state is inactive is reset to its `<data>` expr on re-entry. Asserts the
    CORRECT behavior — a value set while B is inactive survives entering B. RED on `0c7776f`
    (`v` resets 5 -> 1 on entry); GREEN once `dm_initialized` is persisted."""
    from statecharts.chart import statechart, state, data_model
    from statecharts import on, handle, ops
    from statecharts.simple import Session

    chart = statechart(
        {"initial": "A"},
        state(
            {"id": "A"},
            handle("setv", lambda env, d: [ops.assign("v", 5)]),
            on("go", "B"),
        ),
        state({"id": "B"}, data_model({"v": 1}), on("back", "A")),
    )
    s = Session(chart)
    assert s.data.get("v") == 1, "early binding: v initialized to 1 at document start"
    s.send("setv")
    assert s.data.get("v") == 5, "v set to 5 while B inactive"
    s.send("go")  # enter B
    assert s.data.get("v") == 5, (
        f"datamodel re-applied on entry: v was reset to {s.data.get('v')!r}, expected 5"
    )
