# Examples

Each file is self-contained and runnable with no install (it puts `../src` on the
path). Run any with `python3 examples/<name>.py`.

They're **numbered in a recommended order** — start at 01 (the core engine) and work
up to the advanced layers.

| # | Example | Feature it demonstrates |
|---|---|---|
| 01 | `01_payment_flow.py` | **Core engine** — retry-with-cap (guards), a delayed timeout, exhausted retries |
| 02 | `02_visualize.py` | `to_mermaid` / `to_dot` chart rendering (paste into https://mermaid.live) |
| 03 | `03_load_scxml.py` | Loading a standard **SCXML XML** document + the ECMAScript data model |
| 04 | `04_async_traffic_light.py` | `AsyncSession` — a self-cycling timer chart driven over real time (asyncio) |
| 05 | `05_durable_workflow.py` | SQLite-backed durability — a workflow survives a simulated process restart |
| 06 | `06_invoke_demo.py` | `<invoke>` — a parent runs an inline child statechart; `#_parent` + `done.invoke` |
| 07 | `07_fulcro_store.py` | Normalized store + **actors**/**aliases** (the Fulcro-style app-state pattern) |

Run them all in order:

```bash
for f in examples/[0-9]*.py; do echo "== $f =="; python3 "$f"; done
```
