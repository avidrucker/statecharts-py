"""W3C SCXML IRP conformance runner.

Loads each ecmascript test under ``cases/`` and runs it to completion under a
*virtual* clock (delayed sends are advanced instantly), then classifies it by the
standard convention: reaching final state ``pass`` => PASS, ``fail`` => FAIL.

Result classes:
  PASS        reached <final id="pass">              (conformant)
  FAIL        reached <final id="fail">              (real semantic failure)
  INCOMPLETE  halted/stuck without pass or fail      (missing feature / no progress)
  SKIP        chart uses an unsupported construct     (e.g. <invoke>, <script>)
  ERROR       raised during load or execution        (loader/evaluator gap)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from statecharts import initialize, make_chart, make_env, process_event  # noqa: E402
from statecharts.ecma import EcmaError, EcmaExecutionModel  # noqa: E402
from statecharts.event_queue import ManualClock, MemoryEventQueue  # noqa: E402
from statecharts.scxml import UnsupportedConstruct, load_file  # noqa: E402

CASES = os.path.join(os.path.dirname(__file__), "cases")


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


def run_case(path: str, step_limit: int = 2000) -> Result:
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        root, meta = load_file(path)
        chart = make_chart(root)
    except UnsupportedConstruct as exc:
        return Result(name, "SKIP", str(exc))
    except Exception as exc:  # noqa: BLE001
        return Result(name, "ERROR", f"load: {type(exc).__name__}: {exc}")

    clock = ManualClock()
    env = make_env(
        chart,
        execution_model=EcmaExecutionModel(),
        event_queue=MemoryEventQueue(clock=clock),
    )
    env.extra["_name"] = meta["name"]
    env.extra["_binding"] = meta["binding"]
    env.extra["_sessionid"] = f"session-{name}"
    env.extra["_base_dir"] = CASES
    try:
        wm = initialize(env)
        steps = 0
        while wm.running and steps < step_limit:
            pending = env.event_queue._pending
            if not pending:
                break  # no more (delayed) events and machine is waiting => stuck
            clock.set(min(e[0] for e in pending))
            due = env.event_queue.tick()
            if not due:
                break
            for ev in due:
                wm = process_event(env, wm, ev)
                steps += 1
                if not wm.running:
                    break
    except (EcmaError, RuntimeError) as exc:
        return Result(name, "ERROR", f"run: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return Result(name, "ERROR", f"run: {type(exc).__name__}: {exc}")

    if "pass" in wm.configuration:
        return Result(name, "PASS")
    if "fail" in wm.configuration:
        return Result(name, "FAIL")
    return Result(name, "INCOMPLETE", f"config={sorted(wm.configuration)} running={wm.running}")


def run_all(verbose: bool = False):
    files = sorted(f for f in os.listdir(CASES) if f.endswith(".scxml"))
    # sub-machine files (testNNNsubM) are auxiliary; skip as top-level cases
    files = [f for f in files if "sub" not in f]
    results = [run_case(os.path.join(CASES, f)) for f in files]
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    total = len(results)
    print(f"\nW3C SCXML conformance (ecmascript, mandatory/auto): {total} tests")
    for status in ("PASS", "FAIL", "INCOMPLETE", "SKIP", "ERROR"):
        n = counts.get(status, 0)
        print(f"  {status:11} {n:3}  ({100*n/total:.0f}%)")
    runnable = counts.get("PASS", 0) + counts.get("FAIL", 0) + counts.get("INCOMPLETE", 0)
    if runnable:
        print(f"  -> {100*counts.get('PASS',0)/runnable:.0f}% pass of {runnable} runnable (non-skip/error)")

    for status in ("FAIL", "INCOMPLETE", "ERROR"):
        named = [r for r in results if r.status == status]
        if named and (verbose or status in ("FAIL",)):
            print(f"\n{status}:")
            for r in named:
                print(f"  {r.name}: {r.detail}")
    return results


if __name__ == "__main__":
    run_all(verbose="-v" in sys.argv)
