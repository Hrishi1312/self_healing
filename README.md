# self_healing

Prompts and tool code for the AAVA workflow that generates EDI 834 Inbound test cases from
an Azure DevOps user story.

Agent ids in play: **613** scenario generator, **564** test case generator, **559** reviewer,
**367** ADO fetcher; tool **76** drives the rework loop.

## What's here

| Path | Contents |
|---|---|
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
