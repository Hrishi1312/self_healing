# Architecture

## The two workflows

**Workflow 161 — initial pass**, four agents in sequence (`serial` 1→4, `allowDelegation:
false` on all, no manager LLM, so it is strictly sequential):

1. **ADO Story Fetcher** (agent 367) — calls tool 2 to pull the user story from Azure
   DevOps and passes it through unchanged.
2. **Test Scenario Generator** (agent 613, `test 6 EDI 834 Inbound Test Scenario Generator
   Embed KB Clone`) — turns the story's description + acceptance criteria into scenarios.
   Note this is **not** the agent 366 that appears in older exports; 366 was replaced.
   Agent 613 carries **5 knowledge bases**.
3. **Test Case Generator** (agent 564) — expands each scenario into test cases.
4. **Reviewer / LLM-as-a-judge** (agent 559) — scores the output and calls tool 76.

**Workflow 163 — rework round**, two agents: the same 564 and 559. Agent 564 is the entry
point here, which is why its input variables matter so much.

## How the loop closes

Agent 559 calls tool 76 with the confidence score and the current round number. The tool
decides:

| Condition | Action |
|---|---|
| confidence ≤ 30 | abort — no valid verdict was produced |
| confidence ≥ 90 | stop — approved |
| round already at 3 | stop — escalate to a human |
| otherwise | `POST /workflows/workflow-executions` with `pipelineId 163` |

The POST carries the scenarios, the reviewer's feedback, and a round number the **tool**
stamps — never the model. That is what makes the count reliable.

Each rework round is a **separate execution**, not a nested call. The parent finishes and
closes; the child starts fresh. Nothing persists between them except the trigger payload,
and `enableAgenticMemory` is `false` on both workflows.

## Data flow

```
ADO story
   │
   ▼
Scenario Generator ──► JSON array, 9 fields per scenario
   │                   scenarioId, title, descriptionRef, acceptanceCriteriaRef,
   │                   dorRef, dodRef, type, description, priority
   ▼
Test Case Generator ─► { testcases: "<13-column markdown table>",
   │                     scenariojson: <the array, copied verbatim> }
   ▼
Reviewer ────────────► { confidence, approved, feedback, strengths, gaps, scenariojson }
   │                    and a tool call
   ▼
Tool 76 ─────────────► either nothing, or a new execution of workflow 163
```

## Knowledge bases

Attached to the test case generator:

- `kb_edi_834_testcase_analysis_1_embedded` — server and database details
- `kb_edi_schema_details_003_large` — schema; contains three documents,
  `EDI and FACETS Schema 2`, `Facets 834`, `EDIFECS Full with AUX 834`

The reviewer has **no knowledge base attached**. It can confirm a table name is concrete
but cannot confirm it is correct — schema accuracy remains a human check.

`kb_edi_834_companion_guide_1_embedded` was previously attached and is not any more. The
file-naming-convention instruction that depended on it was removed rather than left
pointing at a knowledge base the agent cannot open.

## Constraints that shape everything

- **600-second execution ceiling** enforced by the platform. Agent-level
  `maxExecutionTime: 3600` is not the operative limit.
- **Gateway severs long completions.** A single ~109 KB completion has been cut mid-flight
  with `RemoteDisconnected`. This is why the reviewer no longer echoes the test case table.
- **No timestamps in the activity log.** The `[AAVA-LOOP]` lines from tool 76 are the only
  wall-clock reference. For real per-step timings use
  `GET /analytics/execution/observations?traceId=<execution_id>`.
