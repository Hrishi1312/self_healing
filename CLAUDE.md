# self_healing — EDI 834 Inbound test generation on AAVA

Working copies of the prompts and tool code for the AAVA workflow that turns an Azure
DevOps user story into EDI 834 Inbound test cases. **These files are the source of truth;
the AAVA console is deployed from them, not the other way round.** After editing here,
paste into the console. After editing in the console, copy back here.

For AAVA platform concepts (agents, tools, KBs, lifecycle, the `{{variable}}` rule),
invoke the `aava-main` skill — it routes to the right reference.

## Layout

```
agents/   the three prompts that change
tool/     the CrewAI tool that drives the rework loop
readme/   architecture, deployment, contracts, history, open items
```

## What this pipeline is

Workflow **161** (initial pass): ADO fetcher → scenario generator → test case generator →
LLM-as-a-judge reviewer. If the reviewer rejects, its tool starts workflow **163**
(generator → reviewer only) as a rework round. Bounded at 3 rounds, then escalate.

## Invariants — breaking any of these breaks the pipeline silently

1. **`{{name_type_required}}` placeholders.** A variable only binds when wrapped in double
   braces and spelled character-for-character. Bare `tsInputJson_string_true` is read as
   literal text and the data never arrives. This has bitten this project twice.
2. **Agent 3 must emit `{ testcases, scenariojson }` — exactly two fields, nothing else.**
   The reviewer parses it as JSON and copies `scenariojson` into the tool call. Any
   narrative before or after the object breaks the loop.
3. **The 13-column header must be byte-identical in agent 3 and agent 559.**
4. **Volume numbers must match between agent 3 and agent 559 check 8.** If the generator is
   told one limit and the gate enforces another, every compliant run is rejected.
5. **The confidence threshold lives in two places** — `_CONFIDENCE_THRESHOLD` in the tool
   and `approved: true if confidence >= N` in agent 559. Change both or neither.
6. **Column semantics:** `Status` = Positive/Negative/Edge, `Test Case Type` =
   Functional/Regression. This matches the existing manual test cases so generated rows
   can be imported alongside them. They are easy to swap and have been swapped before.

## Current settings

| Setting | Value | Where |
|---|---|---|
| Scenarios per story | max 4 | agent 2 |
| Test cases per scenario | 3 (Positive + Negative + Edge; 2 allowed if a type doesn't apply) | agent 3 |
| Steps per test case | 15–20 | agent 3 |
| Test cases total | max 20 | agent 3 |
| Approval threshold | 90 | tool + agent 559 |
| Rework rounds | max 3, then escalate | tool |
| Agent 559 `maxIter` | 2 — **live value is still 10**, change it | console config, not in any file |
| Platform execution ceiling | 600 s | AAVA, not ours |

## Before changing volume, read this

Output size is what the 600-second ceiling bites on:

**rows = scenarios × cases per scenario × steps per case**

Measured: **~1,300 characters per step row**. A run of **80 rows / 104 KB completed cleanly**.
A single completion of ~109 KB has previously been severed mid-flight by the gateway
(`RemoteDisconnected`). Current settings produce **180–240 rows**, which is above anything
yet proven. If runs start timing out, the cheapest rollback is agent 2's scenario cap.

## How to verify a run

Grep the activity log for `AAVA-LOOP`. Every tool decision emits one timestamped line —
the only timestamps in the log, since the platform emits none.

```
[AAVA-LOOP] ts=… decision=APPROVED_STOP confidence=92 threshold=90 round=0/3 …
[AAVA-LOOP] ts=… decision=REWORK_TRIGGERED confidence=85 round=1/3 child_execution_id=… post_ms=412 …
[AAVA-LOOP] ts=… decision=LIMIT_REACHED round=3/3 note=rework_limit_reached_escalate_to_human
```

Then check the generated table: `Status` holds Positive/Negative/Edge, the trading partner
is named, and no `<STATE>`-style token survives (`<ISA13>`-style runtime data is fine).

## Known-good reference

The human-authored test cases for the same story — 21 test cases, 23 steps each, 11 columns
— are the quality bar. See `readme/04_history_and_findings.md` for how the generated output
compares. That file lives outside this repo; its path is recorded there.

## Two designs live in this repo

| | Where | Status |
|---|---|---|
| Current — two workflows, model-driven self-heal | repo root `agents/`, `tool/` | in production |
| Next — one orchestrator agent + tool, batched and threaded | `testgen_orchestrator/` | built, 136 offline checks pass, not yet run on the platform |

**Client constraint: Azure Container Apps severs a request at 240 seconds.** This governs
every volume decision in `testgen_orchestrator/`. The current design fits 8 scenarios x 3 test
cases x 15-18 steps with one heal round only because the generator emits nested JSON and the
assembled table goes to stdout rather than back through the calling agent.

**Decision taken 2026-08-14: move to the orchestrator pattern.** Every serious defect in the
current design came from state travelling through payloads and control flow being a model's
decision. The orchestrator removes both causes and enables per-scenario batching. Full
rationale in `testgen_orchestrator/DESIGN.md`.

Until the orchestrator is built and proven, the invariants above still govern the current
design. Do not change one design to suit the other.
