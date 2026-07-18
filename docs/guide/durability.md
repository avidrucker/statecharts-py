# Durable sessions — choosing a backend and writing safe handlers

A durable session lets a workflow **wait across process restarts**: its working memory and
its pending (delayed) events live in a database, not in memory. Charts are *code* (their
guards/actions are Python callables), so they're registered by name in a `ChartRegistry`;
only JSON-able working memory and events are persisted.

There are two backends. They differ in one thing that matters to you as a handler author —
their **delivery guarantee** — so pick with that in mind.

## Choosing a backend

| | `SqliteStore` (`durable.py`) | `PostgresStore` (`durable_postgres.py`) |
|---|---|---|
| **Scope** | single machine | multi-node / multi-worker |
| **Dependency** | stdlib only (`sqlite3`) | the `postgres` extra (`psycopg[binary]`) |
| **Delivery** | **exactly-once** for working memory | **at-least-once** (lease / visibility-timeout) |
| **Concurrency** | safe multi-*process* on one host (WAL + `BEGIN IMMEDIATE`) | N workers pull disjoint work via `FOR UPDATE SKIP LOCKED` |
| **Failure model** | crash mid-delivery → rolled back → redelivered, applied **once** | crash / expired lease → **redelivered** (may apply more than once) |
| **Poison handling** | dead-letter after a cap + per-session retry gate | none yet (retries on the lease interval — see [Known limitations](#known-limitations)) |
| **Runtime** | `DurableRuntime` | `PostgresRuntime` |

**Rule of thumb:** if everything runs in one process (or several processes on one host),
use `SqliteStore` — it's zero-dependency and gives you exactly-once for free. Reach for
`PostgresStore` when you need **more than one machine** delivering a shared queue. That
capability costs you exactly-once: Postgres delivery is at-least-once, so read the next
section before you write a handler.

The precise guarantee is pinned in the [behavior register](../reference/behavior-register.md)
(row 7); each backend's full contract is in its module docstring (`durable.py` /
`durable_postgres.py`).

## The at-least-once idempotency contract (Postgres)

Under `PostgresStore` a worker **leases** a due timer (marks it in-flight), processes it, and
deletes it only after the resulting working memory is committed. If the worker crashes after a
side effect but *before* the delete — or if its lease expires while a slow handler is still
running — another worker re-claims the row and **delivers the same event again**.

So: **a side-effecting guard or action may run more than once for a single logical event.**
Working memory is safe (re-applying an already-applied transition is a no-op on state), but an
*external* side effect is not.

```python
# HAZARD: an action that is not idempotent
def charge_card(env, data):
    payment_api.charge(data["amount"])   # a redelivery charges the card twice
    return []
```

Make the effect idempotent — key it on something stable so a repeat is a no-op:

```python
# SAFE: keyed so a redelivery is a no-op
def charge_card(env, data):
    payment_api.charge(data["amount"], idempotency_key=data["order_id"])
    return []
```

If the downstream system has no idempotency key of its own, dedupe on your side (record
"order_id X was charged" in the same working-memory write, and skip if already present) — the
point is that **the second delivery must not double the effect**. This is the same requirement
the SQLite backend flags for external side effects (SCP-C-015); Postgres promotes it from an
edge case to the default for *all* delivery.

## No deterministic replay

Neither backend replays an event history to reconstruct state (this is the "Absurd" model — a
queue plus a state store — not Temporal's replay-workflow-code model). Only **JSON-able working
memory + pending timers** are persisted. A consequence for Postgres redelivery: non-deterministic
executable content is **re-run, not replayed**. If a guard/action reads the wall clock, calls an
RNG, or does I/O, a redelivery re-executes it against the last committed working memory. The unit
of progress is `(process_event → save_session)`: only committed working memory survives a
restart; an in-flight step that didn't commit is retried from the last committed state.

## Operational shape (Postgres)

Delivery is **polled**, exactly like the SQLite runtime — you drive it:

```python
from statecharts import ChartRegistry, ManualClock
from statecharts.durable_postgres import PostgresStore, PostgresRuntime

store = PostgresStore("postgres://…/mydb")          # DEFAULT_LEASE_S = 30.0
rt = PostgresRuntime(store, ChartRegistry().register("flow", flow_chart()))
rt.start("flow", "job-42")
rt.enqueue("job-42", "begin")

while True:                    # one worker; run this on as many nodes as you like
    rt.tick(lease=30.0)       # claim + deliver everything currently due, then sleep
    time.sleep(poll_interval)
```

- **Lease** — an in-flight row older than `lease` seconds is considered abandoned and becomes
  re-claimable. Size it comfortably above your slowest handler, or a slow-but-alive worker will
  have its row stolen and the event delivered twice. `claim()` and `tick()` both take a `lease`;
  the store's default is `PostgresStore.DEFAULT_LEASE_S`.
- **Many workers, no coordination** — `FOR UPDATE SKIP LOCKED` lets N `tick()` loops on N nodes
  pull disjoint rows without blocking on a shared queue head.

## Known limitations

The Postgres backend is deliberately minimal today (the SQLite-specific exactly-once machinery
does **not** port — see the module docstring). Two gaps to know about, both scoped as follow-ups
in the [design doc](../design/postgres-durability.md) (RQ4):

- **No per-session ordering across workers.** Two workers may process one session's events
  concurrently, so a later event can overtake an earlier one. If you need a session's events
  serialized, that requires a per-session advisory lock (or claim-by-`session_id`) — not yet
  built.
- **No dead-letter cap.** A permanently-failing (poison) event is left in-flight and retried on
  every lease interval, indefinitely — there is no equivalent of SQLite's dead-letter table and
  per-session retry gate yet.

If your workload needs either, prefer `SqliteStore` for now, or track/file the corresponding
follow-up.

## Setup

The Postgres backend is an **optional extra** — the default install stays zero-dependency:

```bash
pip install -e '.[postgres]'      # installs psycopg[binary] (bundles libpq; no system libpq-dev)
```

Point the store at your server via a DSN (the tests read `DATABASE_URL`, defaulting to
`postgres://postgres:postgres@localhost:5432/statecharts_dev`). The Postgres-backed tests
**skip cleanly** when psycopg isn't installed or no server answers, so the zero-dependency suite
(`python3 run_tests.py`) and CI stay green without Postgres.
