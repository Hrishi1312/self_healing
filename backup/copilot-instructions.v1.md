# Self-Healing Agent Workflows — Project Design

This workspace implements a **self-healing test-generation system** for EDI 834 inbound
processing, built on the AAVA agent platform. Two agent workflows cooperate through a
confidence-driven feedback loop that can automatically re-trigger generation when the
reviewer is not confident in the output.

## Workflows

### Workflow 1 — Full generation pipeline (`workflow1/`)
The complete pipeline that generates test artifacts from scratch. Four agents run in
sequence:
1. **ADO Fetcher** — [`workflow1/ADO_fetcher.txt`](../workflow1/ADO_fetcher.txt) — pulls
   requirements / work items from Azure DevOps.
2. **Test Scenario Gen** — [`workflow1/834_inbound_scenario_gen.txt`](../workflow1/834_inbound_scenario_gen.txt)
   — generates test scenarios.
3. **Test Case Gen** — [`workflow1/834_inbound_testcase_gen.txt`](../workflow1/834_inbound_testcase_gen.txt)
   — generates test cases from the scenarios.
4. **Reviewer** — [`workflow1/reviewer.txt`](../workflow1/reviewer.txt) — reviews the
   output, produces a **confidence score** and **feedback**. The re-execution **tool is
   attached to this agent**.

### Workflow 2 — Self-healing re-execution pipeline (`workflow2/`)
The leaner pipeline that Workflow 1's reviewer re-triggers to repair/regenerate output.
Two agents:
1. **Test Case Gen** — [`workflow2/834_inbound_testcase_gen.txt`](../workflow2/834_inbound_testcase_gen.txt)
2. **Reviewer** — [`workflow2/reviewer.txt`](../workflow2/reviewer.txt)

### Shared agents across workflows (ALWAYS KEEP IN SYNC)
Workflow 1's agents **3 and 4** are the **same agents** as Workflow 2's agents **1 and 2**:
- `workflow1/834_inbound_testcase_gen.txt` === `workflow2/834_inbound_testcase_gen.txt`
- `workflow1/reviewer.txt` === `workflow2/reviewer.txt`

The prompts are intentionally duplicated across both folders **only for clarity and
separation** of each workflow. They represent the same underlying agent. **If one prompt
is changed, the matching prompt in the other workflow MUST be updated identically so they
never drift apart.**

## The self-healing tool

`RestApiFormDataCaller` is attached to the **Reviewer** agent.
- Implementation: [`wf_caller_tool.py`](../wf_caller_tool.py) (crewai `BaseTool`, used inside AAVA).
- Standalone runnable copy: [`wf_caller_run.py`](../wf_caller_run.py) (no crewai/AVASecret
  deps; run with `python wf_caller_run.py`, supports `DRY_RUN`).

### Inputs
- `form_data`: `pipelineId` (int) + `userInputs` dict with `tsInputJson`, `rvwFeedbackTxt`
  (and optional `reviewinputs`). The agent must **not** send `priority`.
- `confidence_score`: reviewer confidence, 0–100.

### Confidence logic
- `score <= 30` → **abort**: treated as no valid reviewer confidence; workflow is NOT triggered.
- `score >= 80` (`_CONFIDENCE_THRESHOLD`) → **no-op**: output is good, no re-execution.
- `30 < score < 80` → **re-trigger**: POST to the AAVA workflow API to re-run the workflow
  (self-healing loop).

### Behavior notes
- `priority` is always hardcoded to `1` (`_PRIORITY`); it is never taken from the agent.
- AAVA substitutes `{{var}}` tokens in prompts, so the reviewer's `userInputs` keys are
  re-keyed to the workflow variable names: `tsInputJson_string_true`,
  `rvwFeedbackTxt_string_false`, `reviewinputs`.
- All form-data values are normalized to strings; nested dicts/lists are JSON-serialized.

## Conventions & guardrails (ALWAYS ENFORCE)
- **PAT handling is intentional and stays this way — do NOT "fix" it:**
  - `wf_caller_run.py` (standalone) reads the PAT from a `.env` file via the `AAVA_TOKEN`
    environment variable.
  - `wf_caller_tool.py` (container / AAVA) keeps the PAT **hardcoded** in the script.
  - This split is by design. Do not move the hardcoded token out of the tool script and do
    not flag it as an issue.
- **Keep the two caller scripts in sync at all times:** `wf_caller_tool.py` (AAVA) and
  `wf_caller_run.py` (standalone) must share identical confidence logic, thresholds, payload
  mapping, and `userInputs` re-keying. If one changes, update the other in the same edit —
  the only allowed difference is the PAT handling described above.
- **Keep duplicated agent prompts in sync:** the Test Case Gen and Reviewer prompts exist in
  both `workflow1/` and `workflow2/` and must stay identical (see "Shared agents" above).
- Agent prompt files are plain `.txt` and are the source of truth for each agent's behavior.
