# Examples

Each file is self-contained and runnable with no install (it puts `../src` on the
path). Run any with `python3 examples/<name>.py`.

| Example | Feature it demonstrates |
|---|---|
| `payment_flow.py` | Core engine: retry-with-cap (guards), a delayed timeout, exhausted retries |
| `invoke_demo.py` | `<invoke>` — a parent runs an inline child statechart; `#_parent` + `done.invoke` |
| `fulcro_store.py` | Normalized store + **actors**/**aliases** (the Fulcro-style app-state pattern) |
| `async_traffic_light.py` | `AsyncSession` — a self-cycling timer chart driven over real time (asyncio) |
| `durable_workflow.py` | SQLite-backed durability — a workflow survives a simulated process restart |
| `visualize.py` | `to_mermaid` / `to_dot` chart rendering (paste into https://mermaid.live) |
| `load_scxml.py` | Loading a standard **SCXML XML** document + the ECMAScript data model |

Run them all:

```bash
for f in examples/*.py; do echo "== $f =="; python3 "$f"; done
```
