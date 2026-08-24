"""AavaTestGenOrchestrator — Azure DevOps story to EDI 834 test cases, in one tool call.

TWO-STAGE COPY (testsenarion_selfhealing). This file is the successor to
testgen_orchestrator/tool/AavaTestGenOrchestrator.py, which stays frozen as the in-production
working copy. Everything there is here unchanged, plus a `stage` key in runinputs:

    stage=all         the whole pipeline in one call — byte-compatible with the working copy
    stage=scenarios   fetch story -> scenarios -> scenario review (one rework) -> write the
                      handoff file scenarios/<storyid>/scenarios.json to GitHub
    stage=testcases   read that handoff file (no ADO call) -> threads -> table -> publish

The handoff travels through a GitHub file at a path derived from the story id alone, so no
model and no workflow binding ever relays scenario data between the two stages.

Owns the whole pipeline: fetch the story, generate scenarios, then generate and review test
cases one scenario at a time, in parallel, healing each scenario independently.

Design notes that matter when reading this file:

  * State never leaves memory. Scenarios, verdicts and the round counter are Python objects,
    so nothing has to be copied verbatim by a model and nothing can fail to bind.
  * Control flow is code. The self heal loop is a `while`, not an agent deciding to call a
    tool.
  * A thread cannot fail the run. Every scenario is wrapped; an exception becomes a record
    with status "failed" and the run still returns.
  * Secrets are resolved here, never placed in an agent's userInputs, and scrubbed from
    every log line.

Config keys are flat, lowercase, no separators — the convention used across this codebase
(jiraid, maxattempts, preferruninputs) and required by the AAVA {{variable}} rule.

Full design: ../DESIGN.md
"""

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Type

import requests
from pydantic import BaseModel, Field
from crewai.tools import BaseTool

try:
    import AVASecret
except ImportError:
    AVASecret = None  # type: ignore


# ── defaults ────────────────────────────────────────────────────────────────
DEF_AAVA_BASE = ("https://aava-core-api-agents-svc"
                 ".redtree-f4541a84.eastus.azurecontainerapps.io")
DEF_ADO_BASE = "https://dev.azure.com"

# maxscenarios has no default: it sizes the whole run, so the caller must pass it.
DEF_TESTCASESPERSCENARIO = 3
DEF_STEPSMIN = 15
DEF_STEPSMAX = 18
DEF_MAXHEALROUNDS = 3
DEF_PASSSCORE = 90
DEF_HARDSTOPSCORE = 50        # below this, healing does not help; stop and report
# maxworkers is not an input: it always equals maxscenarios. Anything lower splits the run
# into waves and multiplies wall clock by the wave count, which is what breaches the 240s
# ACA ceiling.
# The hosting ceiling is 500s end to end (was 240s until 2026-08-20). This sits below it on
# purpose: the tool must stop on its own terms and return what it has, because a severed
# connection returns nothing at all. 500 minus ~50s of margin for the last poll interval,
# assembly and publish.
DEF_DEADLINESECONDS = 450
DEF_SCENARIOPASSSCORE = 70     # scenario reviewer approval threshold, passed via reviewinputs
# maxagentcalls has no constant: it sizes itself from the run shape in _config, so the
# caller never has to calculate it. See the formula there.
DEF_USERPRINCIPAL = "aava@testgen"   # audit identity when the caller supplies none

# Where publish=true pushes the run's files. Same folder layout run_local.py writes
# locally, so platform runs and local runs leave the same trail.
GITHUB_API = "https://api.github.com"
DEF_GITHUBREPO = "Hrishi1312/self_healing"
DEF_GITHUBBRANCH = "main"
# The stage handoff file. A STABLE path derived from the story id alone, so the testcases
# stage needs nothing from the scenarios stage except the story id it already has.
HANDOFF_DIR = "scenarios"

# This deployment executes agents ASYNCHRONOUSLY: the submit returns a job id, and the answer
# is fetched separately. Submit accepts multipart/form-data ONLY (JSON gets HTTP 415).
SUBMIT_PATH = "/agents/execute/agent-executions"
POLL_PATH = "/agents/execute/history/execution"
TERMINAL = {"SUCCESS", "FAILED", "ERROR", "CANCELLED", "CANCELED"}
# A 404 while polling just means the result is not recorded yet; keep waiting.
POLL_RETRY_STATUSES = {0, 404, 429, 500, 502, 503, 504}

# Each request is short now, so the old 240s worry does not apply per call.
HTTP_TIMEOUT = 60
# Poll interval grows so a two minute generation costs ~8 polls, not ~40. maxRpm on these
# agents is 20, and eight scenarios polling in lockstep would spend that on nothing.
POLL_START, POLL_MAX, POLL_GROWTH = 5.0, 20.0, 1.5
ADO_TIMEOUT = (15, 45)

COLUMNS = ["ScenarioId", "AcceptanceCriteriaRef", "Name", "Id", "Attachments", "Status",
           "Test Case Type", "Description", "Precondition", "Test Step #",
           "Test Step Description", "Test Step Expected Result", "Test Step Attachment"]

# Field names INSIDE a scenario object, as the scenario generator emits them. These are
# camelCase because the agent prompt is carried verbatim from production and specifies
# camelCase, and real logged output confirms it. Do not "tidy" these to lowercase: the
# config keys are lowercase, the scenario object fields are not, and they are different
# namespaces.
SCENARIO_KEYS = ["scenarioId", "title", "descriptionRef", "acceptanceCriteriaRef",
                 "dorRef", "dodRef", "type", "description", "priority"]

TYPES = {"Positive", "Negative", "Edge"}
PRIORITIES = {"High", "Medium", "Low"}
CATEGORIES = {"Functional", "Regression"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── logging ─────────────────────────────────────────────────────────────────
class _Log:
    """Thread safe structured log. The platform activity log carries no timestamps of its
    own, so every line here stamps its own UTC time."""

    def __init__(self):
        self._lines: List[str] = []
        self._lock = threading.Lock()
        self._secrets: List[str] = []

    def guard(self, *values: Optional[str]) -> None:
        """Register secret values so they can never appear in a log line."""
        for v in values:
            if v and len(str(v)) > 6:
                self._secrets.append(str(v))

    def _scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def line(self, step: str, **fields: Any) -> None:
        parts = [f"[ORCH] ts={_now()}", f"step={step}"]
        parts += [f"{k}={v}" for k, v in fields.items() if v is not None and v != ""]
        entry = self._scrub(" ".join(parts))
        with self._lock:
            self._lines.append(entry)
        # flush on every line. AAVA surfaces no logging output from a tool, only stdout, and
        # stdout to a pipe is block buffered: a run the platform kills at its timeout never
        # exits cleanly, so anything still sitting in the buffer is lost. Threads also
        # interleave mid line without it.
        print(entry, flush=True)

    def dump(self) -> List[str]:
        with self._lock:
            return list(self._lines)


# ── budget ──────────────────────────────────────────────────────────────────
class _Budget:
    """A wall clock deadline and a hard cap on agent calls, shared across threads.

    The platform stops a run without warning. This makes the tool stop first, on its own
    terms, so it can return what it has instead of being killed mid flight.
    """

    def __init__(self, deadlineseconds: float, maxagentcalls: int):
        self.deadline = float(deadlineseconds)
        self.maxcalls = int(maxagentcalls)
        self.calls = 0
        self._t0 = time.monotonic()
        self._lock = threading.Lock()

    def remaining(self) -> float:
        return self.deadline - (time.monotonic() - self._t0)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def allows(self, need_seconds: float = 0.0) -> bool:
        with self._lock:
            return self.calls < self.maxcalls and self.remaining() > need_seconds

    def take(self) -> bool:
        with self._lock:
            if self.calls >= self.maxcalls or self.remaining() <= 0:
                return False
            self.calls += 1
            return True


# ── http ────────────────────────────────────────────────────────────────────
def _http(method: str, url: str, log: _Log, headers: Dict[str, str] = None,
          json_body: Any = None, timeout: Any = HTTP_TIMEOUT,
          form: Dict[str, str] = None) -> Tuple[int, Any]:
    """ONE attempt. Returns (status, parsed_body); status 0 means no response, which is how a
    severed long request shows up.

    Deliberately no retry, matching CGSelfhealingV3Tool. The platform severs an agent call at
    roughly 265 seconds, so a blind retry of a timed out call costs another full timeout: three
    attempts would burn about 800 seconds inside a single scenario and take the whole run's
    budget with it. Retrying is the heal loop's job, under the budget, not this function's.
    """
    try:
        # files= with a None filename is how requests encodes plain multipart fields. The
        # submit endpoint rejects an application/json body outright.
        kw = {"files": {k: (None, v) for k, v in form.items()}} if form else {"json": json_body}
        r = requests.request(method, url, headers=headers or {}, timeout=timeout, **kw)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except requests.RequestException as e:
        return 0, str(e)


def _err_detail(body: Any, limit: int = 300) -> str:
    """Pull the human reason out of an error body, whatever shape the service used.

    Services disagree on the key, so try them all and fall back to the raw payload. A bare
    "http 502" with no reason is useless when the run is on someone else's platform and you
    cannot reproduce it.
    """
    if isinstance(body, dict):
        for k in ("message", "error", "detail", "title", "reason", "errorMessage"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                return v[:limit]
        errs = body.get("errors")
        if isinstance(errs, list) and errs:
            return str(errs[0])[:limit]
        data = body.get("data")
        if isinstance(data, dict):
            inner = _err_detail(data, limit)
            if inner:
                return inner
        return json.dumps(body)[:limit]
    return str(body or "")[:limit]


# ── secrets ─────────────────────────────────────────────────────────────────
def _secret(key: str, fallback: str = "") -> str:
    """AVASecret first, the argument only as a local development fallback."""
    if AVASecret is not None:
        try:
            v = AVASecret.getValue(key)
            if v:
                return str(v)
        except Exception:
            pass
    return fallback or ""


# ── azure devops ────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    """Strip markup once, here, so no agent ever has to. Keeps list items on their own line
    because the acceptance criteria arrive as an ordered list."""
    if not html:
        return ""
    text = re.sub(r"</li\s*>", "\n", html, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6])\s*>", "\n", text, flags=re.I)
    text = _TAG.sub("", text)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(a, b)
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def fetch_story(cfg: Dict[str, Any], pat: str, log: _Log) -> Dict[str, Any]:
    """Plain REST. This was an LLM step in the old pipeline and never needed to be."""
    import base64
    org, project, story = cfg["adoorg"], cfg["adoproject"], cfg["adostoryid"]
    url = (f"{DEF_ADO_BASE}/{org}/{project}/_apis/wit/workitems/{story}?api-version=7.0")
    auth = base64.b64encode(f":{pat}".encode()).decode()
    t0 = time.monotonic()
    try:
        r = requests.get(url, headers={"Authorization": f"Basic {auth}",
                                       "Accept": "application/json"}, timeout=ADO_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"azure devops unreachable: {e}")
    if r.status_code == 401:
        raise RuntimeError("azure devops authentication failed, check the pat scope")
    if r.status_code == 404:
        raise RuntimeError(f"work item {story} not found in {org}/{project}")
    if r.status_code != 200:
        raise RuntimeError(f"azure devops returned http {r.status_code}")

    fields = (r.json() or {}).get("fields", {})
    ac = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or ""
    if not ac:
        for k, v in fields.items():
            if "acceptance" in k.lower() and v:
                ac = v
                break
    out = {
        "storyid": str(story),
        "title": (fields.get("System.Title") or "").strip(),
        "description": _html_to_text(fields.get("System.Description") or ""),
        "acceptancecriteria": _html_to_text(ac),
    }
    if not out["title"]:
        raise RuntimeError("story has no title, nothing to generate from")
    if not out["description"] and not out["acceptancecriteria"]:
        raise RuntimeError("story has neither description nor acceptance criteria")
    log.line("fetch", story=story, titlechars=len(out["title"]),
             descchars=len(out["description"]), acchars=len(out["acceptancecriteria"]),
             ms=int((time.monotonic() - t0) * 1000))
    return out


# ── agent execution ─────────────────────────────────────────────────────────
def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _output_text(payload: Any) -> str:
    """The agent's answer, always as text.

    The poll endpoint returns `output` either as a raw string or as an already decoded object.
    Every parser downstream takes text and does its own json.loads, so normalise back to text
    rather than making each of them handle both.
    """
    out = payload.get("output") if isinstance(payload, dict) else None
    if out is None and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            out = data.get("output")
    if out is None:
        return ""
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def exec_agent(agentid: int, userinputs: Dict[str, Any], cfg: Dict[str, Any],
               token: str, budget: _Budget, log: _Log, label: str) -> Tuple[str, int]:
    """Run one sub agent and return (output_text, elapsed_ms).

    Two steps, because this deployment is asynchronous:

      1. POST /agents/execute/agent-executions   multipart/form-data
         {agentId, executionId, user, userInputs}  ->  {"data": {"agentExecutionId": ...}}
         A "SUCCESS" here means ACCEPTED, not finished.
      2. GET  /agents/execute/history/execution?execution_id=...
         polled until `status` is terminal; the answer is the `output` field.

    Only the submit spends budget. Polling is free: counting each poll as an agent call would
    exhaust maxagentcalls inside a single scenario.
    """
    if not budget.take():
        raise RuntimeError("budget exhausted before call")
    base = cfg["aavabaseurl"].rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    # x-realm-id disabled: the platform rejected realmid 4 for this user ("Realm ID 4 is
    # invalid for user"). Re-enable once the correct realm is confirmed.
    # if cfg.get("realmid"):
    #     headers["x-realm-id"] = str(cfg["realmid"])
    eid = str(uuid.uuid4())
    t0 = time.monotonic()

    status, payload = _http("POST", base + SUBMIT_PATH, log, headers, form={
        "agentId": str(int(agentid)),
        "executionId": eid,
        "user": cfg.get("userprincipal", ""),
        "userInputs": json.dumps(userinputs, ensure_ascii=False),
    })
    if status != 200:
        why = _err_detail(payload)
        log.line("agenterror", label=label, agentid=agentid, phase="submit", status=status,
                 ms=int((time.monotonic() - t0) * 1000), realm=cfg.get("realmid") or "none",
                 user=cfg.get("userprincipal") or "none", why=why[:200] or None)
        raise RuntimeError(f"{label}: agent {agentid} submit returned http {status}"
                           + (f" — {why[:200]}" if why else ""))
    # Prefer the id the server echoes back; fall back to the one we generated.
    server_eid = ((payload or {}).get("data") or {}).get("agentExecutionId") \
        if isinstance(payload, dict) else None
    execid = str(server_eid or eid)

    url = f"{base}{POLL_PATH}?execution_id={execid}"
    wait, last = POLL_START, ""
    while True:
        left = budget.remaining()
        if left <= 0:
            raise RuntimeError(f"{label}: agent {agentid} still {last or 'running'} when the "
                               f"budget ran out (execution {execid})")
        time.sleep(min(wait, max(0.5, left)))
        wait = min(wait * POLL_GROWTH, POLL_MAX)
        pstatus, ppayload = _http("GET", url, log, headers)
        if pstatus != 200:
            if pstatus in POLL_RETRY_STATUSES:
                continue                       # not recorded yet, or a transient blip
            why = _err_detail(ppayload)
            log.line("agenterror", label=label, agentid=agentid, phase="poll", status=pstatus,
                     execution=execid, why=why[:200] or None)
            raise RuntimeError(f"{label}: agent {agentid} poll returned http {pstatus}"
                               + (f" — {why[:200]}" if why else ""))
        last = str((ppayload or {}).get("status") or "").upper() \
            if isinstance(ppayload, dict) else ""
        if last in TERMINAL:
            break

    ms = int((time.monotonic() - t0) * 1000)
    if last != "SUCCESS":
        why = _err_detail(ppayload)
        log.line("agenterror", label=label, agentid=agentid, phase="poll", status=last,
                 execution=execid, ms=ms, why=why[:200] or None)
        raise RuntimeError(f"{label}: agent {agentid} finished {last} (execution {execid})")

    out = _output_text(ppayload)
    if not out:
        log.line("agenterror", label=label, agentid=agentid, phase="poll", status=last,
                 execution=execid, ms=ms,
                 why="terminal SUCCESS but no output field; payload=" + _err_detail(ppayload, 160))
        raise RuntimeError(f"{label}: agent {agentid} succeeded with an empty output")
    # No log line here on purpose: the caller logs one line carrying this ms plus the thing you
    # actually want to see, so a run reads as one line per step, not two.
    return out, ms


# ── parsers, one per boundary ───────────────────────────────────────────────
def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_scenarios(raw: str, maxscenarios: int) -> List[Dict[str, Any]]:
    """Validate every field before anything downstream sees it. A malformed array is
    regenerated, not passed on."""
    text = _strip_fences(raw)
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1:
        data = json.loads(text[start:end + 1])
    else:
        # Seen with maxscenarios=1: the generator drops the array wrapper and emits a
        # single scenario object instead of a one-element array.
        ostart, oend = text.find("{"), text.rfind("}")
        if ostart == -1 or oend == -1:
            raise ValueError("response contains no JSON array")
        data = [json.loads(text[ostart:oend + 1])]
    if not isinstance(data, list) or not data:
        raise ValueError("scenario array is empty")

    seen, out = set(), []
    for i, sc in enumerate(data):
        if not isinstance(sc, dict):
            raise ValueError(f"scenario {i} is not an object")
        missing = [k for k in SCENARIO_KEYS if k not in sc]
        if missing:
            raise ValueError(f"scenario {i} missing keys: {', '.join(missing)}")
        sid = str(sc["scenarioId"]).strip()
        if not re.match(r"^TS_\d+$", sid):
            raise ValueError(f"scenario {i} scenarioid '{sid}' is not TS_ followed by digits")
        if sid in seen:
            raise ValueError(f"duplicate scenarioid {sid}")
        seen.add(sid)
        if str(sc["type"]).strip() not in TYPES:
            raise ValueError(f"{sid} type '{sc['type']}' is not Positive, Negative or Edge")
        if str(sc["priority"]).strip() not in PRIORITIES:
            raise ValueError(f"{sid} priority '{sc['priority']}' is not High, Medium or Low")
        # dorRef and dodRef may legitimately be empty; the story has no definition of ready.
        out.append({k: sc[k] for k in SCENARIO_KEYS})

    order = {"High": 0, "Medium": 1, "Low": 2}
    out.sort(key=lambda x: order.get(str(x["priority"]).strip(), 3))
    return out[:maxscenarios]


# Field names in the generator's JSON, lowercase with no separators, in column order.
TC_KEYS = ["scenarioid", "acceptancecriteriaref", "name", "id", "attachments", "status",
           "testcasetype", "description", "precondition"]
STEP_KEYS = ["no", "description", "expected", "attachment"]


def _cell(v: Any) -> str:
    """One table cell. A pipe or a newline inside a value would break the row, so both are
    neutralised here rather than trusted to the model."""
    return _WS.sub(" ", str(v if v is not None else "")).replace("|", "/").strip()


def expand_testcases(raw: str) -> str:
    """Nested JSON from the generator to the 13 column markdown table.

    The generator emits each test case ONCE with its steps as an array, instead of repeating
    the nine header columns on every step row. Measured on real output, those repeats were
    64.6% of all characters, so this halves what the model has to write. The table itself is
    unchanged: the tool expands it here, so the deliverable still matches the 13 column
    contract byte for byte.
    """
    text = _strip_fences(raw)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("response contains no JSON array of test cases")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list) or not data:
        raise ValueError("test case array is empty")

    out = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for i, tc in enumerate(data):
        if not isinstance(tc, dict):
            raise ValueError(f"test case {i} is not an object")
        missing = [k for k in TC_KEYS if k not in tc]
        if missing:
            raise ValueError(f"test case {i} missing keys: {', '.join(missing)}")
        steps = tc.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{tc.get('id', 'test case ' + str(i))} has no steps array")
        head = [_cell(tc[k]) for k in TC_KEYS]
        for n, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                raise ValueError(f"{tc['id']} step {n} is not an object")
            for k in ("description", "expected"):
                if k not in st:
                    raise ValueError(f"{tc['id']} step {n} missing '{k}'")
            out.append("| " + " | ".join(head + [
                _cell(st.get("no") or n), _cell(st["description"]), _cell(st["expected"]),
                _cell(st.get("attachment") or "None")]) + " |")
    return "\n".join(out)


def read_testcases(raw: str, stepsmin: int, stepsmax: int) -> Dict[str, Any]:
    """Accept either shape. Nested JSON is what the generator emits now; a markdown table is
    still accepted so an older agent, or a model that ignores the format, is not a hard fail."""
    head = _strip_fences(raw).lstrip()[:1]
    return parse_testcases(expand_testcases(raw) if head == "[" else raw, stepsmin, stepsmax)


def parse_testcases(raw: str, stepsmin: int, stepsmax: int) -> Dict[str, Any]:
    """Parse the markdown table and check its shape. Returns rows plus a per test case index."""
    text = _strip_fences(raw)
    # A cell holding a newline splits one table row across physical lines. Real output does
    # this whenever acceptanceCriteriaRef carries two AC lines. Rejoin before splitting on
    # pipes, or the row is dropped and its steps vanish without any check noticing.
    rows: List[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("|"):
            rows.append(s)
        elif s and rows and rows[-1].count("|") < len(COLUMNS) + 1:
            rows[-1] += " " + s
    if len(rows) < 3:
        raise ValueError("no markdown table found")
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header = cells[0]
    if header != COLUMNS:
        raise ValueError(f"header has {len(header)} columns, expected the 13 standard columns")

    body = [c for c in cells[2:] if len(c) == len(COLUMNS)]
    if not body:
        raise ValueError("table has a header but no data rows")

    cases: Dict[str, List[List[str]]] = {}
    current = None
    for row in body:
        if row[3]:
            current = row[3]
        if current:
            cases.setdefault(current, []).append(row)
    if not cases:
        raise ValueError("no test case id found in the Id column")
    for tid in cases:
        if not re.match(r"^TC[_-]?\w*\d+$", tid):
            raise ValueError(f"test case id '{tid}' does not look like TC followed by digits")

    # ── DISABLED 2026-08-18 — moved to the agent instructions ──────────────
    # Step count, Status and Test Case Type are stated in agent 02 (OUTPUT VOLUME
    # DISCIPLINE, rule 7a) and enforced by agent 03 (checks 8 and 9). Keeping them here
    # too put the same rule in two places that could drift. Re-enable if a run ships a
    # swapped Status column or a short test case: the failure is silent, and this is the
    # only thing that would have caught it before the workbook.
    #
    # problems = []
    # for tid, tcrows in cases.items():
    #     n = len(tcrows)
    #     if n < stepsmin or n > stepsmax:
    #         problems.append(f"{tid} has {n} steps, expected {stepsmin} to {stepsmax}")
    #     st = {r[5] for r in tcrows if r[5]}
    #     if st - TYPES:
    #         problems.append(f"{tid} Status holds {sorted(st - TYPES)}, expected Positive, Negative or Edge")
    #     cat = {r[6] for r in tcrows if r[6]}
    #     if cat - CATEGORIES:
    #         problems.append(f"{tid} Test Case Type holds {sorted(cat - CATEGORIES)}, expected Functional or Regression")
    # if problems:
    #     raise ValueError("; ".join(problems))
    # ── end disabled ───────────────────────────────────────────────────────

    # Rebuild the table from the parsed rows rather than handing back the raw text. The raw
    # text still contains any physically split rows this function just rejoined, so returning
    # it would put the split rows straight back into the assembled output and the row counts
    # would disagree with what was parsed. One row per line, always.
    clean = "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(COLUMNS)]
                      + ["| " + " | ".join(r) + " |" for r in body])
    return {"header": header, "rows": body, "cases": cases, "table": clean,
            "chars": len(clean), "ids": list(cases.keys())}


def parse_verdict(raw: str, known_ids: List[str]) -> Dict[str, Any]:
    text = _strip_fences(raw)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("response contains no JSON object")
    v = json.loads(text[start:end + 1])
    scores = v.get("scores")
    if not isinstance(scores, list) or not scores:
        raise ValueError("verdict has no scores array")
    for s in scores:
        if not isinstance(s, dict) or "id" not in s or "score" not in s:
            raise ValueError("a score entry is missing id or score")
        if s["id"] not in known_ids:
            raise ValueError(f"verdict scores unknown test case id {s['id']}")
        if not isinstance(s["score"], int) or not 0 <= s["score"] <= 100:
            raise ValueError(f"{s['id']} score is not an integer 0 to 100")
    return v


# ── cheap deterministic gate, no llm ────────────────────────────────────────
# Disabled 2026-08-18 as duplication of agent 02 / agent 03 checks 3, 6, 7; re-enabled
# 2026-08-19 after the 640764 comparison run shipped "DoD" meta labels in 5 preconditions —
# exactly the silent slip the disable note warned about. See DESIGN.md section 8.
# Checks 3, 6 and 7 of the reviewer's checklist are literal string tests. Doing them here is
# strictly better than paying an Opus call to do them: free, deterministic, and immune to the
# evidence-rule ambiguity that had the reviewer inventing violations. Only work that survives
# this gate is worth a reviewer call.
SOURCE_NAMES = ["kb_", "EDI and FACETS Schema 2", "Facets 834", "EDIFECS Full with AUX 834"]
META_LABELS = ["DoR", "DoD", "Definition of Ready", "Definition of Done",
               "descriptionRef", "dorRef", "dodRef", "per the AC", "as referenced in"]
# Name, Description, Precondition, Test Step Description, Test Step Expected Result.
# ScenarioId and AcceptanceCriteriaRef are exempt, as they are in the reviewer's own rules.
_META_COLS = [2, 7, 8, 10, 11]


def pregate(parsed: Dict[str, Any]) -> List[str]:
    """Return the problems a machine can prove. Empty means it is worth reviewing.

    Deliberately NOT here: the angle-bracket rule. It lives in agent 02 (write
    `[TEST DATA: ...]`, never angle brackets) and agent 03 check 5. Duplicating it in Python
    put the same rule in two places that could drift, and the instruction is now unambiguous.
    If the generator does slip, the reviewer catches it. Keep this function to things a
    machine can prove cheaply and the reviewer would otherwise be paid to eyeball.
    """
    problems: List[str] = []

    for tid, rows in parsed["cases"].items():
        for r in rows:
            if not r[10].strip():
                problems.append(f"{tid} step {r[9] or '?'} has an empty Test Step Description")
                break
            if not r[11].strip():
                problems.append(f"{tid} step {r[9] or '?'} has an empty Test Step Expected Result")
                break

    table = parsed["table"]
    for name in SOURCE_NAMES:
        if name in table:
            problems.append(f"the knowledge base or schema name '{name}' appears in the output")
    for row in parsed["rows"]:
        for c in _META_COLS:
            for label in META_LABELS:
                if label in row[c]:
                    problems.append(f"{row[3] or 'a test case'} carries the meta label "
                                    f"'{label}' in {COLUMNS[c]}")
                    break
    seen, unique = set(), []
    for p in problems:                      # one line per distinct fault, not per row
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:20]


# ── one scenario, start to finish, on its own thread ────────────────────────
def process_scenario(scenario: Dict[str, Any], story: Dict[str, Any], cfg: Dict[str, Any],
                     token: str, budget: _Budget, log: _Log) -> Dict[str, Any]:
    """Generate, review and heal one scenario. Never raises: every failure becomes a record
    so that one bad scenario cannot take the run down with it."""
    sid = scenario["scenarioId"]
    rec: Dict[str, Any] = {
        "scenarioid": sid, "title": scenario.get("title", ""), "status": "failed",
        # which test cases the reviewer flagged, and why. Without this the envelope can say
        # "this scenario has a problem" but not "2 of its 3 test cases are fine".
        "flagged": [],
        "scorehistory": [], "finalscore": None, "rounds": 0,
        "testcasecount": 0, "chars": 0, "elapsedms": 0, "gaps": [], "error": None,
        "table": "",
    }
    t0 = time.monotonic()
    passscore = cfg["passscore"]
    parsed: Optional[Dict[str, Any]] = None
    regenerate: List[Dict[str, Any]] = []
    # maxhealrounds 0 means one pass and no regeneration: score it, report it, change nothing.
    passes = max(1, cfg["maxhealrounds"])
    reviewing = int(cfg["reviewagentid"]) > 0          # 0 disables the judge entirely

    try:
        for rnd in range(1, passes + 1):
            if not budget.allows(need_seconds=60):
                rec["status"] = "skipped" if rnd == 1 else rec["status"]
                rec["error"] = "budget exhausted"
                log.line("budget", scenario=sid, round=rnd, note="stopped, budget low")
                break
            rec["rounds"] = rnd

            gen_inputs = {
                "scenario": _j(scenario),
                "storytitle": story["title"],
                "limits": _j({"testcasesperscenario": cfg["testcasesperscenario"],
                                          "stepsmin": cfg["stepsmin"], "stepsmax": cfg["stepsmax"]}),
            }
            if regenerate:
                gen_inputs["regenerate"] = _j(regenerate)   # omitted on the first round
            raw = ""
            try:
                raw, gen_ms = exec_agent(cfg["testcaseagentid"], gen_inputs, cfg, token,
                                         budget, log, f"generate:{sid}")
                parsed = read_testcases(raw, cfg["stepsmin"], cfg["stepsmax"])
            except Exception as e:
                # Log the head of what the agent actually sent. Run 640764_084112 failed
                # 7/7 with "test case array is empty" and the log carried no way to tell
                # whether the model wrote [], the prompt was broken, or the platform
                # truncated the output. Empty when exec_agent itself raised.
                log.line("generate", scenario=sid, round=rnd, error=str(e)[:120],
                         raw=_WS.sub(" ", raw)[:200] or None)
                regenerate = [{"id": "all", "gaps": [f"previous attempt was rejected: {e}"]}]
                rec["error"] = str(e)[:300]
                continue

            rec["table"] = parsed["table"]
            rec["chars"] = parsed["chars"]
            rec["testcasecount"] = len(parsed["ids"])
            log.line("generate", scenario=sid, round=rnd, tc=len(parsed["ids"]),
                     ids=",".join(parsed["ids"]), chars=parsed["chars"], ms=gen_ms,
                     regen=len(regenerate) or None)

            # Re-enabled 2026-08-19: the 640764 comparison run shipped meta labels ("DoD"
            # in 5 preconditions) that this gate catches for free. Disabling it (2026-08-18)
            # was the drift the code comment warned about. Only work that survives this gate
            # is worth a reviewer call; its problems feed the regenerate round verbatim.
            problems = pregate(parsed)
            if problems:
                log.line("pregate", scenario=sid, round=rnd, failed=len(problems),
                         first=problems[0][:90])
                rec["gaps"] = problems
                rec["status"] = "unhealed"
                if rnd >= passes:
                    break
                regenerate = [{"id": "all", "gaps": problems}]
                continue

            if not reviewing:
                # Degraded mode: no judge configured. The pre gate is the whole gate.
                rec["status"] = "unreviewed"
                rec["gaps"] = []
                rec["flagged"] = []
                log.line("review", scenario=sid, round=rnd, note="skipped, reviewagentid=0")
                break

            rev_inputs = {
                "scenario": _j(scenario),
                "testcases": parsed["table"],
                "limits": _j({"passscore": passscore, "stepsmin": cfg["stepsmin"],
                                          "stepsmax": cfg["stepsmax"],
                                          "testcasesperscenario": cfg["testcasesperscenario"]}),
            }
            raw = ""
            try:
                raw, rev_ms = exec_agent(cfg["reviewagentid"], rev_inputs, cfg, token,
                                         budget, log, f"review:{sid}")
                verdict = parse_verdict(raw, parsed["ids"])
            except Exception as e:
                log.line("review", scenario=sid, round=rnd, error=str(e)[:120],
                         raw=_WS.sub(" ", raw)[:200] or None)
                rec["error"] = str(e)[:300]
                continue

            scores = verdict["scores"]
            failing = [s for s in scores if not s.get("pass")]
            batchscore = int(verdict.get("batchscore") or min(s["score"] for s in scores))
            rec["scorehistory"].append(batchscore)
            rec["finalscore"] = batchscore
            rec["gaps"] = [g for s in failing for g in (s.get("gaps") or [])]
            # `reason` is the reviewer's own plain English line for a person; `gaps` is the
            # precise evidence for the generator. Prefer the first, fall back to the second so
            # a reviewer that has not been updated still says something useful.
            rec["flagged"] = [{"id": s["id"],
                               "why": (str(s.get("reason") or "").strip()
                                       or (s.get("gaps") or [""])[0])[:120]}
                              for s in failing]
            log.line("review", scenario=sid, round=rnd, score=batchscore,
                     passed=f"{len(scores) - len(failing)}/{len(scores)}",
                     failing=",".join(s["id"] for s in failing) or None, ms=rev_ms)

            if not failing:
                rec["status"] = "approved"
                break

            # Hard stop: this far below the bar the output is wrong, not rough, and another
            # round spends budget to arrive at the same place. Report it for a human instead.
            if batchscore < cfg["hardstopscore"]:
                rec["status"] = "hardstop"
                log.line("hardstop", scenario=sid, round=rnd, score=batchscore,
                         floor=cfg["hardstopscore"], note="too low to heal, stopping")
                break

            if cfg["stoponstagnation"] and len(rec["scorehistory"]) >= 2 \
                    and rec["scorehistory"][-1] <= rec["scorehistory"][-2]:
                rec["status"] = "stagnant"
                log.line("stagnant", scenario=sid, round=rnd, scores=rec["scorehistory"])
                break

            if rnd >= passes:
                rec["status"] = "unhealed"
                break

            # Targeted repair: only the test cases that failed, with their quoted gaps.
            regenerate = [{"id": s["id"], "gaps": s.get("gaps") or []} for s in failing]

    except Exception as e:                                   # defensive: never escape a thread
        rec["status"] = "failed"
        rec["error"] = str(e)[:300]
        log.line("threaderror", scenario=sid, error=str(e)[:160])

    rec["elapsedms"] = int((time.monotonic() - t0) * 1000)
    log.line("result", scenario=sid, status=rec["status"],
             scores=json.dumps(rec["scorehistory"]), rounds=rec["rounds"],
             tc=rec["testcasecount"], ms=rec["elapsedms"])
    return rec


# ── cross batch check, deterministic, no llm ────────────────────────────────
def cross_batch_check(records: List[Dict[str, Any]], scenarios: List[Dict[str, Any]]) -> List[str]:
    warnings: List[str] = []
    covered = {r["scenarioid"] for r in records if r["testcasecount"] > 0}
    for s in scenarios:
        if s["scenarioId"] not in covered:
            warnings.append(f"{s['scenarioId']} produced no test cases")

    seen: Dict[str, str] = {}
    statuses: Dict[str, int] = {}
    for r in records:
        for row in (r.get("table") or "").split("\n"):
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            # "---" is the markdown separator row: 13 cells of dashes, which otherwise
            # collides with every other scenario's separator and reports itself as a
            # duplicate. Every warning in every run so far was this row.
            if len(cells) != len(COLUMNS) or cells[3] in ("Id", "") or set(cells[3]) <= {"-", ":"}:
                continue
            statuses[cells[5]] = statuses.get(cells[5], 0) + 1
            key = _WS.sub(" ", (cells[2] + "|" + cells[7]).lower()).strip()
            if key and key in seen and seen[key] != r["scenarioid"]:
                warnings.append(f"{cells[3]} looks like a duplicate of a test case in {seen[key]}")
            elif key:
                seen[key] = r["scenarioid"]

    for t in TYPES:
        if statuses.get(t, 0) == 0:
            warnings.append(f"no {t} test cases across the whole set")
    return warnings


# ── github publish, opt in ──────────────────────────────────────────────────
def publish_run(cfg: Dict[str, Any], log: _Log,
                files: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Push the run's files to GitHub via the contents API, one PUT per file.

    A failure is logged and reported in the returned dict, never raised: publishing is a
    convenience and can never fail the run that produced the thing being published.
    """
    import base64
    token = cfg.get("githubtoken") or ""
    if not token:
        log.line("publish", error="publish=true but no githubtoken in runinputs")
        return {"error": "publish=true but no githubtoken in runinputs"}
    repo, branch = cfg["githubrepo"], cfg["githubbranch"]
    folder = "tool_logs/{}_{}".format(
        cfg["adostoryid"], datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    ok, err = 0, None
    for name, content in files:
        status, body = _http(
            "PUT", f"{GITHUB_API}/repos/{repo}/contents/{folder}/{name}", log, headers,
            json_body={"message": f"run log {cfg['adostoryid']}: {name}",
                       "content": base64.b64encode(content.encode("utf-8")).decode(),
                       "branch": branch})
        if status in (200, 201):
            ok += 1
            log.line("publish", file=name, chars=len(content))
        else:
            err = f"{name}: http {status} {_err_detail(body, 120)}".strip()
            log.line("publish", file=name, status=status, error=_err_detail(body, 120))
    out = {"repo": repo, "branch": branch, "path": folder, "files": ok}
    if err:
        out["error"] = err
    return out


# ── scenario review verdict ─────────────────────────────────────────────────
def parse_scenario_verdict(raw: str) -> Dict[str, Any]:
    """The scenario reviewer's verdict: confidence, approved, feedback, gaps, strengths.
    feedback must exist because it is fed back verbatim on the rework round."""
    text = _strip_fences(raw)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("response contains no JSON object")
    v = json.loads(text[start:end + 1])
    conf = v.get("confidence")
    if not isinstance(conf, int) or not 0 <= conf <= 100:
        raise ValueError("confidence is not an integer 0 to 100")
    if not isinstance(v.get("approved"), bool):
        raise ValueError("approved is not a boolean")
    if not str(v.get("feedback") or "").strip():
        raise ValueError("verdict has no feedback")
    v["gaps"] = [str(g) for g in (v.get("gaps") or [])]
    v["strengths"] = [str(s) for s in (v.get("strengths") or [])]
    return {k: v[k] for k in ("confidence", "approved", "feedback", "gaps", "strengths")}


# ── the stage handoff file ──────────────────────────────────────────────────
def _handoff_path(cfg: Dict[str, Any]) -> str:
    return f"{HANDOFF_DIR}/{cfg['adostoryid']}/scenarios.json"


def _gh_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {cfg['githubtoken']}",
            "Accept": "application/vnd.github+json"}


def write_handoff(cfg: Dict[str, Any], log: _Log, story: Dict[str, Any],
                  scenarios: List[Dict[str, Any]],
                  verdict: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Write the handoff file the testcases stage will read. RAISES on failure — unlike
    publishing, the handoff is the scenarios stage's whole deliverable, so a run that cannot
    write it has not done its job."""
    import base64
    path = _handoff_path(cfg)
    url = f"{GITHUB_API}/repos/{cfg['githubrepo']}/contents/{path}"
    content = json.dumps({"story": story, "scenarios": scenarios, "review": verdict,
                          "writtenat": _now()}, indent=2, ensure_ascii=False)
    body = {"message": f"scenarios {cfg['adostoryid']}: stage handoff",
            "content": base64.b64encode(content.encode("utf-8")).decode(),
            "branch": cfg["githubbranch"]}
    # The path is stable, so a rerun overwrites — and the contents API refuses an overwrite
    # without the current file's sha. Fetch it first; a 404 just means first run.
    status, prev = _http("GET", url + f"?ref={cfg['githubbranch']}", log, _gh_headers(cfg))
    if status == 200 and isinstance(prev, dict) and prev.get("sha"):
        body["sha"] = prev["sha"]
    status, resp = _http("PUT", url, log, _gh_headers(cfg), json_body=body)
    if status not in (200, 201):
        raise RuntimeError(f"handoff write failed: http {status} — {_err_detail(resp, 160)}")
    log.line("handoff", action="write", path=path, scenarios=len(scenarios),
             chars=len(content))
    return {"repo": cfg["githubrepo"], "branch": cfg["githubbranch"], "path": path}


def fetch_handoff(cfg: Dict[str, Any], log: _Log) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Read the handoff file the scenarios stage wrote. Returns (story, scenarios), both
    revalidated here — the file is trusted no more than an agent's output would be."""
    import base64
    path = _handoff_path(cfg)
    url = f"{GITHUB_API}/repos/{cfg['githubrepo']}/contents/{path}?ref={cfg['githubbranch']}"
    status, resp = _http("GET", url, log, _gh_headers(cfg))
    if status == 404:
        raise RuntimeError(f"no handoff file at {path} — run the scenarios stage first")
    if status != 200:
        raise RuntimeError(f"handoff read failed: http {status} — {_err_detail(resp, 160)}")
    try:
        data = json.loads(base64.b64decode(
            (resp or {}).get("content") or "").decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"handoff file is not valid JSON: {e}")
    story = data.get("story")
    if not isinstance(story, dict) or not str(story.get("title") or "").strip():
        raise RuntimeError("handoff file has no story block")
    story.setdefault("storyid", cfg["adostoryid"])
    # Same validator the live generator output goes through — a hand-edited file gets the
    # same scrutiny, and a malformed one fails loudly here rather than inside a thread.
    scenarios = parse_scenarios(json.dumps(data.get("scenarios") or []), 20)
    log.line("handoff", action="read", path=path, scenarios=len(scenarios))
    return story, scenarios


# ── crewai surface ──────────────────────────────────────────────────────────
class AavaTestGenOrchestratorSchema(BaseModel):
    """Input schema for AavaTestGenOrchestrator.

    ONE object, not one field per setting. The orchestrator agent is an LLM: every separate
    variable it has to relay is another chance for it to reformat, reorder or drop a value.
    One opaque string is a single copy operation, and the tool does the parsing.
    """

    runinputs: str = Field(
        ...,
        description=(
            "One JSON object carrying every run setting. Keys are lowercase with no "
            "separators: stage (all, scenarios or testcases), adoorg, adoproject, "
            "adostoryid, scenarioagentid, scenarioreviewagentid, scenariopassscore, "
            "testcaseagentid, reviewagentid, maxscenarios, testcasesperscenario, stepsmin, "
            "stepsmax, maxhealrounds, passscore, hardstopscore, stoponstagnation, "
            "deadlineseconds, maxagentcalls, aavabaseurl, realmid, userprincipal, publish, "
            "githubtoken, githubrepo, githubbranch, and for "
            "local testing only adopat and aavatoken. Set maxhealrounds to 0 for a single "
            "pass with no regeneration, reviewagentid to 0 to run without the judge, or "
            "scenarioreviewagentid to 0 to skip the scenario review. Pass "
            "the value through EXACTLY as received, as one opaque string. Do not parse it, "
            "rebuild it, reorder it or drop any field."
        ),
    )


class AavaTestGenOrchestrator(BaseTool):
    """Runs the whole Azure DevOps story to EDI 834 test cases pipeline in one call."""

    name: str = "Aava Test Gen Orchestrator"
    description: str = (
        "Turns an Azure DevOps user story into EDI 834 inbound test cases, in one call or in "
        "two stages. stage=scenarios fetches the story, generates and reviews test scenarios "
        "with one self-heal rework, and writes the handoff file scenarios/<storyid>/"
        "scenarios.json to GitHub. stage=testcases reads that file, then for each scenario "
        "generates test cases and has an independent reviewer score every one, healing the "
        "failures, with all scenarios running in parallel. stage=all does the whole pipeline "
        "in one call. Returns one JSON envelope with a full structured log. Always returns: "
        "a scenario that failed is reported inside the envelope, never raised."
    )
    args_schema: Type[BaseModel] = AavaTestGenOrchestratorSchema

    # ── config ──────────────────────────────────────────────────────────────
    def _config(self, blob: Any) -> Dict[str, Any]:
        """Parse the runinputs object, then clamp and coerce. Form data arrives as strings,
        and an unbound {{variable}} arrives as its own literal name, so nothing is trusted
        by type."""
        raw = _strip_fences(blob if isinstance(blob, str) else json.dumps(blob))
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("runinputs is not a JSON object")
        cfg = json.loads(raw[start:end + 1])
        if not isinstance(cfg, dict):
            raise ValueError("runinputs did not parse to an object")
        cfg = {k: v for k, v in cfg.items() if v is not None}

        stage = str(cfg.get("stage") or "all").strip().lower()
        if stage not in ("all", "scenarios", "testcases"):
            raise ValueError("stage must be all, scenarios or testcases")
        cfg["stage"] = stage

        # The testcases stage reads everything about the story from the handoff file, so it
        # needs only the story id to derive the file's path.
        needed = ("adostoryid",) if stage == "testcases" \
            else ("adoorg", "adoproject", "adostoryid")
        for key in needed:
            if not str(cfg.get(key, "")).strip():
                raise ValueError(f"runinputs is missing {key}")
            cfg[key] = str(cfg[key]).strip()
        cfg.setdefault("adoorg", "")
        cfg.setdefault("adoproject", "")

        # Each stage requires only the agents it actually calls. 0 = not used / judge off;
        # scenarioreviewagentid 0 runs the scenarios stage without the reviewer.
        def agentid(key, required):
            try:
                cfg[key] = int(cfg.get(key, 0))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be an agent id number")
            if required and cfg[key] <= 0:
                raise ValueError(f"{key} must be a real agent id")

        agentid("scenarioagentid", stage in ("all", "scenarios"))
        agentid("testcaseagentid", stage in ("all", "testcases"))
        agentid("reviewagentid", False)
        agentid("scenarioreviewagentid", False)

        def num(key, default, lo, hi):
            try:
                v = int(cfg.get(key, default))
            except (TypeError, ValueError):
                v = default
            cfg[key] = max(lo, min(v, hi))

        # maxscenarios is mandatory when this run generates scenarios: it sizes the whole
        # run. The testcases stage takes its count from the handoff file instead.
        if stage != "testcases":
            try:
                int(cfg["maxscenarios"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("runinputs is missing maxscenarios")
            num("maxscenarios", 0, 1, 20)
        else:
            num("maxscenarios", 20, 1, 20)     # provisional; replaced after the handoff read
        num("testcasesperscenario", DEF_TESTCASESPERSCENARIO, 1, 5)
        num("stepsmin", DEF_STEPSMIN, 1, 40)
        num("stepsmax", DEF_STEPSMAX, cfg.get("stepsmin", DEF_STEPSMIN), 40)
        num("maxhealrounds", DEF_MAXHEALROUNDS, 0, 5)
        num("passscore", DEF_PASSSCORE, 1, 100)
        num("hardstopscore", DEF_HARDSTOPSCORE, 0, cfg.get("passscore", DEF_PASSSCORE))
        num("scenariopassscore", DEF_SCENARIOPASSSCORE, 1, 100)
        # Not an input. One thread per scenario, always: anything lower splits the run into
        # waves and multiplies wall clock by the wave count, which breaches the ceiling.
        cfg["maxworkers"] = cfg["maxscenarios"]
        num("deadlineseconds", DEF_DEADLINESECONDS, 60, 3600)
        # The exact worst case, so the caller never has to size this. An explicit value
        # still overrides. Scenario review adds at most 2 reviews + 1 rework generate.
        extra = 4 if cfg["scenarioreviewagentid"] > 0 else 0
        if stage == "scenarios":
            num("maxagentcalls", 3 + extra, 1, 500)
        elif stage == "testcases":
            num("maxagentcalls", 0, 0, 500)    # 0 = size from the handoff file in _run
        else:
            num("maxagentcalls",
                3 + cfg["maxscenarios"] * 2 * max(1, cfg["maxhealrounds"]) + extra, 1, 500)

        stag = cfg.get("stoponstagnation", True)
        if isinstance(stag, str):
            stag = stag.strip().lower() not in ("false", "0", "no", "")
        cfg["stoponstagnation"] = bool(stag)
        pub = cfg.get("publish", False)
        if isinstance(pub, str):
            pub = pub.strip().lower() in ("true", "1", "yes")
        cfg["publish"] = bool(pub)
        cfg["githubrepo"] = str(cfg.get("githubrepo") or DEF_GITHUBREPO)
        cfg["githubbranch"] = str(cfg.get("githubbranch") or DEF_GITHUBBRANCH)
        cfg["githubtoken"] = str(cfg.get("githubtoken") or "")
        cfg["aavabaseurl"] = str(cfg.get("aavabaseurl") or DEF_AAVA_BASE)
        cfg["realmid"] = str(cfg.get("realmid") or "")
        # Attribution on every /agents/execute call. Never blank: an unattributed
        # execution is hard to find later in the platform analytics.
        cfg["userprincipal"] = str(cfg.get("userprincipal") or DEF_USERPRINCIPAL)
        return cfg

    # ── entrypoint ──────────────────────────────────────────────────────────
    def _run(self, **kwargs) -> str:
        a = kwargs.get("kwargs", kwargs)
        log = _Log()

        try:
            cfg = self._config(a.get("runinputs") or "")
        except Exception as e:
            return json.dumps({"status": "failed", "stage": "validation",
                               "error": str(e)[:400], "log": log.dump()})

        adopat = _secret("HPIP_prod_ado_read_access", str(cfg.get("adopat") or ""))
        aavatoken = _secret("AAVA_TOKEN_BEARER_INT", str(cfg.get("aavatoken") or ""))
        log.guard(adopat, aavatoken, cfg["githubtoken"])
        if not adopat and cfg["stage"] != "testcases":
            return json.dumps({"status": "failed", "stage": "validation",
                               "error": "no azure devops credential available",
                               "log": log.dump()})
        if not aavatoken:
            return json.dumps({"status": "failed", "stage": "validation",
                               "error": "no aava bearer token available", "log": log.dump()})
        # The handoff file is how the two stages talk, so its token is not optional there.
        if cfg["stage"] != "all" and not cfg["githubtoken"]:
            return json.dumps({"status": "failed", "stage": "validation",
                               "error": "githubtoken is required for the stage handoff",
                               "log": log.dump()})

        # The testcases stage learns its size from the handoff file, so read it before the
        # budget is sized. This read is one cheap GET; the deadline clock starts right after.
        story: Optional[Dict[str, Any]] = None
        scenarios: List[Dict[str, Any]] = []
        scenverdict: Optional[Dict[str, Any]] = None
        if cfg["stage"] == "testcases":
            try:
                story, scenarios = fetch_handoff(cfg, log)
            except Exception as e:
                return json.dumps({"status": "failed", "stage": "handoff",
                                   "error": str(e)[:400], "log": log.dump()})
            cfg["maxscenarios"] = len(scenarios)
            cfg["maxworkers"] = len(scenarios)
            if not cfg["maxagentcalls"]:       # 0 = the config sentinel for "size from file"
                cfg["maxagentcalls"] = 2 + len(scenarios) * 2 * max(1, cfg["maxhealrounds"])

        budget = _Budget(cfg["deadlineseconds"], cfg["maxagentcalls"])
        log.line("start", stage=cfg["stage"], story=cfg["adostoryid"],
                 maxscenarios=cfg["maxscenarios"],
                 tcper=cfg["testcasesperscenario"],
                 steps=f"{cfg['stepsmin']}-{cfg['stepsmax']}",
                 rounds=cfg["maxhealrounds"], workers=cfg["maxworkers"],
                 pass_=f"{cfg['passscore']}/{cfg['hardstopscore']}",
                 judge=cfg["reviewagentid"] or "off",
                 scenjudge=cfg["scenarioreviewagentid"] or "off",
                 deadline=f"{cfg['deadlineseconds']}s")

        if cfg["stage"] != "testcases":
            # 1. story
            try:
                story = fetch_story(cfg, adopat, log)
            except Exception as e:
                return json.dumps({"status": "failed", "stage": "fetch",
                                   "error": str(e)[:400], "log": log.dump()})

            # 2. scenarios, with bounded retry on a parse failure
            feedback = ""
            for attempt in range(1, 4):
                raw = ""
                try:
                    scen_inputs = {
                        "storydata": _j({"storyid": story["storyid"],
                                         "title": story["title"],
                                         "description": story["description"],
                                         "acceptancecriteria": story["acceptancecriteria"]}),
                        "maxscenarios": str(cfg["maxscenarios"]),
                    }
                    if feedback:
                        scen_inputs["feedback"] = feedback   # omitted on the first attempt
                    raw, scen_ms = exec_agent(cfg["scenarioagentid"], scen_inputs,
                                              cfg, aavatoken, budget, log, "scenarios")
                    scenarios = parse_scenarios(raw, cfg["maxscenarios"])
                    break
                except Exception as e:
                    log.line("scenarios", attempt=f"{attempt}/3", error=str(e)[:140],
                             raw=_WS.sub(" ", raw)[:200] or None)
                    feedback = (f"the previous response was rejected: {e}. "
                                f"Return a valid JSON array.")
            if not scenarios:
                return json.dumps({"status": "failed", "stage": "scenarios",
                                   "error": "scenario generation did not return a valid array",
                                   "log": log.dump()})
            log.line("scenarios", count=len(scenarios),
                     ids=",".join(s["scenarioId"] for s in scenarios), ms=scen_ms)

            # 2a. scenario review — one verdict, at most one rework, then proceed either
            # way. A reviewer that errors never blocks the run: the unreviewed list ships.
            if cfg["scenarioreviewagentid"] > 0:
                for srnd in (1, 2):
                    try:
                        raw, srev_ms = exec_agent(
                            cfg["scenarioreviewagentid"],
                            {"reviewinputs": _j({
                                "scenarios": scenarios,
                                "storytitle": story["title"],
                                "storydescription": story["description"],
                                "acceptancecriteria": story["acceptancecriteria"],
                                "passscore": cfg["scenariopassscore"]})},
                            cfg, aavatoken, budget, log, "scenreview")
                        scenverdict = parse_scenario_verdict(raw)
                    except Exception as e:
                        log.line("scenreview", round=srnd, error=str(e)[:140])
                        break
                    log.line("scenreview", round=srnd,
                             confidence=scenverdict["confidence"],
                             approved=scenverdict["approved"],
                             gaps=len(scenverdict["gaps"]) or None, ms=srev_ms)
                    if scenverdict["approved"] or srnd == 2:
                        break
                    # One rework: the full list regenerates with the verdict verbatim —
                    # collapse/merge feedback touches the set, not one scenario.
                    fb = scenverdict["feedback"]
                    if scenverdict["gaps"]:
                        fb += " Specific gaps: " + "; ".join(scenverdict["gaps"])
                    raw = ""
                    try:
                        # scen_inputs survives the generation loop above; reuse its storydata
                        # so the rework sees exactly what the first round saw.
                        raw, ms = exec_agent(cfg["scenarioagentid"],
                                             {"storydata": scen_inputs["storydata"],
                                              "maxscenarios": str(cfg["maxscenarios"]),
                                              "feedback": fb},
                                             cfg, aavatoken, budget, log, "scenarios")
                        scenarios = parse_scenarios(raw, cfg["maxscenarios"])
                        log.line("scenarios", rework=1, count=len(scenarios),
                                 ids=",".join(s["scenarioId"] for s in scenarios), ms=ms)
                    except Exception as e:
                        log.line("scenarios", rework=1, error=str(e)[:140],
                                 raw=_WS.sub(" ", raw)[:200] or None)
                        break                  # keep the reviewed list we already have

        if cfg["stage"] == "scenarios":
            try:
                handoff = write_handoff(cfg, log, story, scenarios, scenverdict)
            except Exception as e:
                return json.dumps({"status": "failed", "stage": "handoff",
                                   "error": str(e)[:400], "log": log.dump()})
            secs = budget.elapsed_ms() // 1000
            log.line("outcome", stage="scenarios", story=cfg["adostoryid"],
                     scenarios=len(scenarios),
                     confidence=scenverdict["confidence"] if scenverdict else "unreviewed",
                     approved=scenverdict["approved"] if scenverdict else None,
                     elapsed=f"{secs // 60}m{secs % 60:02d}s")
            envelope = {
                "status": "completed", "stage": "scenarios",
                "story": {"id": story["storyid"], "title": story["title"]},
                "scenarios": scenarios, "review": scenverdict, "handoff": handoff,
                "agentcalls": budget.calls, "elapsedms": budget.elapsed_ms(),
                "log": log.dump(),
            }
            if cfg["publish"]:
                blanked = dict(cfg, adopat="", aavatoken="", githubtoken="")
                envelope["published"] = publish_run(cfg, log, [
                    ("run.log", "\n".join(log.dump()) + "\n"),
                    ("scenarios.json", json.dumps({"story": story, "scenarios": scenarios,
                                                   "review": scenverdict}, indent=2,
                                                  ensure_ascii=False)),
                    ("envelope.json", json.dumps(envelope, indent=2)),
                    ("runinputs.json", json.dumps(blanked, indent=2)),
                ])
                envelope["log"] = log.dump()
            return json.dumps(envelope)

        # 3. batches, one thread per scenario
        records: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=cfg["maxworkers"]) as pool:
            futures = {pool.submit(process_scenario, s, story, cfg, aavatoken, budget, log): s
                       for s in scenarios}
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    records.append(fut.result())
                except Exception as e:                        # belt and braces
                    records.append({"scenarioid": s["scenarioId"], "status": "failed",
                                    "error": str(e)[:300], "scorehistory": [], "rounds": 0,
                                    "testcasecount": 0, "chars": 0, "elapsedms": 0,
                                    "gaps": [], "table": "", "finalscore": None,
                                    "title": s.get("title", "")})
        order = {s["scenarioId"]: i for i, s in enumerate(scenarios)}
        records.sort(key=lambda r: order.get(r["scenarioid"], 999))

        # 4. cross batch check and assembly
        warnings = cross_batch_check(records, scenarios)
        body: List[str] = []
        for r in records:
            for row in (r.get("table") or "").split("\n"):
                stripped = row.strip()
                if stripped.startswith("|") and not stripped.startswith("|--") \
                        and "| ScenarioId |" not in stripped:
                    body.append(stripped)
        table = ""
        if body:
            table = "\n".join(["| " + " | ".join(COLUMNS) + " |",
                               "|" + "---|" * len(COLUMNS)] + body)

        counts = {k: sum(1 for r in records if r["status"] == k)
                  for k in ("approved", "unreviewed", "unhealed", "stagnant", "hardstop",
                            "failed", "skipped")}
        summary = {
            "scenarios": len(records), **counts,
            "testcases": sum(r["testcasecount"] for r in records),
            "totalrounds": sum(r["rounds"] for r in records),
            "agentcalls": budget.calls, "elapsedms": budget.elapsed_ms(),
        }
        log.line("done", **counts, testcases=summary["testcases"],
                 agentcalls=budget.calls, ms=budget.elapsed_ms())

        # One human readable line to close the log. Everything above is for grepping;
        # this is the line you read first when someone asks how the run went.
        # The line you read first, in the terms the output is used in: how many test cases can
        # go to a tester as they are, and how many want a second look. The machine states
        # (approved, stagnant, unhealed, failed) stay in the envelope for debugging — every
        # scenario so far produced test cases and was reviewed, so the difference between them
        # is how the tool stopped trying, not how much output you got.
        flagged = [(r["scenarioid"], f) for r in records for f in r["flagged"]]
        total_tc = summary["testcases"]
        secs = budget.elapsed_ms() // 1000
        log.line("outcome", story=cfg["adostoryid"],
                 ready=f"{total_tc - len(flagged)}/{total_tc} testcases",
                 flagged=len(flagged) or None,
                 scenarios=len(records),
                 elapsed=f"{secs // 60}m{secs % 60:02d}s",
                 warnings=len(warnings) or None)
        for sid, f in flagged:
            log.line("flagged", tc=f["id"], scenario=sid, why=f["why"])

        board = "  ".join(
            f"{r['scenarioid']}:{r['status']}"
            + (f"({'>'.join(str(s) for s in r['scorehistory'])})" if r["scorehistory"] else "")
            for r in records)
        log.line("detail", calls=budget.calls, rounds=summary["totalrounds"], scoreboard=board)

        # The table goes to stdout, NOT into the envelope. Returning it would make the calling
        # agent regenerate every one of its tokens to relay them: measured at 8 scenarios that
        # is ~53,000 tokens and ~900 seconds to move text the tool already holds. stdout is the
        # only tool output AAVA surfaces, so this reaches the activity log directly.
        rows = table.count("\n") - 1 if table else 0
        if table:
            print(f"[ORCH-TABLE-BEGIN] story={cfg['adostoryid']} scenarios={len(records)} "
                  f"rows={rows}", flush=True)
            print(table, flush=True)
            print(f"[ORCH-TABLE-END] chars={len(table)}", flush=True)

        envelope = {
            "status": "completed",
            "story": {"id": story["storyid"], "title": story["title"]},
            "summary": summary,
            "scenarios": [{k: v for k, v in r.items() if k != "table"} for r in records],
            "warnings": warnings,
            "testcases": {
                "rows": rows, "chars": len(table), "scenarios": len(records),
                "where": "activity log, between [ORCH-TABLE-BEGIN] and [ORCH-TABLE-END]",
            },
            "log": log.dump(),
        }

        if cfg["publish"]:
            blanked = dict(cfg, adopat="", aavatoken="", githubtoken="")
            envelope["published"] = publish_run(cfg, log, [
                ("run.log", "\n".join(log.dump()) + "\n"),
                ("testcases.md", table),
                ("envelope.json", json.dumps(envelope, indent=2)),
                ("runinputs.json", json.dumps(blanked, indent=2)),
            ])
            envelope["log"] = log.dump()      # pick up the publish lines themselves

        return json.dumps(envelope)
