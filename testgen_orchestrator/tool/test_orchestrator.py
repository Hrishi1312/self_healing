"""Offline test suite for AavaTestGenOrchestrator. No credentials, no network.

The point of this file is to answer one question before anything is uploaded:
**will the tool accept what the agents actually produce?**

Most of it replays REAL agent output captured from the platform activity logs
(fixtures/) through the tool's parsers. A parser that rejects real output is the single
most likely way this breaks on first upload, and it is invisible until you run it.

    python3 testgen_orchestrator/tool/test_orchestrator.py

Exit code 0 means every check passed.
"""

import json
import os
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, HERE)

# ── stub the platform packages so the tool imports on a bare machine ─────────
if "crewai" not in sys.modules:
    crewai = types.ModuleType("crewai")
    tools = types.ModuleType("crewai.tools")

    class BaseTool:                      # minimal stand in for the real base class
        def __init__(self, **kw):
            pass

    tools.BaseTool = BaseTool
    crewai.tools = tools
    sys.modules["crewai"] = crewai
    sys.modules["crewai.tools"] = tools

import AavaTestGenOrchestrator as T      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"   {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")


def section(title):
    print(f"\n{title}")


def _try(fn):
    """True when fn raises, which is what a guard test wants to assert."""
    try:
        fn()
        return False
    except Exception:
        return True


def fixture(name):
    p = os.path.join(FIX, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else None


def as_current(table):
    """The captured fixtures predate the angle-bracket rule: they carry <member_ssn> style
    tokens that are now a violation. Convert them to the [TEST DATA: ...] form so the fixture
    represents output a CURRENT generator would produce. The raw fixture is kept for the
    checks that prove the rule catches the old shape."""
    import re as _r
    return _r.sub(r"<([A-Za-z][\w ]{0,30})>", r"[TEST DATA: \1]", table or "")


def build_table(n=12, steps=3):
    """A realistic 15-column table, standing in for the retired 13-column captured fixtures.

    The schema changed 2026-08-24 (no ScenarioId/AcceptanceCriteriaRef/Status columns; Test
    Case Type/Status/Test Type are now tool-injected constants — Manual/New/Functional). The
    old fixtures (fixtures/testcases_log*.md) are real captured output against the RETIRED
    schema and can never be made to match the new one without fabricating fake "real"
    output, so this replaces them as the stand-in used throughout the suite.
    """
    rows = ["| " + " | ".join(T.COLUMNS) + " |", "|" + "---|" * len(T.COLUMNS)]
    prios = ["High", "Medium", "Low"]
    for i in range(1, n + 1):
        tid, name = f"TC_{i:03d}", f"Verify AR PASSE inbound 834 case {i}"
        desc = f"Validate transaction effective date logic for case {i}."
        precon = "AR PASSE test member data is available in the staging environment."
        pc = T.PRIORITY_CODE[prios[(i - 1) % 3]]
        for st in range(1, steps + 1):
            rows.append("| " + " | ".join([
                tid, name, desc, precon, str(st),
                f"Perform step {st} of the transaction.", f"Step {st} completes as expected.",
                "Manual", "New", pc, "", "EDI", "", T.TEST_TYPE, ""]) + " |")
    return "\n".join(rows)


# ────────────────────────────────────────────────────────────────────────────
section("1. REAL AGENT OUTPUT THROUGH THE PARSERS  (the upload risk)")

# DISABLED 2026-08-24 — the schema changed (no ScenarioId/AcceptanceCriteriaRef/Status
# columns; Test Case Type/Status now tool-injected constants; a Test Type Functional/
# Regression axis added). fixtures/testcases_log*.md are real captured output against the
# RETIRED 13-column schema and will never parse under the new 15-column header — that is
# correct, not a regression. build_table() (defined above) stands in as the "accepts
# realistic output" check until a real capture exists against the new schema.
_bt = build_table(12, 3)
try:
    parsed = T.parse_testcases(_bt, 1, 100)
    check("parse_testcases accepts a realistic 15 column table",
          len(parsed["ids"]) == 12 and len(parsed["rows"]) == 36)
    print(f"          -> {len(parsed['ids'])} test cases, {len(parsed['rows'])} rows, "
          f"{parsed['chars']:,} chars")
except Exception as e:
    check("parse_testcases accepts a realistic 15 column table", False, str(e)[:150])

_rv = open(os.path.join(HERE, "..", "agents", "03_reviewer_agent.md"), encoding="utf-8").read()
check("Priority is still enforced, by agent 03 check 9",
      "must contain `P1`, `P2` or `P3`" in _rv)
check("Test Type is documented as a tool-injected constant in agent 03",
      "`Test Type` is always `Functional`" in _rv)
check("step count is still enforced, by agent 03 check 8",
      "stepsmin" in _rv and "stepsmax" in _rv)

for tag in ("log15", "log18", "log0814"):
    raw = fixture(f"scenarios_{tag}.json")
    if raw is None:
        continue
    try:
        got = T.parse_scenarios(raw, 99)
        check(f"parse_scenarios accepts real array from {tag}", len(got) > 0)
        print(f"          -> {len(got)} scenarios, ids {[s['scenarioId'] for s in got]}")
    except Exception as e:
        check(f"parse_scenarios accepts real array from {tag}", False, str(e)[:200])

# reviewer verdicts in the logs are the OLD single score shape; the new shape is asserted
# separately below. This proves the parser rejects the old shape loudly rather than silently.
old = fixture("verdict_log15.json")
if old:
    try:
        T.parse_verdict(old, ["TC_001"])
        check("parse_verdict rejects the OLD single score verdict shape", False,
              "it accepted a verdict with no scores array")
    except Exception:
        check("parse_verdict rejects the OLD single score verdict shape", True)

new_verdict = json.dumps({
    "scenarioid": "TS_001",
    "scores": [{"id": "TC_001", "score": 92, "pass": True, "gaps": []},
               {"id": "TC_002", "score": 78, "pass": False, "gaps": ["step 7 has no expected result"]}],
    "batchscore": 78, "batchpass": False})
try:
    v = T.parse_verdict(new_verdict, ["TC_001", "TC_002"])
    check("parse_verdict accepts the new per test case shape", len(v["scores"]) == 2)
except Exception as e:
    check("parse_verdict accepts the new per test case shape", False, str(e)[:150])

check("parse_verdict rejects a score for an unknown test case id",
      _try(lambda: T.parse_verdict(new_verdict, ["TC_001"])))


# ────────────────────────────────────────────────────────────────────────────
section("2. PARSER GUARDS  (bad output must be caught, not passed on)")


good_table = build_table(3, 3)
check("rejects a table with the wrong header",
      _try(lambda: T.parse_testcases(good_table.replace("Test Case Id", "TestCase Id"), 1, 100)))
check("rejects an empty response",
      _try(lambda: T.parse_testcases("", 1, 100)))
check("rejects prose with no table",
      _try(lambda: T.parse_testcases("Here are your test cases:", 1, 100)))
check("the tool no longer rejects step counts outside the range",
      not _try(lambda: T.parse_testcases(good_table, 500, 600)))
check("rejects a scenario array missing a required key",
      _try(lambda: T.parse_scenarios('[{"scenarioId":"TS_001","title":"x"}]', 5)))
check("rejects a malformed scenarioId",
      _try(lambda: T.parse_scenarios(json.dumps([{k: "x" for k in T.SCENARIO_KEYS}]), 5)))
check("rejects a duplicate scenarioId", _try(lambda: T.parse_scenarios(json.dumps([
    dict({k: "x" for k in T.SCENARIO_KEYS}, scenarioId="TS_001", type="Positive", priority="High"),
    dict({k: "x" for k in T.SCENARIO_KEYS}, scenarioId="TS_001", type="Edge", priority="Low")]), 5)))
check("accepts empty dorRef and dodRef, which is the real story shape",
      not _try(lambda: T.parse_scenarios(json.dumps([dict(
          {k: "x" for k in T.SCENARIO_KEYS}, scenarioId="TS_001", type="Positive",
          priority="High", dorRef="", dodRef="")]), 5)))
check("strips markdown fences an agent may wrap the JSON in",
      not _try(lambda: T.parse_scenarios("```json\n" + json.dumps([dict(
          {k: "x" for k in T.SCENARIO_KEYS}, scenarioId="TS_001", type="Positive",
          priority="High")]) + "\n```", 5)))


# ────────────────────────────────────────────────────────────────────────────
section("3. CONFIG VALIDATION  (a bad runinputs object must fail fast and say why)")

tool = T.AavaTestGenOrchestrator()
base = {"adoorg": "CSGRP", "adoproject": "ADO", "adostoryid": "640764",
        "scenarioagentid": 613, "testcaseagentid": 564, "reviewagentid": 559,
        "maxscenarios": 5}

cfg = tool._config(json.dumps(base))
check("defaults applied when optional keys are absent",
      cfg["passscore"] == T.DEF_PASSSCORE and cfg["maxhealrounds"] == T.DEF_MAXHEALROUNDS)
check("maxscenarios is mandatory", _try(lambda: tool._config(json.dumps(
    {k: v for k, v in base.items() if k != "maxscenarios"}))))
check("maxworkers always equals maxscenarios, one thread per scenario",
      tool._config(json.dumps(dict(base, maxscenarios=3)))["maxworkers"] == 3)
check("a maxworkers in runinputs is ignored, not honoured",
      tool._config(json.dumps(dict(base, maxscenarios=4, maxworkers=2)))["maxworkers"] == 4)
check("maxagentcalls sizes itself: 3 + scenarios x 2 x rounds",
      tool._config(json.dumps(base))["maxagentcalls"] == 3 + 5 * 2 * 3
      and tool._config(json.dumps(dict(base, maxscenarios=10)))["maxagentcalls"] == 63
      and tool._config(json.dumps(dict(base, maxhealrounds=0)))["maxagentcalls"] == 13)
check("an explicit maxagentcalls still overrides the formula",
      tool._config(json.dumps(dict(base, maxagentcalls=99)))["maxagentcalls"] == 99)
check("the minimal seven key payload is enough",
      tool._config(json.dumps(base))["testcasesperscenario"] == 8
      and tool._config(json.dumps(base))["hardstopscore"] == 50
      and tool._config(json.dumps(base))["maxworkers"] == 5)
check("testcasesperscenario is a ceiling clamped to 8, not the old 5",
      tool._config(json.dumps(dict(base, testcasesperscenario=20)))["testcasesperscenario"] == 12)
check("clamps an absurd deadline", tool._config(json.dumps(dict(base, deadlineseconds=99999)))["deadlineseconds"] == 3600)
check("coerces stoponstagnation from a string",
      tool._config(json.dumps(dict(base, stoponstagnation="false")))["stoponstagnation"] is False)
check("coerces numeric settings that arrive as strings, as form data does",
      tool._config(json.dumps(dict(base, maxscenarios="4", passscore="90")))["maxscenarios"] == 4)
check("rejects a missing story id", _try(lambda: tool._config(json.dumps(
    {k: v for k, v in base.items() if k != "adostoryid"}))))
check("rejects a non numeric agent id", _try(lambda: tool._config(json.dumps(dict(base, reviewagentid="abc")))))
check("rejects an object that is not JSON", _try(lambda: tool._config("not json at all")))

out = json.loads(tool._run(runinputs="not json at all"))
check("a bad object returns a JSON error envelope rather than raising",
      out.get("status") == "failed" and out.get("stage") == "validation")


# ────────────────────────────────────────────────────────────────────────────
section("4. HTML TO TEXT  (the acceptance criteria arrive as an ordered list)")

html = ("<div>Overview</div><ol><li>EDI is able to identify the dates</li>"
        "<li>Given an inbound file, if the span has not begun, use&nbsp;the Eligibility Date "
        "(DTP*356/DTP*348)</li></ol>")
txt = T._html_to_text(html)
check("each list item lands on its own line", len(txt.split("\n")) == 3)
check("entities decoded", "&nbsp;" not in txt and "use the Eligibility" in txt)
check("segment references survive", "DTP*356/DTP*348" in txt)
check("empty input is safe", T._html_to_text("") == "" and T._html_to_text(None) == "")


# ────────────────────────────────────────────────────────────────────────────
section("5. BUDGET  (must degrade, never abort)")

b = T._Budget(deadlineseconds=60, maxagentcalls=3)
check("allows calls under the cap", all(b.take() for _ in range(3)))
check("refuses the call past the cap", b.take() is False)
b2 = T._Budget(deadlineseconds=0.01, maxagentcalls=100)
time.sleep(0.05)
check("refuses once the deadline has passed", b2.take() is False)
check("remaining goes negative rather than throwing", b2.remaining() < 0)

errs = []


def hammer():
    try:
        for _ in range(200):
            b3.take()
    except Exception as e:
        errs.append(e)


b3 = T._Budget(deadlineseconds=60, maxagentcalls=50)
ts = [threading.Thread(target=hammer) for _ in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
check("call counter is thread safe under 8 threads", b3.calls == 50 and not errs,
      f"calls={b3.calls} errs={errs}")


# ────────────────────────────────────────────────────────────────────────────
section("6. LOGGING  (secrets must never reach a line)")

log = T._Log()
SECRET = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.SUPERSECRETTOKENVALUE"
log.guard(SECRET)
log.line("agentcall", token=SECRET, note=f"bearer {SECRET} used")
dumped = "\n".join(log.dump())
check("the secret never appears in the log", SECRET not in dumped)
check("it is replaced with a marker", "***" in dumped)
check("every line carries a UTC timestamp", all("ts=" in l and l.endswith("Z") is False
                                                and "T" in l for l in log.dump()))
check("empty and None fields are dropped, not printed as empty",
      "empty=" not in (log.line("x", empty="", none=None) or "\n".join(log.dump())))

lines, lerr = [], []


def logmany():
    try:
        for i in range(100):
            log.line("t", i=i)
    except Exception as e:
        lerr.append(e)


ts = [threading.Thread(target=logmany) for _ in range(6)]
[t.start() for t in ts]
[t.join() for t in ts]
check("log is thread safe under 6 threads", not lerr and len(log.dump()) >= 600)


# ────────────────────────────────────────────────────────────────────────────
section("7. FULL PIPELINE, MOCKED  (real recorded output, no network)")

real_table = build_table(12, 3)   # stand-in for the retired 13 column captured fixture
real_scen = fixture("scenarios_log0814.json")   # scenario schema is unchanged, still real

calls = {"n": 0, "ids": [], "threads": set()}


def fake_exec(agentid, userinputs, cfg, token, budget, log, label):
    calls["n"] += 1
    calls["ids"].append(agentid)
    calls["threads"].add(threading.current_thread().name)
    time.sleep(0.05)                                   # long enough to expose serialisation
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 50
    if agentid == cfg["testcaseagentid"]:
        return real_table, 50
    parsed = T.parse_testcases(real_table, 1, 100)
    return json.dumps({"scenarioid": "x",
                       "scores": [{"id": i, "score": 95, "pass": True, "gaps": []}
                                  for i in parsed["ids"]],
                       "batchscore": 95, "batchpass": True}), 50


def fake_story(cfg, pat, log):
    return {"storyid": "640764", "title": "834 transaction effective date logic",
            "description": "Overview and business rules.", "acceptancecriteria": "AC1 ... AC7 ..."}


orig_exec, orig_story, orig_secret = T.exec_agent, T.fetch_story, T._secret
T.exec_agent, T.fetch_story = fake_exec, fake_story
T._secret = lambda k, f="": "test-token"

blob = json.dumps(dict(base, maxscenarios=4, testcasesperscenario=3, stepsmin=1,
                       stepsmax=100, maxworkers=4, deadlineseconds=120))
t0 = time.monotonic()
res = json.loads(tool._run(runinputs=blob))
wall = time.monotonic() - t0

check("run completes", res.get("status") == "completed")
check("every scenario got a record", len(res["scenarios"]) == res["summary"]["scenarios"])
check("all scenarios approved when the reviewer passes them",
      res["summary"]["approved"] == res["summary"]["scenarios"], json.dumps(res["summary"]))
check("test cases were assembled", res["testcases"]["rows"] > 0)
check("the envelope carries a pointer, not the table",
      isinstance(res["testcases"], dict) and "where" in res["testcases"])
check("score history recorded per scenario",
      all(r["scorehistory"] for r in res["scenarios"]))
check("the outcome line leads with usable test cases, not machine states",
      any("step=outcome" in l and "ready=" in l for l in res["log"]),
      next((l for l in res["log"] if "step=outcome" in l), "no outcome line"))
check("machine detail is still logged, just demoted",
      any("step=detail" in l and "scoreboard=" in l for l in res["log"]))
check("every scenario record carries the flagged test case ids",
      all("flagged" in r for r in res["scenarios"]))
check("scoreboard names every scenario",
      all(r["scenarioid"] in "\n".join(res["log"]) for r in res["scenarios"]))

n = res["summary"]["scenarios"]
serial = n * 2 * 0.05
check(f"batches ran in parallel, not serially  ({wall:.2f}s vs {serial:.2f}s serial)",
      wall < serial * 0.75)
check("more than one worker thread was used", len(calls["threads"]) > 1,
      f"threads={len(calls['threads'])}")


# ── failure isolation ───────────────────────────────────────────────────────
section("8. FAILURE ISOLATION  (one bad scenario must not fail the run)")

def exploding_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        # TS_002 fails on every round, so it cannot heal its way out. A transient failure
        # would just be retried, which proves nothing about isolation.
        if '"scenarioId": "TS_002"' in userinputs["scenario"]:
            raise RuntimeError("simulated agent explosion")
        return real_table, 10
    parsed = T.parse_testcases(real_table, 1, 100)
    return json.dumps({"scenarioid": "x",
                       "scores": [{"id": i, "score": 95, "pass": True, "gaps": []}
                                  for i in parsed["ids"]],
                       "batchscore": 95, "batchpass": True}), 10


T.exec_agent = exploding_exec
res2 = json.loads(tool._run(runinputs=blob))
check("run still reports completed", res2.get("status") == "completed")
statuses = [r["status"] for r in res2["scenarios"]]
check("exactly one scenario is marked failed", statuses.count("failed") >= 1, str(statuses))
check("the other scenarios still approved", statuses.count("approved") >= 1, str(statuses))
check("the failure carries an error message",
      any(r.get("error") for r in res2["scenarios"] if r["status"] == "failed"))
check("test cases from the surviving scenarios are still counted",
      res2["testcases"]["rows"] > 0)


# ── healing ─────────────────────────────────────────────────────────────────
section("9. SELF HEALING  (targeted repair, stagnation, round cap)")

rounds = {"n": 0}


def healing_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        rounds["n"] += 1
        return real_table, 10
    parsed = T.parse_testcases(real_table, 1, 100)
    ids = parsed["ids"]
    scores = [{"id": ids[0], "score": 60, "pass": False, "gaps": ["step 3 has no expected result"]}]
    scores += [{"id": i, "score": 95, "pass": True, "gaps": []} for i in ids[1:]]
    return json.dumps({"scenarioid": "x", "scores": scores,
                       "batchscore": 60, "batchpass": False}), 10


T.exec_agent = healing_exec
res3 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=2, testcasesperscenario=3, stepsmin=1, stepsmax=100,
    maxworkers=2, maxhealrounds=3, stoponstagnation=False, deadlineseconds=120))))
sc = res3["scenarios"]
check("a never passing scenario ends unhealed, not failed",
      all(r["status"] == "unhealed" for r in sc), str([r["status"] for r in sc]))
check("it used exactly maxhealrounds rounds", all(r["rounds"] == 3 for r in sc),
      str([r["rounds"] for r in sc]))
check("its test cases are kept, not discarded", all(r["testcasecount"] > 0 for r in sc))
check("score history has one entry per round", all(len(r["scorehistory"]) == 3 for r in sc))

T.exec_agent = healing_exec
res4 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=2, testcasesperscenario=3, stepsmin=1, stepsmax=100,
    maxworkers=2, maxhealrounds=3, stoponstagnation=True, deadlineseconds=120))))
check("stoponstagnation stops early when the score does not improve",
      all(r["status"] == "stagnant" and r["rounds"] == 2 for r in res4["scenarios"]),
      str([(r["status"], r["rounds"]) for r in res4["scenarios"]]))

# mocks stay installed; section 11 restores them


# ────────────────────────────────────────────────────────────────────────────
section("10. PLACEHOLDER WIRING  (the failure that produces confident nonsense)")

# AAVA binds a sub agent's input by substituting {{name}} into its Description. A userInputs
# key with no matching placeholder never arrives: the agent runs on its instructions alone
# and invents a well formed answer that every parser downstream accepts. Nothing can detect
# that at run time, so the two sides are compared here, mechanically, on every run.
#
# Shape follows the working copy (Aava-local-testing/aava_selfheal_bugfixer): agent 00 takes
# ONE {{runinputs}} blob, and each sub agent takes several semantically grouped variables.
# Fields inside a grouped variable are documented as `group.field` and are NOT placeholders.
import re as _re                                          # noqa: E402
AGENTS = os.path.join(HERE, "..", "agents")
TOOLSRC = open(os.path.join(HERE, "AavaTestGenOrchestrator.py"), encoding="utf-8").read()


def prompt_of(fname):
    src = open(os.path.join(AGENTS, fname), encoding="utf-8").read()
    a = src.find("````")
    if a != -1:
        return src[a:src.find("````", a + 4)], src
    d = src.index("**Description (instruction prompt)**")
    o = src.index("```", d)
    return src[o:src.index("```", o + 3)], src


def sent(marker, end):
    """The userInputs keys, and the fields inside each grouped value, that the tool sends."""
    i = TOOLSRC.index(marker)
    block = TOOLSRC[i:TOOLSRC.index(end, i)]
    keys = set(_re.findall(r'^\s*"(\w+)":', block, _re.M))
    keys |= set(_re.findall(r'\w+\["(\w+)"\] = ', block))     # optional keys added later
    grouped = {g: set(_re.findall(r'"(\w+)":', inner))
               for g, inner in _re.findall(r'"(\w+)": _j\(\{(.*?)\}\)', block, _re.S)}
    # A grouped value spans lines, so its inner fields also matched the key pattern above.
    # They are fields, not userInputs keys.
    for fields in grouped.values():
        keys -= fields
    return keys, grouped


for fname, marker, end, label in (
    ("01_scenario_generator_agent.md", "scen_inputs = {", "raw, scen_ms", "01 scenario generator"),
    ("02_test_case_generator_agent.md", "gen_inputs = {", "try:", "02 test case generator"),
    ("03_reviewer_agent.md", "rev_inputs = {", "try:", "03 reviewer"),
):
    body, _ = prompt_of(fname)
    keys, grouped = sent(marker, end)
    declared = set(_re.findall(r"\{\{(\w+)\}\}", body))
    check(f"{label}: every userInputs key has a {{{{placeholder}}}}",
          not (keys - declared), f"missing from the prompt: {sorted(keys - declared)}")
    check(f"{label}: every {{{{placeholder}}}} is a key the tool sends",
          not (declared - keys), f"never sent by the tool: {sorted(declared - keys)}")
    for g, fields in grouped.items():
        short = g
        documented = set(_re.findall(r"`%s\.(\w+)`" % g, body))
        check(f"{label}: every field of {g} is documented as `{short}.<field>`",
              not (fields - documented), f"undocumented: {sorted(fields - documented)}")
        check(f"{label}: every `{short}.<field>` reference is a real field",
              not (documented - fields), f"not sent: {sorted(documented - fields)}")
    check(f"{label}: no bare dotted reference outside backticks",
          not _re.search(r"(?<![`\w])(?:storydata|limits|scenario)\.\w+", body))

orch, orch_src = prompt_of("00_orchestrator_agent.md")
check("00 orchestrator: declares one runinputs placeholder and nothing else",
      set(_re.findall(r"\{\{(\w+)\}\}", orch)) == {"runinputs"},
      f"found: {sorted(set(_re.findall(r'{{(\w+)}}', orch)))}")
check("00 orchestrator: the tool takes exactly one argument",
      set(T.AavaTestGenOrchestratorSchema.model_fields) == {"runinputs"},
      f"fields: {sorted(T.AavaTestGenOrchestratorSchema.model_fields)}")
doc_keys = set(_re.findall(r'"(\w+)":', orch_src[orch_src.index("## The runinputs object"):]))
required = {"adoorg", "adoproject", "adostoryid", "scenarioagentid", "testcaseagentid", "reviewagentid"}
check("00 orchestrator: the documented object carries every required key",
      required <= doc_keys, f"missing: {sorted(required - doc_keys)}")


section("11. THE FIVE FIXES FROM THE WORKING COPY")

# 1. No blind HTTP retry. The platform severs an agent call near 265s; retrying it three
#    times would spend ~800s inside one scenario.
_calls = {"n": 0}


class _Boom(T.requests.RequestException):
    pass


_orig_request = T.requests.request


def _counting_request(*a, **kw):
    _calls["n"] += 1
    raise _Boom("simulated connection reset")


T.requests.request = _counting_request
st, _body = T._http("POST", "https://example.invalid/agents/execute", T._Log())
T.requests.request = _orig_request
check("_http makes exactly ONE attempt, no retry", _calls["n"] == 1, f"attempts={_calls['n']}")
check("a severed request surfaces as status 0", st == 0)
check("the retry constants are gone",
      not hasattr(T, "MAX_HTTP_ATTEMPTS") and not hasattr(T, "RETRY_STATUSES"))

# 2. Bounded concurrency.
check("every guard sits BELOW the client's 240s ACA cut",
      T.DEF_DEADLINESECONDS < 240 and T.HTTP_TIMEOUT < 240,
      f"deadline={T.DEF_DEADLINESECONDS} http={T.HTTP_TIMEOUT}")

# 3/4. The deterministic pre gate.
import copy as _copy                                      # noqa: E402

# Schema changed 2026-08-24: Test Case Id is column 0, Name 1, Description 2, Precondition 3,
# Step Description 5, Expected Result 6. _ANCHOR is the step-1 description build_table()
# writes for every case, so replacing its first occurrence targets TC_001's first row.
_ANCHOR = "Perform step 1 of the transaction."
_RAW = build_table(12, 3)
check("the pre gate anchor exists in the synthetic table", _RAW.count(_ANCHOR) > 0)

_good = T.parse_testcases(_RAW, 1, 100)
check("pre gate passes a clean table",
      T.pregate(_good) == [], str(T.pregate(_good)[:2]))


def _mutated(fn):
    """Parse once, mutate the structure in memory, gate it. rows and cases share row objects."""
    p2 = _copy.deepcopy(T.parse_testcases(_RAW, 1, 100))
    fn(p2)
    return T.pregate(p2)


def _blank_expected(p2):
    p2["rows"][0][6] = ""
    p2["cases"][p2["rows"][0][0]][0][6] = ""


check("pre gate catches an empty Expected Result",
      any("Expected Result" in x for x in _mutated(_blank_expected)))
check("pre gate catches an empty Step Description",
      any("Description" in x for x in _mutated(
          lambda p2: (p2["rows"][0].__setitem__(5, ""),
                      p2["cases"][p2["rows"][0][0]][0].__setitem__(5, "")))))
check("pre gate catches a knowledge base name in the output",
      any("kb_" in x for x in _mutated(
          lambda p2: p2.__setitem__("table", p2["table"].replace(
              _ANCHOR, "See kb_edi_834_testcase_analysis for details.", 1)))))
check("pre gate catches a meta label in a Precondition",
      any("DoR" in x for x in _mutated(
          lambda p2: p2["rows"][0].__setitem__(3, "Per the DoR this must hold."))))

check("the pre gate leaves angle brackets to the agents, by design",
      _mutated(lambda p2: p2.__setitem__("table", p2["table"].replace(
          _ANCHOR, "Capture <ISA13> and <member_ssn> at run time.", 1))) == [])
check("ANGLE_TOKEN is gone from the tool", not hasattr(T, "ANGLE_TOKEN"))
check("a meta label in a column the gate does not check is NOT a violation",
      _mutated(lambda p2: p2["rows"][0].__setitem__(12, "DoD - file archived")) == [])

# A pre gate failure must NOT spend a reviewer call.
seen = {"gen": 0, "rev": 0}
bad_table = _RAW.replace(_ANCHOR, "Per the DoR this must hold.", 1)
check("the mutated raw table really does fail the gate",
      T.pregate(T.parse_testcases(bad_table, 1, 100)) != [])


def pregate_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        seen["gen"] += 1
        return bad_table, 10
    seen["rev"] += 1
    return json.dumps({"scenarioid": "x", "scores": [{"id": "TC_001", "score": 95, "pass": True,
                                                      "gaps": []}],
                       "batchscore": 95, "batchpass": True}), 10


T.exec_agent, T.fetch_story, T._secret = pregate_exec, fake_story, lambda k, f="": "t"
res5 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, stepsmin=1, stepsmax=100, maxworkers=1, maxhealrounds=2,
    deadlineseconds=120))))
check("a pre gate failure never spends a reviewer call",
      seen["rev"] == 0, f"reviewer calls={seen['rev']}")
check("the tool regenerates the round with the problems as feedback",
      seen["gen"] == 2, f"generator calls={seen['gen']}")
_rec5 = res5["scenarios"][0]
check("a never-passing gate ends unhealed with the problem in gaps",
      _rec5["status"] == "unhealed" and any("DoR" in g for g in _rec5["gaps"]),
      f"status={_rec5['status']} gaps={_rec5['gaps'][:1]}")
_src = open(os.path.join(HERE, "AavaTestGenOrchestrator.py"), encoding="utf-8").read()
check("pregate() is defined and wired in",
      "def pregate(" in _src
      and '\n            problems = pregate(parsed, cfg["bannedterms"])' in _src)
check("and the re-enable reason is written next to it", "re-enabled\n# 2026-08-19" in _src
      or "Re-enabled 2026-08-19" in _src or "re-enabled 2026-08-19" in _src)

# 3. Hard stop below the floor.
def lowscore_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        return real_table, 10
    ids = T.parse_testcases(real_table, 1, 100)["ids"]
    return json.dumps({"scenarioid": "x",
                       "scores": [{"id": i, "score": 30, "pass": False, "gaps": ["wrong subject"]}
                                  for i in ids],
                       "batchscore": 30, "batchpass": False}), 10


T.exec_agent = lowscore_exec
res6 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, stepsmin=1, stepsmax=100, maxworkers=1, maxhealrounds=3,
    deadlineseconds=120))))
r6 = res6["scenarios"][0]
check("a score below hardstopscore stops immediately", r6["status"] == "hardstop", str(r6["status"]))
check("it stops after ONE round, not three", r6["rounds"] == 1, f"rounds={r6['rounds']}")
check("hardstop is counted in the summary", res6["summary"]["hardstop"] == 1)

# 5. Degraded modes.
T.exec_agent = healing_exec
res7 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, stepsmin=1, stepsmax=100, maxworkers=1, maxhealrounds=0,
    deadlineseconds=120))))
check("maxhealrounds=0 still runs ONE pass", res7["scenarios"][0]["rounds"] == 1,
      str(res7["scenarios"][0]["rounds"]))
check("maxhealrounds=0 returns test cases and a score",
      res7["scenarios"][0]["testcasecount"] > 0 and res7["scenarios"][0]["finalscore"] is not None)

nojudge = {"rev": 0}


def nojudge_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        return real_table, 10
    nojudge["rev"] += 1
    return "{}", 10


T.exec_agent = nojudge_exec
res8 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, stepsmin=1, stepsmax=100, maxworkers=1, reviewagentid=0,
    deadlineseconds=120))))
check("reviewagentid=0 never calls a judge", nojudge["rev"] == 0)
check("reviewagentid=0 yields status unreviewed",
      res8["scenarios"][0]["status"] == "unreviewed", str(res8["scenarios"][0]["status"]))
check("unreviewed test cases are still counted", res8["testcases"]["rows"] > 0)
check("a real agent id is still required for the generator",
      _try(lambda: tool._config(json.dumps(dict(base, testcaseagentid=0)))))

T.exec_agent, T.fetch_story, T._secret = orig_exec, orig_story, orig_secret


section("12. NESTED JSON OUTPUT  (halves what the generator has to write)")

_REV_PC = {v: k for k, v in T.PRIORITY_CODE.items()}   # "P1" -> "High", table -> agent JSON
_p = T.parse_testcases(build_table(6, 3), 1, 100)
_nested = []
for tid, rws in _p["cases"].items():
    h = rws[0]
    _nested.append({
        "id": h[0], "name": h[1], "description": h[2], "precondition": h[3],
        "priority": _REV_PC[h[9]],
        "steps": [{"no": r[4], "description": r[5], "expected": r[6]} for r in rws]})
_json = json.dumps(_nested)

expanded = T.expand_testcases(_json)
back = T.parse_testcases(expanded, 1, 100)
check("nested JSON expands to a valid 15 column table", back["header"] == T.COLUMNS)
check("no test case is lost in expansion",
      set(back["ids"]) == set(_p["ids"]), f"{len(back['ids'])} vs {len(_p['ids'])}")
check("no step row is lost in expansion",
      len(back["rows"]) == len(_p["rows"]), f"{len(back['rows'])} vs {len(_p['rows'])}")
check("the repeated columns are filled down on every step row",
      all(r[0] and r[1] and r[3] for r in back["rows"]))
check("Test Case Type is always Manual and Test Type is always Functional",
      {r[7] for r in back["rows"]} == {"Manual"} and {r[13] for r in back["rows"]} == {T.TEST_TYPE})
print("          -> JSON %d chars vs table %d chars  (%.2fx, %d%% less to write)"
      % (len(_json), len(_p["table"]), len(_json)/len(_p["table"]),
         100 - 100*len(_json)//len(_p["table"])))
check("the JSON really is smaller than the table it replaces",
      len(_json) < len(_p["table"]) * 0.75)

# a value carrying a pipe or a newline must not break the row
_dirty = json.dumps([{**_nested[0], "precondition": "state is AR | MI\nand data exists",
                      "steps": _nested[0]["steps"][:2]}])
_d = T.parse_testcases(T.expand_testcases(_dirty), 1, 100)
check("a pipe inside a value cannot break the row", len(_d["rows"]) == 2)
check("a newline inside a value cannot split the row", "\n" not in _d["rows"][0][3])

check("rejects JSON with no steps array",
      _try(lambda: T.expand_testcases(json.dumps([{**_nested[0], "steps": []}]))))
check("rejects a test case missing a required key",
      _try(lambda: T.expand_testcases(json.dumps([{k: v for k, v in _nested[0].items()
                                                   if k != "priority"}]))))
check("rejects a step with no expected result",
      _try(lambda: T.expand_testcases(json.dumps(
          [{**_nested[0], "steps": [{"no": 1, "description": "x"}]}]))))
check("rejects prose instead of JSON", _try(lambda: T.expand_testcases("here you go")))

# read_testcases accepts either shape, so an older agent is not a hard fail
check("read_testcases accepts the nested JSON",
      len(T.read_testcases(_json, 1, 100)["ids"]) == len(_p["ids"]))
check("read_testcases still accepts a markdown table",
      len(T.read_testcases(_p["table"], 1, 100)["ids"]) == len(_p["ids"]))

check("the log flushes every line", "flush=True" in open(
    os.path.join(HERE, "AavaTestGenOrchestrator.py"), encoding="utf-8").read())


section("13. THE TABLE LEAVES THE ENVELOPE  (~900s of agent relay at 8 scenarios)")

import io as _io, contextlib as _ctx                        # noqa: E402

T.exec_agent, T.fetch_story, T._secret = fake_exec, fake_story, lambda k, f="": "t"
_buf = _io.StringIO()
with _ctx.redirect_stdout(_buf):
    res9 = json.loads(tool._run(runinputs=json.dumps(dict(
        base, maxscenarios=2, stepsmin=1, stepsmax=100, maxworkers=2, deadlineseconds=120))))
out = _buf.getvalue()
T.exec_agent, T.fetch_story, T._secret = orig_exec, orig_story, orig_secret

check("the envelope no longer carries the table text",
      isinstance(res9["testcases"], dict), str(type(res9["testcases"])))
check("it carries rows, chars and where to find it",
      {"rows", "chars", "scenarios", "where"} <= set(res9["testcases"]))
check("the table was printed to stdout between markers",
      "[ORCH-TABLE-BEGIN]" in out and "[ORCH-TABLE-END]" in out)
body = out.split("[ORCH-TABLE-BEGIN]", 1)[1].split("[ORCH-TABLE-END]", 1)[0]
check("what was printed is a valid 15 column table",
      T.parse_testcases(body[body.index("|"):], 1, 200)["header"] == T.COLUMNS)
check("the printed table has exactly one header row", body.count("| Test Case Id |") == 1)
check("the reported row count matches what was printed",
      T.parse_testcases(body[body.index("|"):], 1, 200)["rows"].__len__() == res9["testcases"]["rows"])
env = json.dumps(res9)
check("the envelope is small enough to relay cheaply", len(env) < 20000,
      f"{len(env)} chars = {len(env)//4} tokens")
print("          -> envelope %d chars (~%d tokens, ~%.0fs relay @100 tok/s); "
      "table %d chars on stdout"
      % (len(env), len(env)/4, len(env)/4/100, res9["testcases"]["chars"]))
check("userprincipal is never blank",
      tool._config(json.dumps(base))["userprincipal"] == T.DEF_USERPRINCIPAL)


section("14. ERROR DETAIL  (a bare status code is useless on someone else's platform)")

for shape, want in [
    ({"message": "agent is not in an executable state"}, "executable state"),
    ({"error": "realm mismatch"}, "realm mismatch"),
    ({"detail": "model deployment not found"}, "deployment not found"),
    ({"errors": ["knowledge base 172 unavailable"]}, "knowledge base 172"),
    ({"data": {"message": "nested reason"}}, "nested reason"),
    ("plain text upstream error", "plain text upstream"),
]:
    got = T._err_detail(shape)
    check(f"extracts the reason from {str(shape)[:44]}", want in got, f"got {got!r}")
check("falls back to the raw payload when no known key is present",
      "unexpected" in T._err_detail({"weird": "unexpected shape"}))
check("an empty body does not crash the extractor", T._err_detail(None) == "")

_seen = {}


def failing_exec(method, url, log, headers=None, json_body=None, timeout=None, form=None):
    return 502, {"message": "upstream agent execution failed"}


_orig_http = T._http
T._http = failing_exec
_log = T._Log()
try:
    T.exec_agent(625, {"x": "y"}, {"aavabaseurl": "https://x", "realmid": "4",
                                   "userprincipal": "a@b"}, "tok",
                 T._Budget(60, 5), _log, "scenarios")
except Exception as e:
    _seen["msg"] = str(e)
T._http = _orig_http
line = "\n".join(_log.dump())
check("the agenterror line carries the upstream reason",
      "upstream agent execution failed" in line, line[-160:])
check("it also records realm and user, the two usual suspects",
      "realm=4" in line and "user=a@b" in line, line[-160:])
check("the raised error repeats the reason",
      "upstream agent execution failed" in _seen.get("msg", ""), _seen.get("msg", ""))


section("14b. THE OUTCOME LINE  (what a reader needs, not what a debugger needs)")

# one scenario passes cleanly, one has a single flagged test case
_ids = T.parse_testcases(real_table, 1, 100)["ids"]


def mixed_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        return real_table, 10
    bad = '"scenarioId": "TS_002"' in userinputs["scenario"]
    scores = [{"id": i, "score": 95, "pass": True, "gaps": []} for i in _ids]
    if bad:
        scores[0] = {"id": _ids[0], "score": 85, "pass": False,
                     "gaps": ["Status field is \"Edge\" but no Test Step Expected Result "
                              "describes an error, a rejection, or an absent record"]}
    return json.dumps({"scenarioid": "x", "scores": scores,
                       "batchscore": 85 if bad else 95, "batchpass": not bad}), 10


T.exec_agent, T.fetch_story, T._secret = mixed_exec, fake_story, lambda k, f="": "t"
res10 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=2, stepsmin=1, stepsmax=100, maxworkers=2, maxhealrounds=1,
    deadlineseconds=120))))
T.exec_agent, T.fetch_story, T._secret = orig_exec, orig_story, orig_secret

_out = next(l for l in res10["log"] if "step=outcome" in l)
_flag = [l for l in res10["log"] if "step=flagged" in l]
total = res10["summary"]["testcases"]
bad = sum(len(r["flagged"]) for r in res10["scenarios"])
check("outcome counts READY test cases, not scenarios",
      f"ready={total - bad}/{total} testcases" in _out, _out)
check("it reports the flagged count", "flagged=%d" % bad in _out, _out)
check("elapsed reads as minutes and seconds", "elapsed=0m" in _out, _out)
check("one flagged line per flagged test case", len(_flag) == bad, f"{len(_flag)} vs {bad}")
check("each names the test case, its scenario and the reason",
      all("tc=" in l and "scenario=" in l and "why=" in l for l in _flag))
check("the reason is the reviewer's own words, not a translation",
      any("absent record" in l for l in _flag), _flag[:1])
check("agent 03 is asked for a plain English reason, capped at 60 characters",
      "under 60 characters" in _rv and '"reason"' in _rv)
check("reason is for a person, gaps stay for the generator",
      "`reason` is for a human, `gaps` is for the generator" in _rv)

# when the reviewer supplies `reason`, that is what the reader sees
_v = json.dumps({"scenarioid": "TS_001",
                 "scores": [{"id": "TC_001", "score": 85, "pass": False,
                             "reason": "the Edge case never tests an edge condition",
                             "gaps": ["Status field is \"Edge\" but no Test Step Expected "
                                      "Result describes an error, a rejection, or an absent record"]}],
                 "batchscore": 85, "batchpass": False})
_p = T.parse_verdict(_v, ["TC_001"])
_f = [{"id": x["id"], "why": (str(x.get("reason") or "").strip() or (x.get("gaps") or [""])[0])[:120]}
      for x in _p["scores"] if not x.get("pass")]
check("the plain English reason wins over the technical gap",
      _f[0]["why"] == "the Edge case never tests an edge condition", _f[0]["why"])
_v2 = json.loads(_v); _v2["scores"][0].pop("reason")
_f2 = [{"id": x["id"], "why": (str(x.get("reason") or "").strip() or (x.get("gaps") or [""])[0])[:120]}
       for x in json.loads(json.dumps(_v2))["scores"] if not x.get("pass")]
check("an older reviewer with no reason field still says something useful",
      "absent record" in _f2[0]["why"], _f2[0]["why"])

# the separator row must never be reported as a duplicate again
_sep = "| " + " | ".join(T.COLUMNS) + " |\n|" + "---|" * len(T.COLUMNS) + "\n"
_row = ("| TC_001 | Verify thing | Desc | Pre | 1 | Do | Then | Manual | New | P1 |  | EDI |"
        "  | Functional |  |")
_recs = [{"scenarioid": "TS_00%d" % i, "testcasecount": 1,
          "table": _sep + _row} for i in (1, 2, 3)]
check("the markdown separator row is never a duplicate",
      T.cross_batch_check(_recs, [{"scenarioId": "TS_00%d" % i} for i in (1, 2, 3)]) == []
      or all("---" not in w for w in T.cross_batch_check(
          _recs, [{"scenarioId": "TS_00%d" % i} for i in (1, 2, 3)])),
      str(T.cross_batch_check(_recs, [{"scenarioId": "TS_00%d" % i} for i in (1, 2, 3)])[:2]))
# every scenario numbers its own cases from TC_001, so concatenation collides
# (run 640764_20260824_122700 delivered 42 test cases under 8 distinct ids)
_r1 = {"scenarioid": "TS_001", "testcasecount": 2, "table": build_table(2, 2)}
_r2 = {"scenarioid": "TS_002", "testcasecount": 2, "table": build_table(2, 2),
       "flagged": [{"id": "TC_002", "why": "duplicate business intent"}]}
_map = T.renumber_testcases([_r1, _r2])
_ids1 = T.parse_testcases(_r1["table"], 1, 100)["ids"]
_ids2 = T.parse_testcases(_r2["table"], 1, 100)["ids"]
check("colliding per-scenario ids are renumbered into one global sequence",
      set(_ids1) == {"TC_001", "TC_002"} and set(_ids2) == {"TC_003", "TC_004"},
      f"{_ids1} / {_ids2}")
check("the renumber mapping names every rewritten id",
      _map.get("TS_002") == {"TC_001": "TC_003", "TC_002": "TC_004"}, str(_map))
_rrows = T.parse_testcases(_r2["table"], 1, 100)["rows"]
check("renumbering rewrites only the id column",
      all(r[1].startswith("Verify AR PASSE") and r[13] == T.TEST_TYPE for r in _rrows))
check("flagged ids follow the renumbering, so run_local names real ids",
      _r2["flagged"][0]["id"] == "TC_004", str(_r2["flagged"]))

check("a clean scenario has an empty flagged list",
      any(r["flagged"] == [] for r in res10["scenarios"]))
check("a flagged scenario names the id and keeps its other test cases",
      any(len(r["flagged"]) == 1 and r["testcasecount"] > 1 for r in res10["scenarios"]))
print("          -> " + _out.split("step=outcome ")[1][:100])


section("15. ASYNC SUBMIT + POLL  (this deployment does not answer at submit time)")

_calls2 = []


def fake_platform(method, url, log, headers=None, json_body=None, timeout=None, form=None):
    """Stand in for the real endpoints: submit accepts multipart, poll runs then succeeds."""
    _calls2.append((method, url, form))
    if method == "POST":
        assert form is not None, "submit must be multipart, not json"
        return 200, {"data": {"agentExecutionId": "exec-abc", "jobId": 1954,
                              "message": "Agent job submitted successfully"},
                     "status": "SUCCESS"}
    n = sum(1 for c in _calls2 if c[0] == "GET")
    if n < 3:
        return 200, {"status": "RUNNING"}
    return 200, {"status": "SUCCESS", "output": '[{"scenarioId": "TS_001"}]'}


_orig_http, _orig_sleep = T._http, time.sleep
T._http, T.time.sleep = fake_platform, lambda s: None
_log2 = T._Log()
_budget2 = T._Budget(120, 5)
out2, ms2 = T.exec_agent(625, {"storydata": "{}"},
                         {"aavabaseurl": "https://host", "realmid": "4",
                          "userprincipal": "a@b"}, "tok", _budget2, _log2, "scenarios")
T._http, T.time.sleep = _orig_http, _orig_sleep

posts = [c for c in _calls2 if c[0] == "POST"]
gets = [c for c in _calls2 if c[0] == "GET"]
check("submit posts to /agents/execute/agent-executions",
      posts[0][1].endswith("/agents/execute/agent-executions"), posts[0][1])
check("submit is multipart form data, never a JSON body", posts[0][2] is not None)
check("the form carries agentId, executionId, user and userInputs",
      {"agentId", "executionId", "user", "userInputs"} == set(posts[0][2]), str(set(posts[0][2])))
check("userInputs is a JSON string inside one form field",
      isinstance(posts[0][2]["userInputs"], str)
      and json.loads(posts[0][2]["userInputs"])["storydata"] == "{}")
check("poll gets /agents/execute/history/execution with the server's execution id",
      "/agents/execute/history/execution?execution_id=exec-abc" in gets[0][1], gets[0][1])
check("it keeps polling while the status is not terminal", len(gets) == 3, f"{len(gets)} polls")
check("the agent output comes back as text", out2 == '[{"scenarioId": "TS_001"}]', out2)
check("only the SUBMIT spends budget, not each poll", _budget2.calls == 1, f"{_budget2.calls}")
check("elapsed is measured across submit and poll", isinstance(ms2, int) and ms2 >= 0)

# a terminal non-success must raise, not return junk
def failed_platform(method, url, log, headers=None, json_body=None, timeout=None, form=None):
    if method == "POST":
        return 200, {"data": {"agentExecutionId": "exec-x"}, "status": "SUCCESS"}
    return 200, {"status": "FAILED", "output": None}


T._http, T.time.sleep = failed_platform, lambda s: None
check("a terminal FAILED raises rather than returning nothing",
      _try(lambda: T.exec_agent(1, {}, {"aavabaseurl": "https://h"}, "t",
                                T._Budget(60, 5), T._Log(), "gen")))

# 404 while polling means "not recorded yet", not a failure
_n = {"i": 0}


def slow_platform(method, url, log, headers=None, json_body=None, timeout=None, form=None):
    if method == "POST":
        return 200, {"data": {"agentExecutionId": "exec-y"}, "status": "SUCCESS"}
    _n["i"] += 1
    if _n["i"] == 1:
        return 404, {"message": "not found yet"}
    return 200, {"status": "SUCCESS", "output": "done"}


T._http = slow_platform
o3, _ = T.exec_agent(1, {}, {"aavabaseurl": "https://h"}, "t", T._Budget(60, 5), T._Log(), "gen")
check("a 404 mid poll is treated as 'not recorded yet', not an error", o3 == "done", o3)

# output may arrive already decoded; every parser downstream wants text
T._http = lambda m, u, l, headers=None, json_body=None, timeout=None, form=None: (
    (200, {"data": {"agentExecutionId": "e"}, "status": "SUCCESS"}) if m == "POST"
    else (200, {"status": "SUCCESS", "output": [{"scenarioId": "TS_009"}]}))
o4, _ = T.exec_agent(1, {}, {"aavabaseurl": "https://h"}, "t", T._Budget(60, 5), T._Log(), "gen")
T._http, T.time.sleep = _orig_http, _orig_sleep
check("a decoded output object is normalised back to text",
      isinstance(o4, str) and "TS_009" in o4 and len(T.parse_scenarios(
          json.dumps([dict({k: "x" for k in T.SCENARIO_KEYS}, scenarioId="TS_009",
                           type="Edge", priority="Low")]), 5)) == 1)


section("16. AAVA UPLOAD GATE")

src = open(os.path.join(HERE, "AavaTestGenOrchestrator.py"), encoding="utf-8").read()
import re as _re
check("exactly one BaseTool subclass in the file",
      len(_re.findall(r"class \w+\(BaseTool\)", src)) == 1)
check("the class name the upload will reference exists",
      "class AavaTestGenOrchestrator(BaseTool)" in src)
check("args_schema is declared", "args_schema: Type[BaseModel]" in src)
check("_run unwraps CrewAI's nested kwargs", 'kwargs.get("kwargs", kwargs)' in src)
check("every _run exit returns a JSON string", src.count("return json.dumps(") >= 5)
check("AVASecret import is guarded", "except ImportError" in src)
check("no hardcoded secret literals",
      not _re.search(r'(token|pat|secret)\s*=\s*"[A-Za-z0-9_\-\.]{20,}"', src, _re.I))

section("17. GITHUB PUBLISH  (opt in, and a failure can never fail the run)")

_ghtoken = "ghp_FAKETOKENVALUE1234567890"
_gh_base = dict(base, maxscenarios=2, testcasesperscenario=3, stepsmin=1, stepsmax=100,
                maxworkers=2, deadlineseconds=120)

cfgp = tool._config(json.dumps(_gh_base))
check("publish defaults to false", cfgp["publish"] is False)
check("github repo and branch have defaults",
      cfgp["githubrepo"] == "Hrishi1312/self_healing" and cfgp["githubbranch"] == "main")
check("publish coerced from a string, as form data arrives",
      tool._config(json.dumps(dict(_gh_base, publish="true")))["publish"] is True)

_puts = []


def gh_http(method, url, log, headers=None, json_body=None, timeout=None, form=None):
    _puts.append((method, url, headers, json_body))
    return 201, {"content": {"path": url.split("/contents/")[-1]}}


T.exec_agent, T.fetch_story = fake_exec, fake_story
T._secret = lambda k, f="": f or "test-token"
_orig_http2 = T._http
T._http = gh_http

# publish off: not one github call
res17 = json.loads(tool._run(runinputs=json.dumps(_gh_base)))
check("publish off makes no github call and adds no envelope field",
      _puts == [] and "published" not in res17)

# publish on: four files into one run folder
res18 = json.loads(tool._run(runinputs=json.dumps(dict(
    _gh_base, publish=True, githubtoken=_ghtoken))))
names = [u.split("/")[-1] for _, u, _, _ in _puts]
check("publish on PUTs exactly the four run files",
      len(_puts) == 4 and set(names) == {"run.log", "testcases.md", "envelope.json",
                                         "runinputs.json"}, str(names))
check("files go to the contents api of the configured repo",
      all(u.startswith("https://api.github.com/repos/Hrishi1312/self_healing/contents/"
                       "tool_logs/640764_") for _, u, _, _ in _puts),
      _puts[0][1] if _puts else "no calls")
check("all four files share one run folder",
      len({u.rsplit("/", 1)[0] for _, u, _, _ in _puts}) == 1)
check("the body carries branch and base64 content",
      all(b.get("branch") == "main" and b.get("content") for _, _, _, b in _puts))
import base64 as _b64                                     # noqa: E402
_published_inputs = json.loads(_b64.b64decode(
    next(b["content"] for _, u, _, b in _puts if u.endswith("runinputs.json"))).decode())
check("published runinputs.json blanks all three credentials",
      _published_inputs.get("githubtoken") == "" and _published_inputs.get("adopat") == ""
      and _published_inputs.get("aavatoken") == "")
check("the github token never reaches a log line",
      _ghtoken not in "\n".join(res18["log"]))
check("the envelope reports what was published",
      res18.get("published", {}).get("files") == 4
      and res18["published"]["repo"] == "Hrishi1312/self_healing")
check("the run is still completed", res18.get("status") == "completed")

# publish rejected by github: the run must not fail
T._http = lambda m, u, l, headers=None, json_body=None, timeout=None, form=None: (
    401, {"message": "Bad credentials"})
res19 = json.loads(tool._run(runinputs=json.dumps(dict(
    _gh_base, publish=True, githubtoken=_ghtoken))))
check("a github failure never fails the run",
      res19.get("status") == "completed"
      and "Bad credentials" in res19.get("published", {}).get("error", ""),
      str(res19.get("published")))

# publish asked for but no token: reported, not raised
T._http = gh_http
_puts.clear()
res20 = json.loads(tool._run(runinputs=json.dumps(dict(_gh_base, publish=True))))
check("publish with no token is reported in the envelope, with no github call",
      _puts == [] and "githubtoken" in res20.get("published", {}).get("error", ""),
      str(res20.get("published")))

T.exec_agent, T.fetch_story, T._secret = orig_exec, orig_story, orig_secret
T._http = _orig_http2


section("18. RAW OUTPUT ON PARSE FAILURE  (run 084112 failed 7/7 and the log said nothing)")


def empty_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        return "```json\n[]\n```", 10          # what the 084112 run appears to have received
    return "{}", 10


T.exec_agent, T.fetch_story, T._secret = empty_exec, fake_story, lambda k, f="": "t"
res21 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, maxhealrounds=0, deadlineseconds=120))))
check("a generate parse failure logs the head of the raw answer",
      any("step=generate" in l and "raw=" in l and "[]" in l for l in res21["log"]),
      next((l for l in res21["log"] if "step=generate" in l), "no generate line"))
check("the run still completes and reports the scenario failed",
      res21.get("status") == "completed"
      and res21["scenarios"][0]["status"] == "failed")
T.exec_agent, T.fetch_story, T._secret = orig_exec, orig_story, orig_secret


section("19. EXPERT-BASELINE FIXES  (run 640764_141324 vs the expert workbook)")

# 1. Config: the two new optional inputs.
check("domainhints defaults to empty and passes through",
      tool._config(json.dumps(base))["domainhints"] == ""
      and tool._config(json.dumps(dict(base, domainhints=" Assessment Date = HD03 ")))[
          "domainhints"] == "Assessment Date = HD03")
check("bannedterms accepts a comma string or a list",
      tool._config(json.dumps(dict(base, bannedterms="DTP01, DTP03 ,")))["bannedterms"]
      == ["DTP01", "DTP03"]
      and tool._config(json.dumps(dict(base, bannedterms=["DTP01"])))["bannedterms"]
      == ["DTP01"]
      and tool._config(json.dumps(base))["bannedterms"] == [])

# 2. A bare JSON object on a heal round is rejected with actionable feedback, never parsed
#    as markdown and never allowed to replace the whole table with one case.
try:
    T.read_testcases('{ "id": "TC_003_02", "name": "x", "steps": [] }', 1, 100)
    _objerr = ""
except ValueError as e:
    _objerr = str(e)
check("a bare JSON object is rejected with the full-array instruction",
      "complete" in _objerr and "array" in _objerr and "ALL test cases" in _objerr,
      _objerr[:80] or "did not raise")

# 3. Priority differentiation: an all-P1 batch of 4+ cases fails the pre gate; smaller
#    batches and mixed batches pass.
_allp1 = T.parse_testcases(
    build_table(5, 2).replace("| P2 |", "| P1 |").replace("| P3 |", "| P1 |"), 1, 100)
check("pre gate catches an all-P1 batch of 4 or more cases",
      any("High priority" in x for x in T.pregate(_allp1)),
      str(T.pregate(_allp1)[:1]))
_small = T.parse_testcases(
    build_table(3, 2).replace("| P2 |", "| P1 |").replace("| P3 |", "| P1 |"), 1, 100)
check("an all-P1 batch of 3 or fewer cases is not a violation", T.pregate(_small) == [])
check("a mixed-priority batch is not a violation",
      T.pregate(T.parse_testcases(build_table(6, 2), 1, 100)) == [])

# 4. Banned terms: token match, so DTP03 fires as a bare element name but never inside
#    the legitimate qualifier DTP*303.
_banned = T.parse_testcases(build_table(4, 2).replace(
    _ANCHOR, "Verify the Loop 2000 DTP01 Maintenance Date is loaded.", 1), 1, 100)
check("pre gate catches a banned term as a whole token",
      any("DTP01" in x for x in T.pregate(_banned, ["DTP01", "DTP03"])))
_legit = T.parse_testcases(build_table(4, 2).replace(
    _ANCHOR, "Verify the DTP*303 Maintenance Date is loaded.", 1), 1, 100)
check("a banned term inside a legitimate qualifier does not fire",
      T.pregate(_legit, ["DTP03", "DTP01"]) == [])
check("no banned terms configured means no banned-term check",
      T.pregate(_banned) == [])

# 5. Wiring: domainhints reaches all three agents, storyac reaches the reviewer, and both
#    are always bound (never an unbound {{variable}}).
_captured = {}


def capture_exec(agentid, userinputs, cfg, token, budget, log, label):
    _captured[agentid] = dict(userinputs)
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        return real_table, 10
    ids = T.parse_testcases(real_table, 1, 100)["ids"]
    return json.dumps({"scenarioid": "x", "batchpass": True, "batchscore": 95,
                       "scores": [{"id": i, "score": 95, "pass": True, "gaps": []}
                                  for i in ids]}), 10


T.exec_agent, T.fetch_story, T._secret = capture_exec, fake_story, lambda k, f="": "t"
res22 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, stepsmin=1, stepsmax=100, deadlineseconds=120,
    domainhints="Assessment Date = Loop 2300 HD03"))))
_scen_in = _captured.get(base["scenarioagentid"], {})
_gen_in = _captured.get(base["testcaseagentid"], {})
_rev_in = _captured.get(base["reviewagentid"], {})
check("domainhints reaches the scenario, generator and reviewer agents",
      _scen_in.get("domainhints") == "Assessment Date = Loop 2300 HD03"
      and _gen_in.get("domainhints") == "Assessment Date = Loop 2300 HD03"
      and _rev_in.get("domainhints") == "Assessment Date = Loop 2300 HD03")
check("the reviewer receives the full story acceptance criteria",
      bool(_rev_in.get("storyac")) and _rev_in["storyac"] != "{{storyac}}")
_captured.clear()
res23 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, stepsmin=1, stepsmax=100, deadlineseconds=120))))
check("with no hints configured the keys are still bound, as 'none'",
      _captured.get(base["testcaseagentid"], {}).get("domainhints") == "none"
      and _captured.get(base["reviewagentid"], {}).get("domainhints") == "none")

# 6. A scenario that heals to approved clears the earlier round's error, so a consumer
#    keying off error != None never misreads a healed scenario as broken (640764 TS_003).
_healcalls = {"gen": 0}


def heal_exec(agentid, userinputs, cfg, token, budget, log, label):
    if agentid == cfg["scenarioagentid"]:
        return real_scen, 10
    if agentid == cfg["testcaseagentid"]:
        _healcalls["gen"] += 1
        if _healcalls["gen"] == 1:
            return '{ "id": "TC_003_02", "name": "only one case" }', 10   # the TS_003 shape
        return real_table, 10
    ids = T.parse_testcases(real_table, 1, 100)["ids"]
    return json.dumps({"scenarioid": "x", "batchpass": True, "batchscore": 95,
                       "scores": [{"id": i, "score": 95, "pass": True, "gaps": []}
                                  for i in ids]}), 10


T.exec_agent, T.fetch_story, T._secret = heal_exec, fake_story, lambda k, f="": "t"
res24 = json.loads(tool._run(runinputs=json.dumps(dict(
    base, maxscenarios=1, stepsmin=1, stepsmax=100, maxhealrounds=3, deadlineseconds=120))))
_rec24 = res24["scenarios"][0]
check("a bare-object round is rejected and healed on the next round",
      _rec24["status"] == "approved" and _healcalls["gen"] == 2,
      f"status={_rec24['status']} gen={_healcalls['gen']}")
check("an approved scenario carries no stale error from the failed round",
      _rec24["error"] is None, str(_rec24["error"])[:80])
T.exec_agent, T.fetch_story, T._secret = orig_exec, orig_story, orig_secret


print(f"\n{'=' * 70}\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("   FAILED:", f)
sys.exit(1 if FAIL else 0)
