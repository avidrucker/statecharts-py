"""Load a standard SCXML XML document and run it (ECMAScript data model).

Shows the XML loader + the ECMAScript-subset evaluator that drive the W3C suite.
Run:

    python3 examples/load_scxml.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from statecharts import initialize, make_chart, make_env, process_event  # noqa: E402
from statecharts.ecma import EcmaExecutionModel  # noqa: E402
from statecharts.scxml import load_string  # noqa: E402

XML = """<?xml version="1.0"?>
<scxml xmlns="http://www.w3.org/2005/07/scxml" datamodel="ecmascript" initial="counting">
  <datamodel><data id="n" expr="0"/></datamodel>
  <state id="counting">
    <transition event="tick" cond="n &lt; 2" target="counting">
      <assign location="n" expr="n + 1"/>
    </transition>
    <transition event="tick" target="done"/>
  </state>
  <final id="done"/>
</scxml>"""


def main():
    root, meta = load_string(XML)
    chart = make_chart(root)
    env = make_env(chart, execution_model=EcmaExecutionModel())
    env.extra["_name"] = meta["name"]

    wm = initialize(env)
    print(f"start:  config={sorted(wm.configuration)}  n={wm.datamodel['n']}")
    for i in range(3):
        wm = process_event(env, wm, "tick")
        print(f"tick {i}: config={sorted(wm.configuration)}  n={wm.datamodel['n']}  running={wm.running}")


if __name__ == "__main__":
    main()
