# Contracts

The things that must agree across files. Each has broken at least once, and each failure was
silent — the run completed and produced something plausible but wrong.

## 1. Variable binding — the `{{ }}` rule

AAVA substitutes a value only where it sees a placeholder wrapped in double braces, spelled
character-for-character. The suffix is not decoration: `tsInputJson` + `_string` (type) +
`_true` (required) is how the platform knows what the variable is.

| Placeholder | Declared in | Supplied by |
|---|---|---|
| `{{tsInputJson_string_true}}` | agent 3, `### INPUT DATA` | tool 76 |
| `{{rvwFeedbackTxt_string_false}}` | agent 3, `### INPUT DATA` | tool 76 |
| `{{roundNo_string_false}}` | agent 559, `# Input` and `# Tool Call` | tool 76 |
| `{{pat_token_false_true}}` | agent 559, `# Input` and `# Tool Call` | workflow input |

**Failure mode:** without braces the agent receives the literal variable name as text.
Agent 3 ran this way for weeks — the scenarios only reached it through the conversational
handoff from agent 2, which masked the bug in workflow 161 and would have starved it
completely in workflow 163, where it is the entry point.

**How to check:** the activity log prints each agent's live task prompt. Real data means it
bound; the variable's own name means it did not.

## 2. The generator → reviewer handoff

Agent 3 must emit an object with **exactly two fields**:

```json
{ "testcases": "<13-column markdown table>", "scenariojson": <the input array, verbatim> }
```

The reviewer's first instruction is *"Parse it first. It has exactly two fields."* It then
copies `scenariojson` into the tool call as `tsInputJson`.

**Failure modes:**
- Drop `scenariojson` → the rework trigger posts an empty payload.
- Emit anything outside the object — a `## Changes Made This Round` heading, a preamble —
  → the reviewer cannot parse it, scores 0, and the tool aborts.

Agent 3's FEEDBACK HANDLING explicitly forbids a changelog for this reason.

## 3. The 13-column header

Must be byte-identical in agent 3's sample and agent 559's input description:

```
ScenarioId | AcceptanceCriteriaRef | Name | Id | Attachments | Status | Test Case Type |
Description | Precondition | Test Step # | Test Step Description |
Test Step Expected Result | Test Step Attachment
```

**Failure mode:** agent 559 once carried a stale 11-column list and reported `ScenarioId`
and `AcceptanceCriteriaRef` as "extra columns beyond the expected schema" — penalising the
generator for following its own instructions.

## 4. Column semantics

| Column | Holds | Does not hold |
|---|---|---|
| `Status` | Positive, Negative, Edge | Draft, New, Approved |
| `Test Case Type` | Functional, Regression | Positive/Negative/Edge |

This matches the existing manual test cases, so generated rows can be imported alongside
them. The generator previously wrote `Draft` into `Status` and the classification into
`Test Case Type` — the two are swapped relative to the house template and it is an easy
mistake to reintroduce. Agent 559 check 9 enforces it.

## 5. Volume numbers

Written in **three** places and all three must agree:

| Setting | agent 3 volume section | agent 3 validation step 8 | agent 559 check 8 |
|---|---|---|---|
| cases per scenario | 3 (2 allowed) | 3 | fails at 4+ |
| steps per case | 15–20 | 15–20 | fails <15 or >20 |
| total cases | max 20 | 20 | fails >20 |

**Failure mode:** if the generator is told one limit and the gate enforces another, every
compliant run is rejected and the loop burns all three rounds.

## 6. The confidence threshold

Lives in two files:

- `tool/tool76_rest_api_form_data_caller.py` → `_CONFIDENCE_THRESHOLD = 90`
- `agents/agent559_reviewer_llm_judge.txt` → `approved: true if confidence >= 90`

Change both or neither. It was 97 against a rubric whose top band starts at 90, which meant
sound output scored 92 and re-queued forever.

## 7. Angle-bracket tokens

| Token names… | Verdict |
|---|---|
| runtime test data — `<ISA13>`, `<InterchangeID>`, `<member_ssn>`, `<sbsb_ck>`, `<YYYYMMDD>` | **allowed** |
| anything resolvable — `<STATE>`, `<applicable state>`, `<table_name>`, `<column_name>` | **forbidden** |

The rule: if the answer exists in the inputs, resolve it; if it only exists in the tester's
environment at run time, leave it in brackets. This mirrors the manual test cases, which use
`<ISA13>` 21 times and `<InterchangeID>` 42 times.

A blanket ban was tried and was wrong — it blocked approval for two rounds and is stricter
than the human standard.

## 8. Hard gates must be literal

Checks 5–10 in agent 559 are string tests and numeric counts, never judgements about
phrasing. Each carries an evidence rule: *a gate fails only if you can quote the exact
offending substring; if you cannot quote it, the check passes.*

**Failure mode:** written as prose judgements, the reviewer invented violations — reporting
a NAS path as a "schema document name", claiming `per the AC` appeared when it occurred zero
times, and calling 8 test cases across 4 scenarios a breach of "no more than 2 per scenario".
The score pinned at exactly 85 (the cap value) on every run and nothing could ever approve.
