"""Run AavaTestGenOrchestrator from a developer machine, against the real platform.

This is the step between the offline suite and an upload. The offline suite proves the parsers
accept what the agents produce; this proves the agent ids exist, the credentials work, the
{{runinputs}} wiring binds, and the whole thing finishes inside the time budget.

    export AAVA_TOKEN=...      # bearer for /agents/execute
    export ADO_PAT=...         # Azure DevOps read PAT
    python3 testgen_orchestrator/tool/run_local.py 640764

    python3 testgen_orchestrator/tool/run_local.py 640764 --scenarios 2 --no-heal
    python3 testgen_orchestrator/tool/run_local.py 640764 --probe      # bind check, no run

Credentials come from the environment, never from a file in the repo. They are passed in
runinputs only because AVASecret does not exist off-platform; the tool still scrubs them from
every log line.
"""

import argparse
import json
import os
import sys
import time
import types
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The platform packages do not exist locally. Stub the one class the tool inherits from.
if "crewai" not in sys.modules:
    crewai, tools = types.ModuleType("crewai"), types.ModuleType("crewai.tools")

    class BaseTool:
        def __init__(self, **kw):
            pass

    tools.BaseTool = BaseTool
    crewai.tools = tools
    sys.modules["crewai"], sys.modules["crewai.tools"] = crewai, tools

import AavaTestGenOrchestrator as T      # noqa: E402


def probe(cfg):
    """One call to the scenario generator with a recognisable marker in the story text.

    AAVA binds a sub agent's input by substituting {{name}} into its Description. If that
    binding is broken the agent still answers, fluently, from its instructions alone, and every
    parser downstream accepts the result. This is the only way to tell the difference: put a
    string in the input that cannot come from anywhere else and look for it coming back.
    """
    marker = f"PROBE-{uuid.uuid4().hex[:8].upper()}"
    log = T._Log()
    inputs = {
        "storydata": T._j({"storyid": "0", "title": f"{marker} probe story",
                           "description": f"The system must handle {marker} correctly.",
                           "acceptancecriteria": f"AC1 - Given {marker}, Then it is echoed."}),
        "maxscenarios": "1",
    }
    print(f"probing agent {cfg['scenarioagentid']} with marker {marker} ...")
    budget = T._Budget(120, 2)
    try:
        out, ms = T.exec_agent(cfg["scenarioagentid"], inputs, cfg, cfg["aavatoken"],
                               budget, log, "probe")
    except Exception as e:
        print(f"FAILED after {budget.elapsed_ms()}ms: {e}")
        return 1
    hit = marker in out
    print(f"answered in {ms}ms, {len(out)} chars")
    print(f"marker {'FOUND' if hit else 'ABSENT'} in the response")
    if not hit:
        print("\n  The agent answered without the data it was given. Either {{storydata}} is\n"
              "  missing from its Description, or the name does not match character for\n"
              "  character. Nothing downstream can detect this: fix it before running.")
    print("\n--- first 400 chars ---\n" + out[:400])
    return 0 if hit else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("storyid")
    ap.add_argument("--org", default=os.environ.get("ADO_ORG", "CSGRP"))
    ap.add_argument("--project", default=os.environ.get("ADO_PROJECT", "ADO"))
    ap.add_argument("--scenarios", type=int, default=4)
    ap.add_argument("--cases", type=int, default=3)
    ap.add_argument("--stepsmin", type=int, default=15)
    ap.add_argument("--stepsmax", type=int, default=18)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--no-heal", action="store_true", help="single pass, no regeneration")
    ap.add_argument("--no-judge", action="store_true", help="pre gate only, no reviewer")
    ap.add_argument("--deadline", type=int, default=190)
    ap.add_argument("--scenarioagent", type=int, default=613)
    ap.add_argument("--testcaseagent", type=int, default=564)
    ap.add_argument("--reviewagent", type=int, default=559)
    ap.add_argument("--base", default="https://int-ai.aava.ai")
    ap.add_argument("--realm", default=os.environ.get("AAVA_REALM", "4"))
    ap.add_argument("--user", default=os.environ.get("AAVA_USER", T.DEF_USERPRINCIPAL))
    ap.add_argument("--probe", action="store_true", help="check {{variable}} binding and exit")
    a = ap.parse_args()

    token, pat = os.environ.get("AAVA_TOKEN", ""), os.environ.get("ADO_PAT", "")
    if not token:
        sys.exit("AAVA_TOKEN is not set")
    if not pat and not a.probe:
        sys.exit("ADO_PAT is not set")

    runinputs = {
        "adoorg": a.org, "adoproject": a.project, "adostoryid": a.storyid,
        "scenarioagentid": a.scenarioagent, "testcaseagentid": a.testcaseagent,
        "reviewagentid": 0 if a.no_judge else a.reviewagent,
        "maxscenarios": a.scenarios, "testcasesperscenario": a.cases,
        "stepsmin": a.stepsmin, "stepsmax": a.stepsmax,
        "maxhealrounds": 0 if a.no_heal else a.rounds,
        "maxworkers": a.scenarios,          # one wave, or the wall clock multiplies
        "deadlineseconds": a.deadline,
        "aavabaseurl": a.base, "realmid": a.realm, "userprincipal": a.user,
        "adopat": pat, "aavatoken": token,
    }

    if a.probe:
        tool = T.AavaTestGenOrchestrator()
        sys.exit(probe(tool._config(json.dumps(runinputs))))

    print(f"story {a.storyid}: {a.scenarios} scenarios x {a.cases} cases x "
          f"{a.stepsmin}-{a.stepsmax} steps, "
          f"{'no heal' if a.no_heal else str(a.rounds) + ' heal round(s)'}, "
          f"{'no judge' if a.no_judge else 'judged'}, deadline {a.deadline}s\n")

    t0 = time.monotonic()
    envelope = T.AavaTestGenOrchestrator()._run(runinputs=json.dumps(runinputs))
    wall = time.monotonic() - t0
    res = json.loads(envelope)

    print(f"\n=== {res.get('status')} in {wall:.0f}s ===")
    if res.get("status") != "completed":
        print(f"stage {res.get('stage')}: {res.get('error')}")
        sys.exit(1)

    for r in res["scenarios"]:
        print("  %-8s %-11s score %-16s rounds %d  tc %d  %5.0fs%s"
              % (r["scenarioid"], r["status"], r["scorehistory"] or "-", r["rounds"],
                 r["testcasecount"], r["elapsedms"] / 1000,
                 "  " + (r["error"] or "") if r["error"] else ""))
    for w in res["warnings"]:
        print("  warning:", w)

    s = res["summary"]
    print(f"\n{s['approved']}/{s['scenarios']} approved, {s['testcases']} test cases, "
          f"{s['agentcalls']} agent calls, {res['testcases']['rows']} table rows")
    print(f"envelope {len(envelope):,} chars — the table is on stdout above, between "
          f"[ORCH-TABLE-BEGIN] and [ORCH-TABLE-END]")

    if wall > 240:
        print(f"\nWARNING: {wall:.0f}s exceeds the 240s ACA ceiling. On the platform this run "
              f"would have been severed.")


if __name__ == "__main__":
    main()
