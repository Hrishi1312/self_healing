# Open items

## Untested — the current settings have never run

Volume was raised to 3 test cases per scenario at 15–20 steps in the same change as the
column and angle-bracket fixes. That produces **180–240 step rows**.

The largest run that has ever completed cleanly was **80 rows / 104 KB**, and a single
~109 KB completion has been severed by the gateway before. Current settings are roughly
**3× beyond proven territory**.

**If the first run times out:** agent 2, `SCENARIO VOLUME DISCIPLINE`, change `MAXIMUM of 4
test scenarios` to `2`. That gives 6 cases × 20 steps = 120 rows and keeps everything else.

## Not fixed

### The workflow executes twice per trigger

Two complete pipeline runs per trigger, evidenced by two different scenario sets in one log.
`maxIter: 2` rules out the reviewer. The trigger source is the remaining candidate and is
outside these files. The `child_execution_id` now in the `[AAVA-LOOP]` log line makes the
parent/child relationship visible, which should settle it.

### Credentials travel through the model

The ADO PAT and the AAVA JWT are agent input variables, so they land in prompt context and
therefore in exported activity logs. Fix is `AVASecret.getValue(...)` in the tool with the
argument as a dev-only fallback. Until then, exported logs contain live secrets.

### The reviewer cannot verify schema accuracy

Agent 559 has no knowledge base attached. It can confirm a table name is concrete, not that
it is correct. Attaching `kb_edi_schema_details_003_large` to the reviewer would close this
but adds retrieval time to every review.

### Coverage is well short of the manual standard

4 scenarios × 3 cases = 12 test cases against the manual's 21, and the story has 16
acceptance criteria and DoD items. Options, none yet chosen:

- raise agent 2's scenario cap toward 7 (one per AC) — blocked by the 600 s ceiling
- run the same story several times with the scenario generator focused on a different slice
- split generation per scenario so no single completion is large — a workflow change, not a
  prompt change

### File naming convention

The manual test cases use `ARPASSE_834I_TCnnn_YYYYMMDD.dat`. The instruction that produced
this came from `kb_edi_834_companion_guide_1_embedded`, which is no longer attached, so the
instruction was removed rather than left dangling. Re-attach the KB to restore it.

### SQL targets a different layer than the manual

The manual asserts the derived transaction effective date in `EDIStageArchive.dbo.*` —
`Interchange834`, `Dates834`, `MemberCoverage834`, `Crosswalk`, `MELC`. The generator queries
FACETS `CMC_*` tables. Both are real; only the manual asserts at the layer where the
effective date is derived.

### Preconditions

All 21 manual test cases share one 7-point environment checklist. The prompts still demand
scenario-specific preconditions and forbid "a generic phrase" — the opposite of the house
standard. Left alone deliberately; changing it was scoped as item D and not selected.

## Platform limitations, not fixable here

- **No timestamps in the activity log.** The `[AAVA-LOOP]` lines are the only wall-clock
  reference. If APS exposes a log verbosity or timestamp setting, that is the right lever.
- **600-second execution ceiling.** Agent-level `maxExecutionTime: 3600` is not the
  operative limit.
- **Duplicate event blocks** in the log — some are byte-identical logging artefacts, some
  are genuinely separate runs. Diff the blocks before concluding anything.

## Decisions taken, with the reasoning

| Decision | Chosen | Why |
|---|---|---|
| Columns | 13, not the 11 in the new guidelines | keeps reviewer traceability; the manual uses 11, so an export mapping may be needed later |
| Step floor | 15 | "max 20" was specified; without a floor the model regressed to 10 |
| Cases per scenario | 3, with 2 allowed | matches the manual's positive/edge/negative triad; the exception avoids forcing an invented negative case |
| Rework rounds | 3 | requested; AAVA's AQG norm is ~2 then escalate |
| `maxIter` | 2, not 1 | the reviewer needs one iteration to call the tool and one to answer |
| Threshold | 90 | matches the rubric's own "all basics pass" band |
