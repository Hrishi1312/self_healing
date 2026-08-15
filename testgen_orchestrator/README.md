# testgen_orchestrator

The **next** design for EDI 834 test case generation: one orchestrator agent and one
orchestrator tool, replacing the two-workflow pipeline in the repo root.

**Status: designed, not built.** `DESIGN.md` is approved in shape; the tool and the adapted
agent prompts are the next step.

## Why this exists

The pipeline in the repo root (`agents/`, `tool/`) works, but every serious defect found in
it came from the same two causes: **state travelling through payloads**, and **control flow
being a model's decision**. That produced placeholders that never bound, an agent asked to
copy 100 KB byte-for-byte, a round counter that could silently reset, a reviewer echoing the
generator's whole table until the gateway cut the connection, an unexplained double execution
per trigger, and a PAT in plaintext in exported logs.

This design removes the causes rather than patching the symptoms. State lives in a Python
dict. The self-heal loop is a `while`. Secrets are resolved in Python and never enter a
prompt. See `DESIGN.md` §1 and §12.

It also adds what the current design cannot do: **batching**. One execution per scenario,
run in parallel, each healing independently. That is what makes 7 scenarios × 3 test cases ×
20 steps possible — 540,000 characters total, but never more than ~69,000 in any single
completion.

## Layout

```
DESIGN.md     the full design — read this first
agents/       prompts for the orchestrator and the three subagents
tool/         AavaTestGenOrchestrator.py and its local runner
```

## What changes for the subagents

Agents 613, 564 and 559 are kept — their judging and generation logic is unchanged. What they
lose is the plumbing:

| Removed from the prompts | Because |
|---|---|
| `{{tsInputJson_string_true}}` and friends | state is passed as a function argument |
| "copy `scenariojson` verbatim, byte-for-byte" | the orchestrator already holds it |
| tool-call instructions and argument shapes | no agent calls a tool |
| round-number passing | it is a local variable |

Agent 564 gains one thing: it receives **one scenario**, not an array.

## Retired when this ships

Workflow 163, tool 76, agent 367 with tool 2 (the ADO fetch becomes a REST call), and every
`{{ }}` variable in the chain. See `DESIGN.md` §12.

## Before building

One number is unverified and it sizes the budget: the real execution ceiling. Observed is
600 s on a 4-agent pipeline; every agent is configured `maxExecutionTime: 3600`. Whether a
single-agent workflow is also cut at 600 s is unknown.

`../probe/AavaExecutionTimeoutProbe.py` measures it. Run it at `timeout_seconds: 1200`:

- it raises its own error → the ceiling is above 600 s, set `deadlineSeconds` accordingly
- the run is killed at 600 s → that is the ceiling, and `deadlineSeconds` should be ~540

The design works at either value — budget-aware degradation means it returns partial results
instead of dying — but the constant should be measured, not guessed.
