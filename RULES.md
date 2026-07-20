# RULES

House conventions for agents working on **statecharts-py**. One file, so any agent
(JACKFRUIT and friends) can be onboarded without reverse-engineering the style from prior
issues and commits.

**This file *adds to*, and never overrides, the ambient `CLAUDE.md` files** — the
machine-global `~/.claude/CLAUDE.md` ("suggest, don't act") and the Study-tree
`~/Documents/Study/CLAUDE.md` (git identity, Work-vs-Study separation). Where a rule here
looks like it conflicts with those, those win; this file only tightens or specialises.

It documents the **status quo**. Changing an actual convention is a separate ticket, not an
edit to this file (see the `[design]` / `[research]` flow below). Kept lean on purpose: a
rule earns a spot only if breaking it on a random task is both plausible and harmful.

**Rule IDs** (`A1`, `B2`, …) are stable citation stems — cite a rule by its ID in reviews and
comments. They are hand-maintained and not renumbered when the list is trimmed.

Modeled on the pmtools `RULES.md` (`~/Documents/Study/Python/pmtools/RULES.md`); the
`pmtools …` command semantics referenced below are governed there and in its `CONTRACT.md`.

---

## A. Tickets

- **A1 — Title carries a `[type]` prefix.** The types in use are `[feat]`, `[docs]`,
  `[test]`, `[perf]`, `[research]`, `[design]`, `[infra]` (grounded: `gh issue list --state
  all` — e.g. #28 `[infra]`, #27 `[docs]`, #24 `[feat]`). Pick the dominant one.
- **A2 — Body is a complaint: `## Have / Should have`, then `## Acceptance criteria`, then
  `## Estimate` (`H: <minutes>`).** This is the shape every recent ticket uses (#27, #28).
  yegor-bdd: state what's broken/missing and what "done" looks like, verifiably.
- **A3 — One deliverable per ticket.** Bundled work (a refactor *and* a docs update *and* a
  script) is split; use cross-references for related work. A ticket that overruns its `H:`
  estimate is decomposed, not stretched.
- **A4 — The reporter closes.** Do not close a ticket you did not file unless explicitly
  asked; a WRITER/DEV agent lands the PR, the reporter verifies and closes (yegor-bdd).
- **A5 — Deferred or found work becomes a ticket before you close** — a bug found, scope
  dropped, or a design question opened. The closing comment cites the ticket number(s)
  rather than describing the work in prose. (This session's own example: the Postgres RQ4
  gaps are named in `docs/guide/durability.md` "Known limitations"; the branch-naming
  override became `avidrucker/pmtools#127`.)

## B. Commits & PRs

- **B1 — Commit subject is `type(#issue): summary`, and the squash-merge subject carries the
  PR: `type(#issue): summary (#PR)`.** Grounded in `git log origin/main` —
  `feat(#28): convert to fleet mode … (#33)`, `docs(#30): durability guide …`.
- **B2 — Every commit ends with the `Co-Authored-By` trailer**
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` (present on all
  recent commits).
- **B3 — Never commit on `main`; branch first.** Feature branches follow
  `<agent>/issue-<N>` (e.g. `jackfruit/issue-27`) when hand-driven, or the
  pmtools-generated `br-<agent>/<project>-<lang>-issue-<N>` under fleet mode (§C).
- **B4 — A change ships only with the gate green (§E) and, for a bugfix, its regression
  test (§F).** A testless bugfix PR is auto-reject (yegor-review).

## C. Fleet workflow (pmtools)

statecharts-py is a **fleet-mode** pmtools project as of #28 (`.claude/orchestrate.json`,
`mode: "fleet"`). It is *not* self-hosting pmtools — pmtools is an external tool here — so
there is no "tool under repair" caveat; use the `pmtools` commands normally.

- **C1 — Claim through pmtools; close/release through pmtools.** `pmtools claim <N> --as
  <fruit>` stakes a worktree under `.claude/worktrees/`; `pmtools close <N>` does the
  race-safe land + teardown; `pmtools release <N>` abandons a claim (issue stays OPEN).
  Don't hand-roll the push+teardown unless a human authorises a direct merge in-session.
- **C2 — No-code / research tickets close differently.** A ticket with no `Closes #N` commit
  (research, comment-only) is closed with `gh issue close <N>` after posting the finding,
  then `pmtools release <N>` to drop the claim ref + worktree. Never fabricate a no-op
  commit to satisfy `pmtools close`.
- **C3 — Run the gate right after claim, before changing anything (§E).** A fresh worktree
  branches off current `main`; confirm green first so a pre-existing failure is never
  attributed to your change.
- **C4 — Velocity logging does not apply here.** `storage.velocity.enabled = false`
  (`.claude/orchestrate.json`, per #28's settled decision 3), so — unlike pmtools — no
  velocity row is required before close.
- **C5 — Error self-audit before an outward-facing close.** `storage.errors.enabled =
  true`. Re-read the session from claim to now, log any loggable errors (`pmtools error log
  '<json>'`), and state the outcome in the closing comment (`error self-audit: N row(s)`
  or `no loggable errors this session`).
- **C6 — Branch/worktree naming lives in pmtools, not in config.** `orchestrate.json` does
  **not** carry a naming pattern (a per-project override is `avidrucker/pmtools#127`, unbuilt);
  pmtools' `build_branch` owns the shape. Don't re-add a `worktreeBranchPattern` field —
  pmtools reads it nowhere.

## D. Labels

- **D1 — `severity:*` is for defects only.** Features/enhancements carry `enhancement` (or a
  `[type]` prefix) and no `severity:*`; they rank below triaged bugs by design. The shared
  taxonomy is created by `scripts/create-standard-labels.sh`.
- **D2 — `blocked` encodes real dependencies** — prefer it over faking severity to express
  ordering.
- **D3 — Each ticket gets exactly one `area:*` label.** The nine project areas are `area:algo`,
  `area:durable`, `area:model`, `area:bench`, `area:docs`, `area:tracker`, `area:scxml`,
  `area:viz`, and `area:xcc` (cross-cutting catch-all, used **sparingly**) — defined in #28.
  A ticket spanning two lanes picks the dominant one and suggests a split.

## E. Test & build gate

- **E1 — The zero-dep runner is the fast local gate: `python3 run_tests.py`.** The package
  has **zero runtime dependencies**; the runner loads `tests/test_*.py` and calls each
  top-level `test_*` function with no arguments (no pytest, no fixtures) — keep new tests in
  that shape so the default install stays dependency-free.
- **E2 — Conformance floor: `python3 tests/w3c/runner.py --min 150`.** A change must not drop
  the W3C pass count below 150 (current: 153). Both E1 and E2 are what `pmtools close`
  runs as `close.verify` (cwd = worktree, no `.venv` needed — they're zero-dep).
- **E3 — CI is the merge gate** (`.github/workflows/ci.yml`): `pytest` + `python
  tests/w3c/runner.py --min 150` across Python 3.10–3.13. `pytest` is a dev-only extra
  (`pip install -e ".[dev]"`); it discovers the same `test_*` functions.

## F. Testing discipline

- **F1 — Every bugfix lands a regression test in the same commit** — the test is what stops
  the bug returning (yegor-unit-tests). A fix without a test is not done.
- **F2 — The test must be able to fail.** Confirm it's red without the fix and green with it;
  a test that has never been red proves nothing.
- **F3 — Deliberate SCXML-semantics choices are pinned in the behavior register**
  (`docs/reference/behavior-register.md`) with the test that fixes each. A change that
  alters a corner of semantics updates the register, not just the code.

## G. Claims ledger (verify-claims)

- **G1 — A load-bearing numeric/behavioural assertion gets pinned in `claims-data/` before
  you rely on it** — "a number you did not pin is a number you have already lost." The ledger
  is git-excluded (a working epistemic record); what graduates out is a committed findings
  doc or an issue. Config: `.claude/ledger.json` (prefix `SCP`).
- **G2 — Situate overloaded terms.** When a headline uses one of `claims.overloadedTerms`
  (`tick`, `store`, `atomic`, `recover`, `defer`, `poison`, `backoff`, `session`, `claim`),
  qualify it (`DurableRuntime.tick`, not bare `tick`) — see `claims-data/README.md`.
- **G3 — Verifier ≠ asserter for anything load-bearing.** The independent `/code-review`
  workflow and reproducing red→green tests are the verifiers; the main-loop agent is the
  asserter. Full admission rubric and evidence-kind table live in `claims-data/README.md`.

## H. Grounding

- **H1 — Point at named landmarks, not raw line numbers.** In any authored reference (ticket,
  comment, review, commit, doc), cite a function/class name + file path for code, or a
  section heading for markdown — never `file.py:73`. Line numbers drift and misdirect.
  Carve-out: commit-pinned permalinks and quoted tool output keep their own line numbers.
- **H2 — Verify live state before asserting it.** Re-query an issue's OPEN/CLOSED state, a
  file's contents, or the test count in the same turn (`gh`/`git`/read) rather than trusting
  memory or a prior turn.
- **H3 — Ground every convention claim in an artifact.** A rule, a "we do X", or a review
  citation names the issue, commit, config file, or code landmark behind it — including in
  this file.

## I. Identity & dependencies

- **I1 — Git identity is inherited, not restated.** Commits use the Study-tree identity
  (`avidrucker` + the GitHub noreply email) via `~/.gitconfig`'s include of the tracked
  dotfiles config — see `~/Documents/Study/CLAUDE.md`. Don't hardcode or override it per-repo.
- **I2 — Adding a runtime dependency needs explicit human approval — propose, don't
  install.** The package is zero-runtime-dependency by design (`pyproject.toml`
  `dependencies = []`); optional backends go in an extra (e.g. `postgres`), and the default
  install and `python3 run_tests.py` must stay dependency-free. *Using* a declared dep and
  *suggesting* a library are fine; the gate is on installing.
