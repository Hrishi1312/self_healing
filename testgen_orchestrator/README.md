# testgen_orchestrator

## Executive summary

**What it is.** One agent and one tool that turn an Azure DevOps user story into EDI 834
inbound test cases, replacing the two workflow pipeline in the repo root.

**Why.** Every serious defect in the current pipeline came from two causes: state travelling
through payloads, and control flow being a model's decision. That produced placeholders that
never bound, an agent asked to copy 100 KB byte for byte, a round counter that could silently
reset, a reviewer echoing the whole table until the gateway cut the connection, an
unexplained double execution per trigger, and a credential in plaintext in exported logs.
This design removes the causes rather than patching the symptoms.

**How it works.** Fetch the story over REST, generate scenarios, then run one thread per
scenario. Each thread generates its test cases, has an independent reviewer score every one,
and repairs only the failures, up to three rounds. Threads are isolated: a scenario that
fails is reported in the result, it does not fail the run.

**What it buys.**

| | Two workflow pipeline | This |
|---|---|---|
| Batching | none, one response for every scenario | one execution per scenario |
| Self healing | shared, model driven | per scenario, a Python loop |
| Largest single response | 124,664 chars through the gateway, which has failed | ~226,000 measured on the direct path, delivered cleanly |
| A scenario failing | the whole story produces nothing | the other scenarios are unaffected |
| Credentials | agent inputs, so they reach the logs | resolved in Python, never in a prompt |
| Timing data | none, the platform logs no timestamps | every line stamps its own UTC |
| Local testing | not possible | run it from a developer machine |

**Status: in active use against the real platform via `tool/run_local.py`** — `tool_logs/`
in the repo root holds the run history (story 640764 and others, Aug 2026). The offline suite
(`tool/test_orchestrator.py`, 227 checks) covers the tool; `--probe` checks `{{variable}}`
binding before anything else is attempted. `DESIGN.md` carries the original rationale and,
in §14, what has changed since it was approved.

**The binding constraint is time.** The client fronts AAVA with Azure Container Apps, which
severs a request at **240 seconds** — that governs any run launched *inside* ACA
(`deadlineseconds` defaults to 190 for that case). Runs launched from a developer machine are
not under that ceiling and use 630s. Measured at v2.0 volume (7 scenarios × up to 10 cases ×
12–20 uniform-depth steps), a full 3-round run took **525s** — fine locally, far over the ACA
budget. The front-loaded step shape (one deep primary case, ~7-step variants; see agent 02)
was adopted from the domain experts' reference output partly to close that gap; the fit under
240s is still unproven.

**Cost.** Roughly N+1 agent calls per story rather than 2, traded for capacity and isolation.

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
run in parallel, each healing independently. That is what makes v2.0 volume (7 scenarios ×
up to 10 test cases each) possible: the story's ~950K characters of output never travel
through one completion. The largest single completion observed on the direct
`/agents/execute` path is ~226K characters, delivered cleanly — the ~124K failures belonged
to the old pipeline's gateway round-trip.

## Layout

```
DESIGN.md     the full design — read this first
agents/       prompts for the orchestrator and the three subagents
tool/         AavaTestGenOrchestrator.py and its local runner
```

## What changes for the subagents

The live console agents are **654 (scenarios), 652 (test cases) and 653 (reviewer)**, carrying
the prompts in `agents/`. They descend from the root pipeline's 613/564/559, minus the
plumbing (the prompts have since diverged further — the v2.0 output contract, DESIGN.md §14):

| Removed from the prompts | Because |
|---|---|
| `{{tsInputJson_string_true}}` and friends | state is passed as a function argument |
| "copy `scenariojson` verbatim, byte-for-byte" | the orchestrator already holds it |
| tool-call instructions and argument shapes | no agent calls a tool |
| round-number passing | it is a local variable |

The test case generator gains one thing: it receives **one scenario**, not an array.

## Retired when this ships

Workflow 163, tool 76, agent 367 with tool 2 (the ADO fetch becomes a REST call), and every
`{{ }}` variable in the chain. See `DESIGN.md` §12.

## Open number

The AAVA-hosted execution ceiling for a single-agent workflow is still unmeasured (observed:
600 s on a 4-agent pipeline). It only matters if the tool is ever run *on* the platform rather
than from a machine — local runs to the 630 s deadline complete fine (525 s observed).
`../probe/AavaExecutionTimeoutProbe.py` measures it if that deployment happens. Budget-aware
degradation means the tool returns partial results at either value rather than dying, but the
constant should be measured, not guessed. Inside the client's ACA, 240 s governs regardless.
