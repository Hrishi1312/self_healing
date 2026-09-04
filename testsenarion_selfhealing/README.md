# testsenarion_selfhealing — the two-stage split

The pipeline from `testgen_orchestrator/`, divided into **two orchestrator agents** connected
by a **GitHub handoff file**, so each stage gets its own execution window and the scenario
list gets its own self-healing review before the expensive test-case stage spends anything
on it.

```
Agent A (stage=scenarios)                    Agent B (stage=testcases)
fetch story (ADO REST)                       read the handoff file (no ADO call)
-> generate scenarios (agent 688)            -> one thread per scenario:
-> scenario review (agent 704, new)             generate (689) -> review (672) -> heal
   rejected? one rework with feedback        -> assemble the 15-column table
-> write scenarios/<storyid>/scenarios.json  -> publish run.log, testcases.md,
   to GitHub                                    envelope.json, runinputs.json, testcases.xlsx
```

The handoff file's path is derived from the story id alone, so Agent B needs nothing from
Agent A — no workflow variable ever carries scenario data, and no model ever relays it. Rerun
Agent B as often as you like against the same approved scenarios; edit the file by hand
between stages if you want a human gate.

Splitting the run buys each stage its own request, **not a bigger one**. The client fronts
AAVA with Azure Container Apps, which severs a request at **240 seconds**, and that ceiling
is per request either way — `deadlineseconds` defaults to 190 so the tool stops on its own
terms and returns what it has. Runs launched from a developer machine are not under that
ceiling; `.env.example` ships 630 for them.

## What lives here vs what is reused

**The pipeline logic evolves in `testgen_orchestrator/`.** This folder carries the same logic
plus the stage split, and is re-synced from it — so a change to generation, healing, gating or
assembly belongs there first, and comes here afterwards. Only the rows in the first table
below are this folder's own.

| Here (new or modified) | |
|---|---|
| `agents/04_scenario_reviewer_agent.md` | The one NEW console agent: judges the scenario list (coverage, redundancy both directions, traceability), returns confidence/approved/feedback/gaps |
| `tool/AavaTestGenOrchestratorTwoStage.py` | The working-copy tool plus: `stage` key (`all`/`scenarios`/`testcases`), scenario review with one rework, the handoff write/read. Renamed from `AavaTestGenOrchestrator.py` — the two tools share a class name and platform display name otherwise. |
| `tool/run_local.py` | Plus `--stage` and `--scenarioreviewagent` |
| `tool/test_orchestrator.py` | All working-copy checks plus section 20 (stages, handoff, budget, new prompt wiring) — 263 checks |
| `tool/.env.example` | Plus `stage`, `scenarioreviewagentid`, `scenariopassscore` |

| Reused as-is from `testgen_orchestrator/` — referenced, never copied | |
|---|---|
| `agents/00_orchestrator_agent.md` | The thin pass-through entry prompt. Paste it into BOTH console agents (A and B), just under two names |
| `agents/01_scenario_generator_agent.md` | Sub agent 688, called by stage `scenarios`/`all` |
| `agents/02_test_case_generator_agent.md` | Sub agent 689, called by stage `testcases`/`all` |
| `agents/03_reviewer_agent.md` | Sub agent 672, called by stage `testcases`/`all` |
| `tool/fixtures/` | Real recorded agent output; this folder's test suite reads it by relative path |

Console agents 688/689/672 need no redeployment.

## Kept deliberately different from `testgen_orchestrator/`

Everything else in the two tools should be identical — anything not on this list that shows
up in `diff` is drift, not design, and should be reconciled toward `testgen_orchestrator/`:

- the module docstring, the class and schema names, and the platform display name
- `HANDOFF_DIR`, `_handoff_path`, `_gh_headers`, `write_handoff`, `fetch_handoff`
- `DEF_SCENARIOPASSSCORE` and `parse_scenario_verdict`
- in `_config`: the `stage` key, the per-stage `needed` credentials, the `agentid()` helper
  that makes each agent id required only for the stages that call it, `maxscenarios` being
  optional for `stage=testcases`, and the `extra` term in `maxagentcalls` that budgets for
  the scenario reviewer's two reviews plus one rework generate
- in `_run`: the handoff read that sizes the testcases stage, the story/scenario block being
  skipped for `stage=testcases`, the scenario review with its single rework, and the
  scenarios-stage envelope
- in `run_local.py`: `--stage`, `--scenarioreviewagent`, the ADO-credential exemption for the
  testcases stage, and the scenarios-stage summary
- in `test_orchestrator.py`: `SHARED`/`AGENTS_LOCAL` (fixtures and 00–03 are referenced, not
  copied) and section 20

## The handoff contract

`scenarios/<storyid>/scenarios.json` on the configured repo/branch, written by stage
`scenarios`, read by stage `testcases`:

```json
{
  "story":     {"storyid": "...", "title": "...", "description": "...", "acceptancecriteria": "..."},
  "scenarios": [ {"scenarioId": "TS_001", "title": "...", "...": "..."} ],
  "review":    {"confidence": 88, "approved": true, "feedback": "...", "gaps": [], "strengths": []},
  "writtenat": "2026-08-20T00:00:00Z"
}
```

The path is stable, so a rerun overwrites (the tool handles the contents-API sha dance). The
reader revalidates the scenario array with the same parser live generator output goes
through, so a hand-edited file gets the same scrutiny.

## Runinputs per stage

Everything from the working copy still applies (`stage=all` is byte-compatible). New keys:
`stage`, `scenarioreviewagentid` (0 = no scenario review), `scenariopassscore` (default 70,
passed to the reviewer inside `reviewinputs` — single source of truth).

Minimal payloads:

```json
// Agent A
{"stage": "scenarios", "adoorg": "CSGRP", "adoproject": "ADO", "adostoryid": "640764",
 "scenarioagentid": 688, "scenarioreviewagentid": 704, "maxscenarios": 7,
 "githubtoken": "..."}

// Agent B
{"stage": "testcases", "adostoryid": "640764",
 "testcaseagentid": 689, "reviewagentid": 672, "githubtoken": "..."}
```

`githubtoken` is required for both stages — the handoff is the interface, not optional
publishing. Stage `testcases` needs no ADO credential at all. `maxscenarios`, workers and the
agent-call budget for stage `testcases` all come from the handoff file.

## Deployment

1. Console: create the scenario reviewer agent from `agents/04_scenario_reviewer_agent.md`;
   note its id (704 today).
2. Console: create two agents from the existing `00_orchestrator_agent.md` prompt (e.g.
   `TestScenario SelfHealing` and `TestCase Generator`), attach this folder's tool to both.
3. Run Agent A with `stage=scenarios`, check
   `https://github.com/<repo>/blob/main/scenarios/<storyid>/scenarios.json`, then Agent B
   with `stage=testcases`.
4. Wire A → B in an AAVA workflow whenever ready — B only needs the story id, which A's
   runinputs already carry.

## Verify offline

```
python3 testsenarion_selfhealing/tool/test_orchestrator.py
```

263 checks. Two of them (`publish on PUTs exactly the five run files`, `the envelope reports
what was published`) fail on a machine without `openpyxl`, because the xlsx is then skipped
and only four files are pushed — `testgen_orchestrator/`'s suite fails the same two for the
same reason. Everything else must pass.

Local live runs:

```
python3 testsenarion_selfhealing/tool/run_local.py 640764 --stage scenarios
python3 testsenarion_selfhealing/tool/run_local.py 640764 --stage testcases
```

`--probe` checks `{{variable}}` binding by submitting a marker to the configured agent, so it
needs a reachable platform and a valid token — it is not an offline check.
