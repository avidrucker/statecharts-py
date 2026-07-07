# Behavior register — deliberate divergences

Where `statecharts-py` makes a **deliberate choice** about a corner of SCXML semantics,
this table records it: what the W3C spec (and upstream
[`fulcrologic/statecharts`](https://github.com/fulcrologic/statecharts)) say, what we do,
why, and the test that pins it. These are decisions, not accidents — several make this port
*stricter* / more complete than the Clojure library it was ported from.

For the separate question of the 3 non-green W3C tests (embedded-scripting scope), see
[`why-98-percent-passing.md`](../../why-98-percent-passing.md).

| # | Concern | Spec / upstream position | Our behavior | Anchor | Pinned by |
|---|---|---|---|---|---|
| 1 | Executable-content error semantics | W3C §4.4: on error, queue `error.execution` **and abort the rest of the block**. Upstream makes the abort **opt-in** (`(simple/strict-env)`). | **Strict by default** — an error queues `error.execution` and aborts the remaining sibling content. | `algorithm.py:646` (`_run_block` except) | `test_semantics.py::test_system_variable_write_raises_and_aborts_block`, `::test_error_execution_precedes_done_state_event` |
| 2 | Writing a system variable | W3C §5.10: assigning to `_sessionid`/`_name`/`_event`/`_ioprocessors` must raise `error.execution`. Upstream does **not** enforce it (pluggable DataModel). | **Enforced** — the `<assign>` raises `error.execution`. | `algorithm.py:689` | `test_semantics.py::test_system_variable_write_raises_and_aborts_block` |
| 3 | `error.communication` | An unreachable/unknown send target should raise `error.communication`. Upstream does **not** implement it (skips W3C test 496). | **Implemented, async, non-block-aborting** — the sibling content after a failing `<send>` still runs; the error is delivered afterward. | `algorithm.py:742`, `769`–`770` | `test_semantics.py::test_error_communication_is_async_and_non_aborting` |
| 4 | `_ioprocessors` system variable | Optional; upstream does **not** expose it. | **Populated** in the data view with the SCXML event-processor entry. | `algorithm.py:92` | `test_semantics.py::test_ioprocessors_system_variable_populated` |
| 5 | Data binding | W3C allows `binding="early"` or `"late"`. Upstream supports **early only**. | **Both** — with `_binding="late"`, a declared `<data>` is `None` until its owning state is entered. | `algorithm.py:125` | `test_semantics.py::test_late_binding_defers_data_until_entry` |
| 6 | Embedded scripting corners: `<script>` flow control, inline `<scxml>` as a data value, cross-session delayed-event cancel | Large embedded-JS surface / one spec-undefined corner. | **Unsupported** (deliberate, reversible via the `ExecutionModel` seam). | — | [`why-98-percent-passing.md`](../../why-98-percent-passing.md) (W3C tests 302/303/304 SKIP; 224/530/207 FAIL) |

## Notes

- **Rows 1–5 make us diverge from upstream by being *stricter* or *more complete*.** A
  developer porting a chart from `fulcrologic/statecharts` should read
  `docs/guide/porting-from-clojure.md` (forthcoming — issue #5), which will link back here
  for the authoritative list.
- **Row 1 vs Row 2 interact:** because block-abort is strict by default (row 1), the illegal
  system-variable write in row 2 also prevents the *rest of that entry/exit block* from
  running. The `error.communication` path (row 3) is the deliberate exception — it does
  **not** abort the block.
- Every "chosen behavior" row above links to a test that can actually fail. The
  system-variable and `error.communication` guards were both proven non-vacuous when their
  tests were written (a legal assign / reachable target flips the assertions).
