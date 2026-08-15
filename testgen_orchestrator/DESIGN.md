# AAVA Test Generation Orchestrator — Design

**Date:** 2026-08-14
**Status:** Approved for implementation planning
**Replaces:** workflows 161 + 163, tool 76, and the model-driven self-heal loop

---

## 1. Problem

The current pipeline is two workflows and a self-heal loop driven by an LLM calling a
tool. It works, but every serious defect found in it traces to the same two causes:

- **State travels through payloads.** Scenarios, feedback and the round counter are passed
  between executions as workflow variables, so they can fail to bind, be paraphrased, or be
  dropped.
- **Control flow is a model's decision.** Whether to re-trigger, how many times, and with
  what arguments is decided by an LLM each run.

Concretely, this has produced: placeholders that never bound (`tsInputJson_string_true` read
as literal text for weeks), an agent instructed to copy 100 KB byte-for-byte, a round counter
that could reset silently, a reviewer echoing the generator's whole table until the gateway
severed the connection, an unexplained double execution per trigger, and a PAT sitting in
plaintext in exported logs.

It is also **unbatched**: one execution generates test cases for every scenario at once, so a
single response reaches ~124,000 characters, which reliably fails.

## 2. Goals

1. One entry point taking a single structured input object.
2. Test case generation **batched per scenario**, run **in parallel**.
3. Self-healing **per scenario**, independent, bounded.
4. A failing scenario must **never** fail the run.
5. Per-scenario scores surfaced, including runs that never reached the pass mark.
6. Everything logged, with timestamps the platform does not provide.

### Non-goals

- Choosing a destination for the finished test cases. Output stays structured in the result
  and the log; the destination is a later decision.
- Cross-batch LLM review. The cross-batch check is deterministic Python (§8).

## 3. Architecture

One agent, one tool. The agent's only job is to pass `{{inputs}}` to the tool.

```
Orchestrator Agent  →  AavaTestGenOrchestrator (tool)
                          │
   ┌──────────────────────┴───────────────────────────────────────┐
   │  1  validate inputs, resolve secrets                          │
   │  2  fetch ADO story .................... REST                 │
   │  3  generate scenarios ................. /agents/execute      │
   │                                                               │
   │  4  ThreadPoolExecutor(max_workers)                           │
   │       one thread per scenario, each running:                  │
   │           generate test cases ........... /agents/execute     │
   │           review ........................ /agents/execute     │
   │           while not passed and rounds < max: regenerate       │
   │                                                               │
   │  5  cross-batch check .................. Python               │
   │  6  assemble, log, return                                     │
   └───────────────────────────────────────────────────────────────┘
```

Agents 613 (scenarios), 564 (test cases) and 559 (reviewer) are retained as the bodies the
orchestrator calls. Their prompts lose all plumbing — no `{{ }}` variables, no tool-call
instructions, no verbatim-copy rules, no round numbers. The judging and generation logic is
unchanged.

## 4. Input contract

A single object, `{{inputs}}`, validated before any work begins.

```json
{
  "ado": {
    "org": "CSGRP",
    "project": "ADO",
    "storyId": "640764",
    "workItemType": "User Story",
    "areaPath": "ADO\\Products and Services\\EDI\\jEDI Warriors"
  },
  "agents": {
    "scenarioGenerator": 613,
    "testCaseGenerator": 564,
    "reviewer": 559
  },
  "run": {
    "maxScenarios": 5,
    "testCasesPerScenario": 3,
    "stepsMin": 15,
    "stepsMax": 18,
    "maxHealRounds": 3,
    "passScore": 90,
    "maxWorkers": 5,
    "stopOnStagnation": true
  },
  "budget": {
    "deadlineSeconds": 3000,
    "maxAgentCalls": 60
  },
  "secrets": {
    "adoPat": "",
    "aavaToken": ""
  }
}
```

**Agent ids are configuration.** Swapping a subagent is a number change, not a code change.

**Secrets resolve `AVASecret` first**; the `secrets` block is a local-development fallback
only. Secret values are scrubbed from every log line and are never placed in any agent's
`userInputs`. This removes the PAT and JWT from prompt context entirely.

**Validation.** Missing or malformed required fields fail fast with a named error before any
network call. Numeric ranges are clamped: `maxWorkers` 1–10, `maxHealRounds` 0–5,
`deadlineSeconds` 60–3600.

## 5. Data contracts between steps

The central improvement over the workflow design: **every handoff is parsed and validated in
Python before the next call is made.** Malformed agent output is caught at the boundary and
retried, rather than flowing downstream as text.

### 5.1 ADO → scenario generator

The ADO REST response is reduced to a validated dict before use:

```python
{ "id": int, "title": str, "description": str, "acceptanceCriteria": str }
```

`description` and `acceptanceCriteria` arrive as HTML and are converted to plain text here,
once, in Python — not by an agent.

**Validation:** `title` non-empty, and at least one of `description` / `acceptanceCriteria`
non-empty. Failure aborts the run with a clear reason; there is nothing to generate from.

### 5.2 Scenario generator → test case generator

Agent 613 returns a JSON array. It is parsed, and **each object validated field by field**:

```python
REQUIRED = ["scenarioId", "title", "descriptionRef", "acceptanceCriteriaRef",
            "dorRef", "dodRef", "type", "description", "priority"]
```

- `scenarioId` matches `^TS_\d+$`, unique across the array
- `type` in `{Positive, Negative, Edge}`
- `priority` in `{High, Medium, Low}`
- `dorRef` / `dodRef` may be empty strings — the story has no Definition of Ready, and that
  is expected, not a defect

**On failure:** retry the scenario-generation call up to 2 further times with the parse error
appended to the prompt. If it still fails, abort — there is nothing to batch.

Scenarios are then **truncated to `maxScenarios`**, highest `priority` first.

### 5.3 Test case generator → reviewer

Agent 564 receives **exactly one scenario object** plus the story context, and returns a
markdown table. The orchestrator parses it into rows and validates:

- 13 columns, header matching the expected list exactly
- every row has the same column count
- at least one `Id` matching `^TC[_-]?\w*\d+$`
- `Status` in `{Positive, Negative, Edge}`
- `Test Case Type` in `{Functional, Regression}`
- steps per test case within `[stepsMin, stepsMax]`
- test case count equals `testCasesPerScenario` (or fewer, with a reason)

**On parse failure:** counts as a failed round and feeds the parse error back as the
regeneration reason. It does not crash the thread.

### 5.4 Reviewer → orchestrator

Agent 559 receives the parsed table and the one scenario, and returns:

```json
{
  "scenarioId": "TS_001",
  "scores": [ { "id": "TC_001", "score": 92, "pass": true,  "gaps": [] },
              { "id": "TC_002", "score": 78, "pass": false, "gaps": ["step 7 has no expected result"] } ],
  "batchScore": 85,
  "batchPass": false
}
```

**Validation:** `scores` non-empty; every `id` present in the generated table; `score` an
integer 0–100. A malformed verdict is retried once, then treated as a failed round with the
reason recorded.

**No verbatim copying anywhere.** The orchestrator already holds the scenarios and the table
in memory; nothing is asked to reproduce them.

## 6. Execution mechanism

Agents are invoked with `POST /agents/execute` — a **synchronous** call that returns the
agent's answer in the response body:

```python
body    = {"agentId": int, "executionId": str(uuid4()), "user": principal, "userInputs": {...}}
headers = {"Authorization": f"Bearer {token}", "x-realm-id": realm}
# answer at: data.agentResponse.agent.output
```

**No polling is required.** Polling belongs to the workflow path
(`POST /workflows/workflow-executions` → `execution_id` → `GET .../result`), which this
design does not use.

**Retry policy:** 3 attempts per call with exponential backoff (2 s, 4 s, 8 s) on HTTP 0,
403, 429, 500, 502, 503, 504. HTTP 0 means the server dropped a long request; it is retried
like any other transient failure.

**Per-call timeout:** 600 s on the HTTP request. Note that `aava_selfheal_bugfixer` records a
practical wall near 265 s for large payloads on this endpoint; keeping each batch small
(§9) is what keeps calls well under it.

**Polling fallback (documented, not built).** If direct execution proves unreliable for long
calls, `_exec_agent()` is the single seam to change: trigger a one-agent workflow and poll
`GET /workflows/workflow-executions/{id}/result` every 20 s, capped at 40 attempts. Nothing
else in the design moves.

## 7. Threading, self-healing and scoring

### 7.1 Threading

`ThreadPoolExecutor(max_workers=run.maxWorkers)`, one task per scenario, collected with
`as_completed`. Each task is fully self-contained: generate, review, heal, return a record.

Timing, with N scenarios:

```
sequential:  T = fetch + scenarios + N × (gen + review) × (1 + rounds)
parallel:    T = fetch + scenarios +     (gen + review) × (1 + rounds_worst)
```

Parallelism removes N from the equation. At 5 scenarios this is roughly 5× faster and is what
makes healing affordable inside the execution ceiling.

### 7.2 Self-healing

Per thread, in Python:

```
round = 1
generate all test cases for this scenario
review
while not batchPass and round < maxHealRounds and budget.allows():
    round += 1
    regenerate ONLY the test cases whose `pass` is false, with their `gaps` as the reason
    review again
    if stopOnStagnation and score <= previous score:
        mark stagnant, stop
```

The round counter is a **local variable**. It cannot fail to bind, cannot reset, and is not
visible to any model.

Regeneration is **targeted** — only failing test cases are rebuilt, not the whole batch. This
is possible because the reviewer scores per test case.

### 7.3 Terminal states

| Status | Meaning | Test cases kept? |
|---|---|---|
| `approved` | score ≥ `passScore` | yes |
| `unhealed` | rounds exhausted, still below the mark | **yes, flagged** |
| `stagnant` | score did not improve between rounds; healing stopped early | **yes, flagged** |
| `failed` | agent call or parse failed after retries | whatever was produced |
| `skipped` | budget exhausted before this scenario began | none |

**Score history is recorded, not just the final score:**

```
TS_001  approved   [72 → 91]        2 rounds   3 test cases
TS_002  approved   [93]             1 round    3 test cases
TS_003  unhealed   [68 → 74 → 76]   3 rounds   3 test cases
TS_004  stagnant   [71 → 71]        2 rounds   3 test cases
TS_005  failed     [—]              1 round    0 test cases
```

Output below the pass mark is **kept and labelled**, never discarded. A 76 is worth a human's
review; throwing it away is worse than shipping it marked.

## 8. Failure isolation

**No thread can fail the run.** Every task body is wrapped; an exception becomes a record with
`status: "failed"` and the error text. `as_completed` gathers whatever comes back.

The run returns `status: "completed"` whenever the orchestrator itself completed, regardless
of how many scenarios succeeded. Only a failure in validation, the ADO fetch, or scenario
generation aborts — because in those cases there is nothing to batch.

**Cross-batch check**, after all threads finish, in Python — no LLM, no payload:

- duplicate test cases across scenarios (normalised name + description comparison)
- scenarios with zero test cases
- Positive / Negative / Edge balance across the whole set
- total test case count against expectation

Findings are reported as warnings; they never fail the run.

## 9. Budget

A thread-safe `_Budget(deadlineSeconds, maxAgentCalls)`, checked before every agent call.

- **Wall clock:** when remaining time falls below one round's estimated cost, healing stops
  and scenarios not yet started are marked `skipped`.
- **Call cap:** a hard ceiling on total `/agents/execute` calls, so a pathological heal loop
  cannot run away.

The failure mode becomes **partial output with an explanation**, replacing today's "timed out,
produced nothing".

Sizing: the platform ceiling observed on a 4-agent pipeline is 600 s, while agents are
configured `maxExecutionTime: 3600`. Whether a single-agent workflow is also cut at 600 s is
**unverified** — `probe/AavaExecutionTimeoutProbe.py` exists to measure it. `deadlineSeconds`
should be set to the measured ceiling minus a 10 % margin.

## 10. Output

```json
{
  "status": "completed",
  "story": { "id": "640764", "title": "..." },
  "summary": {
    "scenarios": 5, "approved": 2, "unhealed": 1, "stagnant": 1, "failed": 1, "skipped": 0,
    "testCases": 12, "totalRounds": 8, "elapsedMs": 352000,
    "agentCalls": 19, "tokensIn": 0, "tokensOut": 0, "costUsd": 0
  },
  "scenarios": [
    { "scenarioId": "TS_001", "title": "...", "status": "approved",
      "scoreHistory": [72, 91], "finalScore": 91, "rounds": 2,
      "testCaseCount": 3, "chars": 69390, "elapsedMs": 430000, "gaps": [] }
  ],
  "warnings": [ "TC_004 and TC_009 appear to be duplicates" ],
  "testcases": "<assembled 13-column markdown table>",
  "log": [ "[ORCH] ...", "[ORCH] ..." ]
}
```

Token, cost and time per call come from `GET /analytics/execution/observations?traceId=`,
following the pattern in `AavaCodeDocumentationOrchestrator.fetch_usage_stats()`. Fields are
zero when analytics are unavailable; they never block a run.

## 11. Logging

One `[ORCH]` line per event, appended under a lock, each carrying a UTC timestamp because the
platform activity log has none.

```
[ORCH] ts=2026-08-14T09:00:02Z step=fetch     story=640764 ms=1840
[ORCH] ts=2026-08-14T09:02:02Z step=scenarios count=5 chars=4212 ms=118000
[ORCH] ts=2026-08-14T09:05:23Z step=generate  scenario=TS_003 round=1 tc=3 chars=69390 ms=201000
[ORCH] ts=2026-08-14T09:05:53Z step=review    scenario=TS_003 round=1 score=68 pass=1/3 failing=TC_002,TC_003
[ORCH] ts=2026-08-14T09:07:27Z step=generate  scenario=TS_003 round=2 regen=2 ms=94000
[ORCH] ts=2026-08-14T09:07:57Z step=review    scenario=TS_003 round=2 score=74 pass=2/3
[ORCH] ts=2026-08-14T09:09:31Z step=result    scenario=TS_003 status=unhealed scores=[68,74,76] rounds=3
[ORCH] ts=2026-08-14T09:09:32Z step=done      approved=2 unhealed=1 stagnant=1 failed=1 ms=352000
```

`grep "step=result"` gives the per-scenario scoreboard. Secret values never appear.

## 12. What is retired

| Retired | Because |
|---|---|
| Workflow 163 | healing is a Python loop |
| Tool 76 | no re-triggering |
| `{{tsInputJson_string_true}}`, `{{rvwFeedbackTxt_string_false}}`, `{{roundNo_string_false}}` | state lives in memory |
| Verbatim-copy instructions in agent 559 | nothing is copied |
| Round-number passing | a local variable |
| Agent 367 + tool 2 | ADO fetch is a REST call, not an LLM step |

## 13. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Execution ceiling is 600 s, not 3600 s | **high** | budget-aware degradation; measure with the probe before setting `deadlineSeconds` |
| `/agents/execute` throttles under concurrency | medium | `maxWorkers` is configurable; start at 3 and measure |
| Practical ~265 s wall on `/agents/execute` | medium | batches sized ~69 KB so calls return well inside it; retry on HTTP 0 |
| `maxRpm: 20` per agent limits parallelism | medium | keep `maxWorkers` ≤ 5; observe throttling in the log |
| One tool is a single point of failure | medium | local runner (`run_local.py`) for testing before deploy |
| Loss of the canvas view | low | accepted; the log replaces it |

## 14. Testing

- **Local runner** (`run_local.py`) executing the tool against the real API from a developer
  machine, following `cg_run_local.py`. This is the main iteration loop and the largest
  practical gain over the workflow design.
- **Contract tests** for each parser in §5, using recorded agent outputs from the existing
  activity logs — including known-malformed ones.
- **Failure injection:** force a thread to raise and assert the run still returns
  `status: "completed"` with that scenario marked `failed`.
- **Budget test:** set `deadlineSeconds` low and assert scenarios are marked `skipped` rather
  than the run aborting.

## 15. Open questions

1. **What is the real execution ceiling for a single-agent workflow?** Blocks the
   `deadlineSeconds` value. Measured by the probe.
2. **Does `/agents/execute` serialise concurrent calls from one caller?** If it does,
   threading degrades to sequential and `maxScenarios` must drop.
3. **Where do finished test cases eventually go?** Deferred by decision; the output envelope
   is designed so a destination step can be added without touching anything else.
