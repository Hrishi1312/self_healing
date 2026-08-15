# self_healing

Prompts and tool code for the AAVA pipeline that generates EDI 834 Inbound test cases from
an Azure DevOps user story.

Agent ids in play: **613** scenario generator, **564** test case generator, **559** reviewer,
**367** ADO fetcher; tool **76** drives the rework loop.

## Two designs live here

| | Where | Status |
|---|---|---|
| **Current** — two workflows, model-driven self-heal | repo root: `agents/`, `tool/`, `readme/` | **in production** |
| **Next** — one orchestrator agent + tool, batched and threaded | `testgen_orchestrator/` | designed, not built |

**The decision, taken 2026-08-14:** move to the orchestrator pattern.

Every serious defect in the current design traces to two causes — state travelling through
payloads, and control flow being a model's decision. That produced placeholders that never
bound, an agent asked to copy 100 KB verbatim, a round counter that could reset silently, a
reviewer echoing the whole table until the gateway severed the connection, an unexplained
double execution per trigger, and a PAT in plaintext in exported logs. Patching each one has
cost weeks.

The orchestrator removes the causes: state is a Python dict, the self-heal loop is a `while`,
secrets never enter a prompt. It also enables **batching** — one execution per scenario, run
in parallel, each healing independently — which is the only way to reach 7 scenarios × 3 test
cases × 20 steps without a single response hitting the size that severs connections.

Rationale and trade-offs in full: `testgen_orchestrator/DESIGN.md`.
The current design stays in production and unchanged until the orchestrator is built and proven.

## What's here

| Path | Contents |
|---|---|
| `testgen_orchestrator/` | **the next design** — orchestrator agent + tool, batched, threaded |
| `probe/` | execution-timeout probe: measures the real ceiling, which sizes the orchestrator's budget |
| `agents/agent2_test_scenario_generator.txt` | Story → test scenarios |
| `agents/agent3_test_case_generator.txt` | Scenarios → test cases |
| `agents/agent559_reviewer_llm_judge.txt` | Scores the test cases, fires the rework loop |
| `tool/tool76_rest_api_form_data_caller.py` | Re-triggers the rework workflow, counts rounds, logs decisions |
| `readme/01_architecture.md` | How the workflows, agents and tools connect |
| `readme/02_deployment.md` | Deploy order and verification steps |
| `readme/03_contracts.md` | The handoffs and variables that must not break |
| `readme/04_history_and_findings.md` | What was wrong, what was fixed, what the evidence was |
| `readme/05_open_items.md` | Not done, known risks, decisions still open |
| `CLAUDE.md` | Context loaded automatically by a Claude Code session |

## Start here

1. `readme/01_architecture.md` — what the pipeline does
2. `readme/02_deployment.md` — how to push these four files to AAVA
3. `readme/03_contracts.md` — before editing anything

## The one-line version

Workflow 161 runs four agents; if the reviewer rejects the output it starts workflow 163 as
a rework round, up to three times, then escalates to a human.

## Deploying

Four artefacts, three pastes and one dropdown. All are AAVA drafts, so they're editable in
place — no clone-to-edit. Full steps in `readme/02_deployment.md`.

## Keeping this in sync

The AAVA console has no export that round-trips cleanly, so these files drift the moment
someone edits a prompt in the UI. If a run behaves in a way these files don't explain,
check the console first — the activity log prints each agent's live task prompt, which is
the fastest way to see what actually ran.
