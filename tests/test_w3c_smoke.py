"""Fast W3C conformance regression guard.

Runs a curated subset of the real W3C mandatory ecmascript tests (one per major
feature area) and asserts each reaches the `pass` state. The full suite lives in
tests/w3c/runner.py; this keeps the main test run quick while catching regressions.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests", "w3c"))

from runner import CASES, run_case  # noqa: E402

# (test id -> what it exercises)
REPRESENTATIVE = {
    "test144": "internal event ordering (FIFO)",
    "test279": "early binding of unentered-state data",
    "test280": "late binding",
    "test403b": "optimally-enabled-set is a set",
    "test404": "exit order (reverse document order)",
    "test406": "entry order (document order)",
    "test412": "initial-transition content ordering",
    "test421": "internal events take priority over external",
    "test159": "error aborts subsequent executable content",
    "test294": "donedata param/content populates done event",
    "test529": "content body as event data (loose equality)",
    "test189": "#_internal send target",
    "test152": "illegal foreach array -> error.execution",
    "test496": "unknown session target -> error.communication",
}


def _check(test_id):
    r = run_case(os.path.join(CASES, f"{test_id}.scxml"))
    assert r.status == "PASS", f"{test_id} ({REPRESENTATIVE[test_id]}): {r.status} {r.detail}"


def test_w3c_representative_subset():
    for test_id in REPRESENTATIVE:
        _check(test_id)
