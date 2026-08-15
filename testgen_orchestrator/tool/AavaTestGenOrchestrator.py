"""AavaTestGenOrchestrator — Azure DevOps story to EDI 834 test cases, in one tool call.

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
DEF_AAVA_BASE = "https://int-ai.aava.ai"
DEF_ADO_BASE = "https://dev.azure.com"

DEF_MAXSCENARIOS = 5
DEF_TESTCASESPERSCENARIO = 3
DEF_STEPSMIN = 15
DEF_STEPSMAX = 18
DEF_MAXHEALROUNDS = 3
DEF_PASSSCORE = 90
DEF_MAXWORKERS = 5
DEF_DEADLINESECONDS = 3000
DEF_MAXAGENTCALLS = 60

HTTP_TIMEOUT = 600          # per agent call
ADO_TIMEOUT = (15, 45)
RETRY_STATUSES = {0, 403, 429, 500, 502, 503, 504}
MAX_HTTP_ATTEMPTS = 3

COLUMNS = ["ScenarioId", "AcceptanceCriteriaRef", "Name", "Id", "Attachments", "Status",
           "Test Case Type", "Description", "Precondition", "Test Step #",
           "Test Step Description", "Test Step Expected Result", "Test Step Attachment"]

SCENARIO_KEYS = ["scenarioid", "title", "descriptionref", "acceptancecriteriaref",
                 "dorref", "dodref", "type", "description", "priority"]

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
        print(entry)

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
          json_body: Any = None, timeout: Any = HTTP_TIMEOUT) -> Tuple[int, Any]:
    """One request with bounded retry. Returns (status, parsed_body). Status 0 means the
    client never got a response, which is how a severed long request shows up."""
    last: Tuple[int, Any] = (0, None)
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            r = requests.request(method, url, headers=headers or {}, json=json_body,
                                 timeout=timeout)
            try:
                body = r.json()
            except Exception:
                body = r.text
            if r.status_code not in RETRY_STATUSES:
                return r.status_code, body
            last = (r.status_code, body)
        except requests.RequestException as e:
            last = (0, str(e))
        if attempt < MAX_HTTP_ATTEMPTS:
            wait = 2.0 ** attempt
            log.line("retry", url=url.rsplit("/", 1)[-1], attempt=f"{attempt}/{MAX_HTTP_ATTEMPTS}",
                     status=last[0], wait=f"{wait:.0f}s")
            time.sleep(wait)
    return last


# ── secrets ─────────────────────────────────────────────────────────────────
def _secret(key: str, fallback: str = "") -> str:
    """AVASecret first, the inputs blob only as a local development fallback."""
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
def exec_agent(agentid: int, userinputs: Dict[str, Any], cfg: Dict[str, Any],
               token: str, budget: _Budget, log: _Log, label: str) -> str:
    """POST /agents/execute. Synchronous: the answer comes back in the response body, so no
    polling is involved. To switch to trigger and poll, this is the only function to change.
    """
    if not budget.take():
        raise RuntimeError("budget exhausted before call")
    url = cfg["aavabaseurl"].rstrip("/") + "/agents/execute"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if cfg.get("realmid"):
        headers["x-realm-id"] = str(cfg["realmid"])
    body = {"agentId": int(agentid), "executionId": str(uuid.uuid4()),
            "user": cfg.get("userprincipal", ""), "userInputs": userinputs}

    t0 = time.monotonic()
    status, payload = _http("POST", url, log, headers, body)
    ms = int((time.monotonic() - t0) * 1000)
    if status != 200:
        log.line("agenterror", label=label, agentid=agentid, status=status, ms=ms)
        raise RuntimeError(f"{label}: agent {agentid} returned http {status}")

    out = ""
    if isinstance(payload, dict):
        try:
            out = payload["data"]["agentResponse"]["agent"]["output"]
        except (KeyError, TypeError):
            out = payload.get("output") or payload.get("result") or ""
    if not out:
        raise RuntimeError(f"{label}: agent {agentid} returned an empty output")
    log.line("agentcall", label=label, agentid=agentid, outchars=len(str(out)), ms=ms)
    return str(out)


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
    if start == -1 or end == -1:
        raise ValueError("response contains no JSON array")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list) or not data:
        raise ValueError("scenario array is empty")

    seen, out = set(), []
    for i, s in enumerate(data):
        if not isinstance(s, dict):
            raise ValueError(f"scenario {i} is not an object")
        missing = [k for k in SCENARIO_KEYS if k not in s]
        if missing:
            raise ValueError(f"scenario {i} missing keys: {', '.join(missing)}")
        sid = str(s["scenarioid"]).strip()
        if not re.match(r"^TS_\d+$", sid):
            raise ValueError(f"scenario {i} scenarioid '{sid}' is not TS_ followed by digits")
        if sid in seen:
            raise ValueError(f"duplicate scenarioid {sid}")
        seen.add(sid)
        if str(s["type"]).strip() not in TYPES:
            raise ValueError(f"{sid} type '{s['type']}' is not Positive, Negative or Edge")
        if str(s["priority"]).strip() not in PRIORITIES:
            raise ValueError(f"{sid} priority '{s['priority']}' is not High, Medium or Low")
        # dorref and dodref may legitimately be empty; the story has no definition of ready.
        out.append({k: s[k] for k in SCENARIO_KEYS})

    order = {"High": 0, "Medium": 1, "Low": 2}
    out.sort(key=lambda s: order.get(str(s["priority"]).strip(), 3))
    return out[:maxscenarios]


def parse_testcases(raw: str, stepsmin: int, stepsmax: int) -> Dict[str, Any]:
    """Parse the markdown table and check its shape. Returns rows plus a per test case index."""
    text = _strip_fences(raw)
    rows = [r.strip() for r in text.split("\n") if r.strip().startswith("|")]
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

    problems = []
    for tid, tcrows in cases.items():
        n = len(tcrows)
        if n < stepsmin or n > stepsmax:
            problems.append(f"{tid} has {n} steps, expected {stepsmin} to {stepsmax}")
        st = {r[5] for r in tcrows if r[5]}
        if st - TYPES:
            problems.append(f"{tid} Status holds {sorted(st - TYPES)}, expected Positive, Negative or Edge")
        cat = {r[6] for r in tcrows if r[6]}
        if cat - CATEGORIES:
            problems.append(f"{tid} Test Case Type holds {sorted(cat - CATEGORIES)}, expected Functional or Regression")
    if problems:
        raise ValueError("; ".join(problems))

    return {"header": header, "rows": body, "cases": cases, "table": text,
            "chars": len(text), "ids": list(cases.keys())}


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


# ── one scenario, start to finish, on its own thread ────────────────────────
def process_scenario(scenario: Dict[str, Any], story: Dict[str, Any], cfg: Dict[str, Any],
                     token: str, budget: _Budget, log: _Log) -> Dict[str, Any]:
    """Generate, review and heal one scenario. Never raises: every failure becomes a record
    so that one bad scenario cannot take the run down with it."""
    sid = scenario["scenarioid"]
    rec: Dict[str, Any] = {
        "scenarioid": sid, "title": scenario.get("title", ""), "status": "failed",
        "scorehistory": [], "finalscore": None, "rounds": 0,
        "testcasecount": 0, "chars": 0, "elapsedms": 0, "gaps": [], "error": None,
        "table": "",
    }
    t0 = time.monotonic()
    passscore = cfg["passscore"]
    parsed: Optional[Dict[str, Any]] = None
    regenerate: List[Dict[str, Any]] = []

    try:
        for rnd in range(1, cfg["maxhealrounds"] + 1):
            if not budget.allows(need_seconds=60):
                rec["status"] = "skipped" if rnd == 1 else rec["status"]
                rec["error"] = "budget exhausted"
                log.line("budget", scenario=sid, round=rnd, note="stopped, budget low")
                break
            rec["rounds"] = rnd

            gen_inputs = {
                "scenario": json.dumps(scenario),
                "storytitle": story["title"],
                "testcasesperscenario": cfg["testcasesperscenario"],
                "stepsmin": cfg["stepsmin"],
                "stepsmax": cfg["stepsmax"],
                "regenerate": json.dumps(regenerate) if regenerate else "",
            }
            try:
                raw = exec_agent(cfg["testcaseagentid"], gen_inputs, cfg, token, budget, log,
                                 f"generate:{sid}")
                parsed = parse_testcases(raw, cfg["stepsmin"], cfg["stepsmax"])
            except Exception as e:
                log.line("generate", scenario=sid, round=rnd, error=str(e)[:120])
                regenerate = [{"id": "all", "gaps": [f"previous attempt was rejected: {e}"]}]
                rec["error"] = str(e)[:300]
                continue

            rec["table"] = parsed["table"]
            rec["chars"] = parsed["chars"]
            rec["testcasecount"] = len(parsed["ids"])
            log.line("generate", scenario=sid, round=rnd, tc=len(parsed["ids"]),
                     chars=parsed["chars"])

            rev_inputs = {
                "scenario": json.dumps(scenario),
                "testcases": parsed["table"],
                "passscore": passscore,
                "stepsmin": cfg["stepsmin"],
                "stepsmax": cfg["stepsmax"],
                "testcasesperscenario": cfg["testcasesperscenario"],
            }
            try:
                raw = exec_agent(cfg["reviewagentid"], rev_inputs, cfg, token, budget, log,
                                 f"review:{sid}")
                verdict = parse_verdict(raw, parsed["ids"])
            except Exception as e:
                log.line("review", scenario=sid, round=rnd, error=str(e)[:120])
                rec["error"] = str(e)[:300]
                continue

            scores = verdict["scores"]
            failing = [s for s in scores if not s.get("pass")]
            batchscore = int(verdict.get("batchscore") or min(s["score"] for s in scores))
            rec["scorehistory"].append(batchscore)
            rec["finalscore"] = batchscore
            rec["gaps"] = [g for s in failing for g in (s.get("gaps") or [])]
            log.line("review", scenario=sid, round=rnd, score=batchscore,
                     passed=f"{len(scores) - len(failing)}/{len(scores)}",
                     failing=",".join(s["id"] for s in failing) or None)

            if not failing:
                rec["status"] = "approved"
                break

            if cfg["stoponstagnation"] and len(rec["scorehistory"]) >= 2 \
                    and rec["scorehistory"][-1] <= rec["scorehistory"][-2]:
                rec["status"] = "stagnant"
                log.line("stagnant", scenario=sid, round=rnd, scores=rec["scorehistory"])
                break

            if rnd >= cfg["maxhealrounds"]:
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
        if s["scenarioid"] not in covered:
            warnings.append(f"{s['scenarioid']} produced no test cases")

    seen: Dict[str, str] = {}
    statuses: Dict[str, int] = {}
    for r in records:
        for row in (r.get("table") or "").split("\n"):
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) != len(COLUMNS) or cells[3] in ("Id", ""):
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


# ── crewai surface ──────────────────────────────────────────────────────────
class AavaTestGenOrchestratorSchema(BaseModel):
    """Input schema for AavaTestGenOrchestrator."""

    inputs: str = Field(
        ...,
        description=(
            "One JSON blob with every run setting. Keys are lowercase with no separators: "
            "adoorg, adoproject, adostoryid, adoworkitemtype, adoareapath, scenarioagentid, "
            "testcaseagentid, reviewagentid, maxscenarios, testcasesperscenario, stepsmin, "
            "stepsmax, maxhealrounds, passscore, maxworkers, stoponstagnation, "
            "deadlineseconds, maxagentcalls, aavabaseurl, realmid, userprincipal, and for "
            "local testing only adopat and aavatoken. Pass the value through exactly as "
            "received, including any credential fields; do not parse or rebuild it."
        ),
    )


class AavaTestGenOrchestrator(BaseTool):
    """Runs the whole Azure DevOps story to EDI 834 test cases pipeline in one call."""

    name: str = "Aava Test Gen Orchestrator"
    description: str = (
        "Turns an Azure DevOps user story into EDI 834 inbound test cases. Fetches the story, "
        "generates test scenarios, then for each scenario generates test cases and has an "
        "independent reviewer score every one, healing the failures, with all scenarios "
        "running in parallel. Returns one JSON envelope carrying a score per scenario, the "
        "assembled test case table and a full structured log. Always returns: a scenario that "
        "failed is reported inside the envelope, never raised. Takes a single argument, "
        "inputs, which is a JSON blob."
    )
    args_schema: Type[BaseModel] = AavaTestGenOrchestratorSchema

    # ── config ──────────────────────────────────────────────────────────────
    def _config(self, blob: str) -> Dict[str, Any]:
        raw = _strip_fences(blob if isinstance(blob, str) else json.dumps(blob))
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("inputs is not a JSON object")
        cfg = json.loads(raw[start:end + 1])
        if not isinstance(cfg, dict):
            raise ValueError("inputs did not parse to an object")

        for key in ("adoorg", "adoproject", "adostoryid"):
            if not str(cfg.get(key, "")).strip():
                raise ValueError(f"inputs is missing {key}")
        for key in ("scenarioagentid", "testcaseagentid", "reviewagentid"):
            try:
                cfg[key] = int(cfg[key])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"inputs {key} must be an agent id number")

        def num(key, default, lo, hi):
            try:
                v = int(cfg.get(key, default))
            except (TypeError, ValueError):
                v = default
            cfg[key] = max(lo, min(v, hi))

        num("maxscenarios", DEF_MAXSCENARIOS, 1, 20)
        num("testcasesperscenario", DEF_TESTCASESPERSCENARIO, 1, 5)
        num("stepsmin", DEF_STEPSMIN, 1, 40)
        num("stepsmax", DEF_STEPSMAX, cfg.get("stepsmin", DEF_STEPSMIN), 40)
        num("maxhealrounds", DEF_MAXHEALROUNDS, 0, 5)
        num("passscore", DEF_PASSSCORE, 1, 100)
        num("maxworkers", DEF_MAXWORKERS, 1, 10)
        num("deadlineseconds", DEF_DEADLINESECONDS, 60, 3600)
        num("maxagentcalls", DEF_MAXAGENTCALLS, 1, 500)

        stag = cfg.get("stoponstagnation", True)
        if isinstance(stag, str):
            stag = stag.strip().lower() not in ("false", "0", "no", "")
        cfg["stoponstagnation"] = bool(stag)
        cfg["aavabaseurl"] = str(cfg.get("aavabaseurl") or DEF_AAVA_BASE)
        cfg["realmid"] = str(cfg.get("realmid") or "")
        cfg["userprincipal"] = str(cfg.get("userprincipal") or "")
        return cfg

    # ── entrypoint ──────────────────────────────────────────────────────────
    def _run(self, **kwargs) -> str:
        a = kwargs.get("kwargs", kwargs)
        log = _Log()

        try:
            cfg = self._config(a.get("inputs") or "")
        except Exception as e:
            return json.dumps({"status": "failed", "stage": "validation",
                               "error": str(e)[:400], "log": log.dump()})

        adopat = _secret("HPIP_prod_ado_read_access", str(cfg.get("adopat") or ""))
        aavatoken = _secret("AAVA_TOKEN_BEARER_INT", str(cfg.get("aavatoken") or ""))
        log.guard(adopat, aavatoken)
        if not adopat:
            return json.dumps({"status": "failed", "stage": "validation",
                               "error": "no azure devops credential available",
                               "log": log.dump()})
        if not aavatoken:
            return json.dumps({"status": "failed", "stage": "validation",
                               "error": "no aava bearer token available", "log": log.dump()})

        budget = _Budget(cfg["deadlineseconds"], cfg["maxagentcalls"])
        log.line("start", story=cfg["adostoryid"], maxscenarios=cfg["maxscenarios"],
                 tcper=cfg["testcasesperscenario"],
                 steps=f"{cfg['stepsmin']}-{cfg['stepsmax']}",
                 rounds=cfg["maxhealrounds"], workers=cfg["maxworkers"],
                 deadline=f"{cfg['deadlineseconds']}s")

        # 1. story
        try:
            story = fetch_story(cfg, adopat, log)
        except Exception as e:
            return json.dumps({"status": "failed", "stage": "fetch", "error": str(e)[:400],
                               "log": log.dump()})

        # 2. scenarios, with bounded retry on a parse failure
        scenarios: List[Dict[str, Any]] = []
        feedback = ""
        for attempt in range(1, 4):
            try:
                raw = exec_agent(cfg["scenarioagentid"], {
                    "storyid": story["storyid"], "title": story["title"],
                    "description": story["description"],
                    "acceptancecriteria": story["acceptancecriteria"],
                    "maxscenarios": cfg["maxscenarios"], "feedback": feedback,
                }, cfg, aavatoken, budget, log, "scenarios")
                scenarios = parse_scenarios(raw, cfg["maxscenarios"])
                break
            except Exception as e:
                log.line("scenarios", attempt=f"{attempt}/3", error=str(e)[:140])
                feedback = f"the previous response was rejected: {e}. Return a valid JSON array."
        if not scenarios:
            return json.dumps({"status": "failed", "stage": "scenarios",
                               "error": "scenario generation did not return a valid array",
                               "log": log.dump()})
        log.line("scenarios", count=len(scenarios),
                 ids=",".join(s["scenarioid"] for s in scenarios))

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
                    records.append({"scenarioid": s["scenarioid"], "status": "failed",
                                    "error": str(e)[:300], "scorehistory": [], "rounds": 0,
                                    "testcasecount": 0, "chars": 0, "elapsedms": 0,
                                    "gaps": [], "table": "", "finalscore": None,
                                    "title": s.get("title", "")})
        order = {s["scenarioid"]: i for i, s in enumerate(scenarios)}
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
                  for k in ("approved", "unhealed", "stagnant", "failed", "skipped")}
        summary = {
            "scenarios": len(records), **counts,
            "testcases": sum(r["testcasecount"] for r in records),
            "totalrounds": sum(r["rounds"] for r in records),
            "agentcalls": budget.calls, "elapsedms": budget.elapsed_ms(),
        }
        log.line("done", **counts, testcases=summary["testcases"],
                 agentcalls=budget.calls, ms=budget.elapsed_ms())

        return json.dumps({
            "status": "completed",
            "story": {"id": story["storyid"], "title": story["title"]},
            "summary": summary,
            "scenarios": [{k: v for k, v in r.items() if k != "table"} for r in records],
            "warnings": warnings,
            "testcases": table,
            "log": log.dump(),
        })
