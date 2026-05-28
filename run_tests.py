#!/usr/bin/env python3
"""Zero-dependency test runner (stands in for `pytest` where it isn't installed).

Discovers `test_*.py` files under tests/, runs every top-level `test_*` function,
and reports pass/fail. Run with: `python3 run_tests.py`
"""
import importlib.util
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))
TESTS = os.path.join(ROOT, "tests")


def load_module(path):
    name = "t_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    files = sorted(f for f in os.listdir(TESTS) if f.startswith("test_") and f.endswith(".py"))
    passed = failed = 0
    failures = []
    for f in files:
        mod = load_module(os.path.join(TESTS, f))
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"  PASS {f}::{name}")
            except Exception:
                failed += 1
                failures.append((f, name, traceback.format_exc()))
                print(f"  FAIL {f}::{name}")
    print("\n" + "=" * 60)
    for f, name, tb in failures:
        print(f"\nFAILURE: {f}::{name}\n{tb}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
