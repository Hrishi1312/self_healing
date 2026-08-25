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
import ssl
import sys
import tempfile
import time
import types
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _ensure_ca_bundle() -> None:
    """Merge certifi's roots with the Windows trust store and point requests at the result.

    Corporate TLS-inspection proxies (Zscaler/Forcepoint/Umbrella style) re-sign outbound
    HTTPS with a locally-issued root that certifi does not know about, so plain requests
    calls fail with "self-signed certificate in certificate chain". The Windows store does
    trust that root; this borrows it for the one process.
    """
    if os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    import certifi
    bundle_path = os.path.join(tempfile.gettempdir(), "aava_ca_bundle.pem")
    with open(certifi.where(), "r", encoding="utf-8") as fh:
        merged = fh.read()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    for der in ctx.get_ca_certs(binary_form=True):
        merged += "\n" + ssl.DER_cert_to_PEM_cert(der)
    with open(bundle_path, "w", encoding="utf-8") as fh:
        fh.write(merged)
    os.environ["REQUESTS_CA_BUNDLE"] = bundle_path


def _relax_strict_x509() -> None:
    """Corporate TLS-inspection proxies re-sign traffic with certs that omit the Authority
    Key Identifier extension. urllib3 enables OpenSSL's strict X.509 checking by default,
    which rejects that as malformed even once the issuing root itself is trusted. Chain
    trust (the CA bundle above) still applies; this only tolerates that one non-conformant
    extension.
    """
    import urllib3.connection as urllib3_conn
    import urllib3.util.ssl_ as urllib3_ssl
    _orig = urllib3_ssl.create_urllib3_context

    def _patched(*a, **kw):
        ctx = _orig(*a, **kw)
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    # urllib3.connection imported the name with `from .util.ssl_ import ...`, which binds
    # its own reference at import time, so patching util.ssl_ alone does not reach it.
    urllib3_ssl.create_urllib3_context = _patched
    urllib3_conn.create_urllib3_context = _patched


_ensure_ca_bundle()
_relax_strict_x509()

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


def load_env(path):
    """Read a .env into a dict. Ten lines beats a dependency.

    Keys are the runinputs names verbatim, so what is in the file is what the tool receives
    and there is no translation layer to get wrong. Blank values are dropped so they fall
    through to the built in defaults rather than sending an empty string.
    """
    env = {}
    if not os.path.exists(path):
        return env
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            env[k.strip()] = v
    return env


class _Tee:
    """Three files per run, one timestamp, plus the terminal.

    The tool prints BOTH the [ORCH] log and the assembled table to stdout, because stdout is
    the only stream AAVA captures. Locally that means 84 KB per scenario scrolls the log out
    of the terminal buffer, so this splits them apart again:

        tool_logs/<story>_<ts>/run.log        every [ORCH] line and this runner's output
        tool_logs/<story>_<ts>/testcases.md   the assembled table
        tool_logs/<story>_<ts>/envelope.json  the envelope, where per scenario gaps live
        tool_logs/<story>_<ts>/runinputs.json the settings used, credentials blanked

    Log lines still appear on the terminal as they happen.
    """

    def __init__(self, rundir):
        os.makedirs(rundir, exist_ok=True)
        self.dir = os.path.abspath(rundir)
        self.term = sys.__stdout__
        self.log = open(os.path.join(rundir, "run.log"), "w", encoding="utf-8")
        self.tbl = open(os.path.join(rundir, "testcases.md"), "w", encoding="utf-8")
        self.logpath, self.tblpath = self.log.name, self.tbl.name
        self.in_table = False
        self.buf = ""
        self.rows = 0

    def write(self, chunk):
        self.buf += chunk
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._line(line + "\n")

    def _line(self, line):
        if line.startswith("[ORCH-TABLE-BEGIN]"):
            self.in_table = True
        elif line.startswith("[ORCH-TABLE-END]"):
            self.in_table = False
            self._out(line)
            self._out("           %d rows -> %s\n"
                      % (self.rows, os.path.relpath(self.tblpath)))
            return
        if self.in_table and not line.startswith("[ORCH-TABLE-BEGIN]"):
            self.tbl.write(line)
            self.rows += 1
        else:
            self._out(line)

    def _out(self, line):
        self.term.write(line); self.term.flush()
        self.log.write(line); self.log.flush()

    def flush(self):
        self.term.flush(); self.log.flush(); self.tbl.flush()

    def close(self):
        if self.buf:
            self._line(self.buf)
        self.log.close(); self.tbl.close()


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


def _files(tee, rundir):
    """Name what was written and put stdout back. Called on every exit path, so a failed run
    still leaves a complete folder."""
    print("\n%s" % tee.dir)
    for n, what in (("run.log", "every [ORCH] line and this summary"),
                    ("testcases.md", "the assembled table"),
                    ("envelope.json", "per scenario scores and the reviewer's gaps"),
                    ("runinputs.json", "the exact settings used, credentials blanked")):
        if os.path.exists(os.path.join(rundir, n)):
            print("   %-15s %s" % (n, what))
    sys.stdout = sys.__stdout__
    tee.close()


def main():
    ap = argparse.ArgumentParser(
        description="Run AavaTestGenOrchestrator locally. Settings come from .env next to "
                    "this file; any flag below overrides it.")
    # --env has to be resolved before the other defaults exist, so read it with a throwaway
    # parser. Doing it on `ap` would make -h exit here, printing only this one flag.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env", default=os.path.join(HERE, ".env"))
    known, _ = pre.parse_known_args()
    E = load_env(known.env)
    ap.add_argument("--env", default=known.env,
                    help="path to the .env (default: alongside run_local.py)")

    def d(key, fallback, cast=str):
        v = E.get(key)
        if v is None:
            return fallback
        if cast is bool:
            return v.strip().lower() not in ("false", "0", "no")
        return cast(v)

    ap.add_argument("storyid", nargs="?", default=d("adostoryid", None))
    ap.add_argument("--org", default=d("adoorg", "CSGRP"))
    ap.add_argument("--project", default=d("adoproject", "ADO"))
    ap.add_argument("--scenarios", type=int, default=d("maxscenarios", 4, int))
    ap.add_argument("--cases", type=int, default=d("testcasesperscenario", 8, int),
                    help="ceiling, not a target: the generator writes fewer when a scenario "
                         "supports fewer distinct conditions")
    ap.add_argument("--stepsmin", type=int, default=d("stepsmin", 15, int))
    ap.add_argument("--stepsmax", type=int, default=d("stepsmax", 18, int))
    ap.add_argument("--rounds", type=int, default=d("maxhealrounds", 2, int))
    ap.add_argument("--no-heal", action="store_true", help="single pass, no regeneration")
    ap.add_argument("--no-judge", action="store_true", help="pre gate only, no reviewer")
    ap.add_argument("--deadline", type=int, default=d("deadlineseconds", 190, int))
    ap.add_argument("--scenarioagent", type=int, default=d("scenarioagentid", 625, int))
    ap.add_argument("--testcaseagent", type=int, default=d("testcaseagentid", 626, int))
    ap.add_argument("--reviewagent", type=int, default=d("reviewagentid", 627, int))
    ap.add_argument("--base", default=d("aavabaseurl", T.DEF_AAVA_BASE))
    ap.add_argument("--realm", default=d("realmid", ""))
    ap.add_argument("--user", default=d("userprincipal", T.DEF_USERPRINCIPAL))
    ap.add_argument("--probe", action="store_true", help="check {{variable}} binding and exit")
    ap.add_argument("--publish", action="store_true", default=d("publish", False, bool),
                    help="push run.log, testcases.md, envelope.json and runinputs.json to "
                         "GitHub (githubtoken from .env or GITHUB_TOKEN)")
    a = ap.parse_args()

    token = E.get("aavatoken") or os.environ.get("AAVA_TOKEN", "")
    pat = E.get("adopat") or os.environ.get("ADO_PAT", "")
    if not a.storyid:
        sys.exit("no story id: pass one as an argument or set adostoryid in %s" % known.env)
    if not token:
        sys.exit("no aavatoken in %s and no AAVA_TOKEN in the environment" % known.env)
    if not pat and not a.probe:
        sys.exit("no adopat in %s and no ADO_PAT in the environment" % known.env)

    runinputs = {
        "adoorg": a.org, "adoproject": a.project, "adostoryid": a.storyid,
        "scenarioagentid": a.scenarioagent, "testcaseagentid": a.testcaseagent,
        "reviewagentid": 0 if a.no_judge else a.reviewagent,
        "maxscenarios": a.scenarios, "testcasesperscenario": a.cases,
        "stepsmin": a.stepsmin, "stepsmax": a.stepsmax,
        "maxhealrounds": 0 if a.no_heal else a.rounds,
        # maxworkers is no longer an input: the tool always uses one thread per scenario.
        "passscore": d("passscore", 80, int),
        "hardstopscore": d("hardstopscore", 50, int),
        "stoponstagnation": d("stoponstagnation", True, bool),
        # None is dropped by the tool, which then sizes maxagentcalls from the run shape;
        # a value in .env still overrides.
        "maxagentcalls": d("maxagentcalls", None, int),
        "deadlineseconds": a.deadline,
        "aavabaseurl": a.base, "realmid": a.realm, "userprincipal": a.user,
        "adopat": pat, "aavatoken": token,
        "publish": a.publish,
        "githubtoken": E.get("githubtoken") or os.environ.get("GITHUB_TOKEN", ""),
        "githubrepo": d("githubrepo", ""),        # blank falls to the tool's default
        "githubbranch": d("githubbranch", ""),
        # Optional, from .env only: a domain glossary handed to all three agents, and a
        # comma separated list of terms the pregate rejects from the assembled table.
        "domainhints": d("domainhints", ""),
        "bannedterms": d("bannedterms", ""),
    }

    if a.probe:
        tool = T.AavaTestGenOrchestrator()
        sys.exit(probe(tool._config(json.dumps(runinputs))))

    hdr = (f"story {a.storyid}: {a.scenarios} scenarios x {a.cases} cases x "
           f"{a.stepsmin}-{a.stepsmax} steps, "
           f"{'no heal' if a.no_heal else str(a.rounds) + ' heal round(s)'}, "
           f"{'no judge' if a.no_judge else 'judged'}, deadline {a.deadline}s")

    rundir = os.path.join("tool_logs", "%s_%s" % (a.storyid, time.strftime("%Y%m%d_%H%M%S")))
    tee = _Tee(rundir)
    # Written up front, not at the end: on a failed run this is the file that says what was
    # attempted, and the failure paths below exit before any closing block would run.
    open(os.path.join(rundir, "runinputs.json"), "w", encoding="utf-8").write(
        json.dumps(dict(runinputs, adopat="", aavatoken="", githubtoken=""), indent=2))
    sys.stdout = tee                      # everything below is captured as well as shown
    print(hdr)
    print("config: %s\n" % (known.env if os.path.exists(known.env)
                                     else "built in defaults, no .env found"))
    t0 = time.monotonic()
    try:
        envelope = T.AavaTestGenOrchestrator()._run(runinputs=json.dumps(runinputs))
    except Exception:
        sys.stdout = sys.__stdout__; tee.close(); raise
    wall = time.monotonic() - t0
    res = json.loads(envelope)

    open(os.path.join(rundir, "envelope.json"), "w", encoding="utf-8").write(envelope)
    print(f"\n=== {res.get('status')} in {wall:.0f}s ===")
    if res.get("status") != "completed":
        print(f"stage {res.get('stage')}: {res.get('error')}")
        _files(tee, rundir)
        sys.exit(1)

    # The readable half: what can go to a tester, and what wants a look. Machine states are
    # still in envelope.json; they answer "why did the tool stop", not "can I use this".
    total = res["summary"]["testcases"]
    flagged = sum(len(r["flagged"]) for r in res["scenarios"])
    mins, secs = divmod(int(wall), 60)
    print(f"\nStory {a.storyid} — {total} test cases in {mins}m {secs:02d}s\n")
    print(f"  READY TO USE   {total - flagged} of {total} test cases")
    if flagged:
        print(f"  NEEDS A LOOK   {flagged} of {total} test cases")
    print()
    for r in res["scenarios"]:
        n, bad = r["testcasecount"], len(r["flagged"])
        ids = "   " + ", ".join(f["id"] for f in r["flagged"]) if bad else ""
        note = "  " + (r["error"] or "")[:60] if r["error"] and not n else ""
        print("  %-8s %d of %d ready%s%s" % (r["scenarioid"], n - bad, n, ids, note))
    if flagged:
        print("\n  why each was flagged:")
        for r in res["scenarios"]:
            for f in r["flagged"]:
                print("     %-16s %s" % (f["id"], f["why"]))
    for w in res["warnings"]:
        print("  warning:", w)
    print(f"\n  All {total} test cases are in testcases.md. envelope.json carries the "
          f"reviewer's full reasoning.")
    pub = res.get("published")
    if pub:
        print("  published: %s" % (pub.get("error")
              or "https://github.com/%s/tree/%s/%s" % (pub["repo"], pub["branch"], pub["path"])))
    _files(tee, rundir)

    if wall > 240:
        print(f"\nWARNING: {wall:.0f}s exceeds the 240s ACA ceiling. On the platform this run "
              f"would have been severed.")


if __name__ == "__main__":
    main()
