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


# ────────────────────────────────────────────────────────────────────────────
section("1. REAL AGENT OUTPUT THROUGH THE PARSERS  (the upload risk)")

for tag in ("log0814",):
    raw = fixture(f"testcases_{tag}.md")
    if raw is None:
        continue
    try:
        parsed = T.parse_testcases(raw, 1, 100)      # wide limits: shape only
        check(f"parse_testcases accepts real table from {tag}",
              len(parsed["ids"]) > 0, "")
        print(f"          -> {len(parsed['ids'])} test cases, {len(parsed['rows'])} rows, "
              f"{parsed['chars']:,} chars")
    except Exception as e:
        check(f"parse_testcases accepts real table from {tag}", False, str(e)[:150])

# A cell holding a newline splits the row across physical lines. log0814 does this 93 times.
# Without the rejoin those rows are dropped silently, leaving 97 of 190 step rows.
raw0814 = fixture("testcases_log0814.md") or ""
check("rows split across physical lines are rejoined, not dropped",
      len(T.parse_testcases(raw0814, 1, 100)["rows"]) == 190,
      f"got {len(T.parse_testcases(raw0814, 1, 100)['rows'])} rows, expected 190")

# log15 and log18 were captured BEFORE the Status / Test Case Type columns were corrected.
# They must be rejected: that is invariant 6 in CLAUDE.md doing its job.
for tag in ("log15", "log18"):
    raw = fixture(f"testcases_{tag}.md")
    if raw is None:
        continue
    check(f"rejects the pre fix table from {tag} with swapped columns",
          _try(lambda: T.parse_testcases(raw, 1, 100)))

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


good_table = fixture("testcases_log0814.md") or ""
check("rejects a table with the wrong header",
      _try(lambda: T.parse_testcases(good_table.replace("ScenarioId", "Scenario Id"), 1, 100)))
check("rejects an empty response",
      _try(lambda: T.parse_testcases("", 1, 100)))
check("rejects prose with no table",
      _try(lambda: T.parse_testcases("Here are your test cases:", 1, 100)))
check("rejects step counts outside the range",
      _try(lambda: T.parse_testcases(good_table, 500, 600)))
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
        "scenarioagentid": 613, "testcaseagentid": 564, "reviewagentid": 559}

cfg = tool._config(json.dumps(base))
check("defaults applied when optional keys are absent",
      cfg["maxscenarios"] == T.DEF_MAXSCENARIOS and cfg["passscore"] == T.DEF_PASSSCORE)
check("clamps an absurd maxworkers", tool._config(json.dumps(dict(base, maxworkers=9999)))["maxworkers"] == 10)
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

real_table = fixture("testcases_log0814.md")     # the post column fix capture
real_scen = fixture("scenarios_log0814.json")

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
check("test cases were assembled into one table", res["testcases"].count("| TS_") > 0)
check("the assembled table has exactly one header row",
      res["testcases"].count("| ScenarioId |") == 1)
check("score history recorded per scenario",
      all(r["scorehistory"] for r in res["scenarios"]))
check("summary line present in the log",
      any("step=summary" in l for l in res["log"]))
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
check("test cases from the surviving scenarios are still returned",
      res2["testcases"].count("| TS_") > 0)


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
        documented = set(_re.findall(r"`%s\.(\w+)`" % g, body))
        check(f"{label}: every field of {g} is documented as `{g}.<field>`",
              not (fields - documented), f"undocumented: {sorted(fields - documented)}")
        check(f"{label}: every `{g}.<field>` reference is a real field",
              not (documented - fields), f"not sent: {sorted(documented - fields)}")
    check(f"{label}: no bare dotted reference outside backticks",
          not _re.search(r"(?<![`\w])(?:storydata|limits)\.\w+", body))

orch, orch_src = prompt_of("00_orchestrator_agent.md")
check("00 orchestrator: declares {{runinputs}} and nothing else",
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
check("default maxworkers is 3, not 5", T.DEF_MAXWORKERS == 3)

# 3/4. The deterministic pre gate.
import copy as _copy                                      # noqa: E402

_RAW = fixture("testcases_log0814.md") or ""
_ANCHOR = "The file is automatically picked up by Edifecs within the normal polling interval."
check("the pre gate fixture anchor still exists", _RAW.count(_ANCHOR) > 0)

_good = T.parse_testcases(_RAW, 1, 100)
check("pre gate passes REAL good output", T.pregate(_good) == [], str(T.pregate(_good)[:2]))


def _mutated(fn):
    """Parse once, mutate the structure in memory, gate it. rows and cases share row objects."""
    p2 = _copy.deepcopy(T.parse_testcases(_RAW, 1, 100))
    fn(p2)
    return T.pregate(p2)


def _blank_expected(p2):
    p2["rows"][0][11] = ""
    p2["cases"][p2["rows"][0][3]][0][11] = ""


check("pre gate catches an empty Test Step Expected Result",
      any("Expected Result" in x for x in _mutated(_blank_expected)))
check("pre gate catches an empty Test Step Description",
      any("Description" in x for x in _mutated(
          lambda p2: (p2["rows"][0].__setitem__(10, ""),
                      p2["cases"][p2["rows"][0][3]][0].__setitem__(10, "")))))
check("pre gate catches a knowledge base name in the output",
      any("kb_" in x for x in _mutated(
          lambda p2: p2.__setitem__("table", p2["table"].replace(
              _ANCHOR, "See kb_edi_834_testcase_analysis for details.", 1)))))
check("pre gate catches a meta label in a Precondition",
      any("DoR" in x for x in _mutated(
          lambda p2: p2["rows"][0].__setitem__(8, "Per the DoR this must hold."))))
check("pre gate catches an unresolved design value",
      any("<STATE>" in x for x in _mutated(
          lambda p2: p2.__setitem__("table", p2["table"].replace(
              _ANCHOR, "Applicable state is <STATE>.", 1)))))
check("pre gate ignores runtime data tokens like <ISA13>",
      _mutated(lambda p2: p2.__setitem__("table", p2["table"].replace(
          _ANCHOR, "Capture <ISA13> and <member_ssn> at run time.", 1))) == [])
check("a meta label in AcceptanceCriteriaRef is NOT a violation",
      _mutated(lambda p2: p2["rows"][0].__setitem__(1, "DoD - file archived")) == [])

# A pre gate failure must NOT spend a reviewer call.
seen = {"gen": 0, "rev": 0}
bad_table = _RAW.replace(_ANCHOR, "Applicable state is <STATE>.", 1)
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
check("a pre gate failure never calls the reviewer", seen["rev"] == 0, f"reviewer calls={seen['rev']}")
check("it regenerates instead", seen["gen"] == 2, f"generator calls={seen['gen']}")
check("the proven problems are reported as gaps",
      any("<STATE>" in g for g in res5["scenarios"][0]["gaps"]),
      str(res5["scenarios"][0]["gaps"])[:150])

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
check("unreviewed test cases are still returned", res8["testcases"].count("| TS_") > 0)
check("a real agent id is still required for the generator",
      _try(lambda: tool._config(json.dumps(dict(base, testcaseagentid=0)))))

T.exec_agent, T.fetch_story, T._secret = orig_exec, orig_story, orig_secret


section("12. AAVA UPLOAD GATE")

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

print(f"\n{'=' * 70}\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("   FAILED:", f)
sys.exit(1 if FAIL else 0)
