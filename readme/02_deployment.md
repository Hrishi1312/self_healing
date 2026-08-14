# Deployment

Four artefacts. Three pastes and one dropdown. All are AAVA drafts (`status: CREATED`), so
they are editable in place — no clone-to-edit, no re-approval.

## Order

Deploy the tool first so the reviewer's tool call matches its new signature.

### 1. Tool 76 — `tool/tool76_rest_api_form_data_caller.py`

Console → Tools → **REST API Form Data Caller test5** → code editor → select all → delete →
paste the whole file → save.

Carries: threshold 90, 3-round cap, `timeout=(10, 30)`, `roundNo` stamping, `[AAVA-LOOP]`
logging. Exactly one `BaseTool` subclass, as AAVA requires.

### 2. Agent 2 — `agents/agent2_test_scenario_generator.txt`

Console → Agents → the scenario generator → instructions → select all → paste.

### 3. Agent 3 — `agents/agent3_test_case_generator.txt`

Console → Agents → **test 6 EDI 834 Inbound Test Case Generator From JSON Scenarios Embed
KB Feedback Clone** → instructions → select all → paste.

Confirm both knowledge bases are attached: `kb_edi_834_testcase_analysis_1_embedded` and
`kb_edi_schema_details_003_large`. Without the second one every schema instruction fails.

### 4. Agent 559 — `agents/agent559_reviewer_llm_judge.txt`

Console → Agents → **Test Case Reviewer LLM as a Judge Clone loop Test5** → instructions →
select all → paste.

### 5. Agent 559 config — `maxIter` → **2**

Not in any file. Two, not one: the reviewer needs one iteration to call the tool and one to
produce its verdict. At 1 it may answer without ever calling the tool, which kills the loop.

## Do not paste piecemeal

Every prompt here is a whole-file replacement. Merging sections by hand has twice produced a
broken prompt — once deleting the `{{pat_token_false_true}}` placeholder, once truncating a
sentence mid-word. Select all, delete, paste.

## Verify the first run

**1. Variables bound.** In the activity log, agent 3's task prompt under `### INPUT DATA`
must show real scenario JSON, not the literal text `tsInputJson_string_true`. Agent 559's
must show a real JWT and a digit where its placeholders sit.

**2. The loop decided.** Grep for `AAVA-LOOP`:

```
decision=APPROVED_STOP        confidence ≥ 90, no rework
decision=REWORK_TRIGGERED     round=N/3, child_execution_id present
decision=LIMIT_REACHED        three rounds done, escalate
decision=API_TIMEOUT          client gave up before the gateway
```

**3. The output is shaped right.**

- object has exactly `testcases` and `scenariojson`, nothing outside it
- table has all 13 columns
- `Status` holds Positive/Negative/Edge — **not** `Draft`
- `Test Case Type` holds Functional/Regression
- the trading partner is named in each test case
- no `<STATE>` / `<applicable state>` / `<table_name>` tokens; `<ISA13>`-style runtime data
  is expected and fine
- 3 test cases per scenario, 15–20 steps each

**4. No failures.** Zero `LLM call failed`, zero `Execution timed out after 600s`.

## If it times out

Current settings produce 180–240 step rows. The largest run that has completed cleanly was
80 rows. The cheapest rollback is one number:

`agents/agent2_test_scenario_generator.txt` → `SCENARIO VOLUME DISCIPLINE` → change
`MAXIMUM of 4 test scenarios` to `2`. That halves output while keeping the depth and the
column fixes.

Do not change the step range or cases-per-scenario without also changing agent 559's
check 8 — the gate enforces those numbers and will reject compliant output otherwise.
