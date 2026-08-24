# testsenarion_selfhealing — the two-stage split

The pipeline from `testgen_orchestrator/`, divided into **two orchestrator agents** connected
by a **GitHub handoff file**, so each stage gets its own 500-second execution window and the
scenario list gets its own self-healing review before the expensive test-case stage spends
anything on it.

```
Agent A (stage=scenarios)                    Agent B (stage=testcases)
fetch story (ADO REST)                       read the handoff file (no ADO call)
-> generate scenarios (agent 625)            -> one thread per scenario:
-> scenario review (agent 04, new)              generate (626) -> review (627) -> heal
   rejected? one rework with feedback        -> assemble the 13-column table
-> write scenarios/<storyid>/scenarios.json  -> publish run.log, testcases.md,
   to GitHub                                    envelope.json, runinputs.json
```

The handoff file's path is derived from the story id alone, so Agent B needs nothing from
Agent A — no workflow variable ever carries scenario data, and no model ever relays it. Rerun
Agent B as often as you like against the same approved scenarios; edit the file by hand
between stages if you want a human gate.

## What lives here vs what is reused

**This folder is the successor: its tool is the one that evolves.**
`testgen_orchestrator/` is the frozen in-production working copy — do not patch it.

| Here (new or modified) | |
|---|---|
| `agents/04_scenario_reviewer_agent.md` | The one NEW console agent: judges the scenario list (coverage, redundancy both directions, traceability), returns confidence/approved/feedback/gaps |
| `tool/AavaTestGenOrchestratorTwoStage.py` | The working-copy tool plus: `stage` key (`all`/`scenarios`/`testcases`), scenario review with one rework, the handoff write/read, deadline default 450 (500s ceiling − margin). Renamed from `AavaTestGenOrchestrator.py` — the two tools share a class name and platform display name otherwise. |
| `tool/run_local.py` | Plus `--stage` and `--scenarioreviewagent` |
| `tool/test_orchestrator.py` | All working-copy checks plus section 18 (stages, handoff, new prompt wiring) — 218 checks |
| `tool/.env.example` | Plus `stage`, `scenarioreviewagentid`, `scenariopassscore` |

| Reused as-is from `testgen_orchestrator/` — referenced, never copied | |
|---|---|
| `agents/00_orchestrator_agent.md` | The thin pass-through entry prompt. Paste it into BOTH console agents (A and B), just under two names |
| `agents/01_scenario_generator_agent.md` | Sub agent 625, called by stage `scenarios`/`all` |
| `agents/02_test_case_generator_agent.md` | Sub agent 626, called by stage `testcases`/`all` |
| `agents/03_reviewer_agent.md` | Sub agent 627, called by stage `testcases`/`all` |
| `tool/fixtures/` | Real recorded agent output; this folder's test suite reads it by relative path |

Console agents 625/626/627 need no redeployment.

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
 "scenarioagentid": 625, "scenarioreviewagentid": <new agent id>, "maxscenarios": 7,
 "githubtoken": "..."}

// Agent B
{"stage": "testcases", "adostoryid": "640764",
 "testcaseagentid": 626, "reviewagentid": 627, "githubtoken": "..."}
```

`githubtoken` is required for both stages — the handoff is the interface, not optional
publishing. Stage `testcases` needs no ADO credential at all. `maxscenarios`, workers and the
agent-call budget for stage `testcases` all come from the handoff file.

## Deployment

1. Console: create the scenario reviewer agent from `agents/04_scenario_reviewer_agent.md`;
   note its id.
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

218 checks, exit 0. Local live runs:

```
python3 testsenarion_selfhealing/tool/run_local.py 640764 --stage scenarios --scenarioreviewagent <id>
python3 testsenarion_selfhealing/tool/run_local.py 640764 --stage testcases
```
