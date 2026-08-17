"""Run AavaTestGenOrchestrator locally against a saved runinputs payload.

Reads a line of the form {"runinputs": "<json string>"} — the exact shape a crewai tool
call carries — from a file, so a payload that usually holds live credentials never has to
be typed into the terminal or a command argument.

    python testgen_orchestrator/tool/run_payload.py <path-to-payload-file>
"""

import json
import os
import ssl
import sys
import tempfile
import time
import types

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

if os.environ.get("DEBUG_RAW"):
    _orig_parse_scenarios = T.parse_scenarios

    def _debug_parse_scenarios(raw, maxscenarios):
        print(f"\n--- raw scenario agent output ({len(raw)} chars) ---\n{raw}\n--- end ---\n")
        return _orig_parse_scenarios(raw, maxscenarios)

    T.parse_scenarios = _debug_parse_scenarios


def _find_payload(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('{"runinputs"'):
                return json.loads(line)["runinputs"]
    raise SystemExit(f'no line starting with {{"runinputs" found in {path}')


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_payload.py <path-to-payload-file>")
    runinputs = _find_payload(sys.argv[1])

    t0 = time.monotonic()
    envelope = T.AavaTestGenOrchestrator()._run(runinputs=runinputs)
    wall = time.monotonic() - t0
    res = json.loads(envelope)

    print(f"\n=== {res.get('status')} in {wall:.0f}s ===")
    if res.get("status") != "completed":
        print(f"stage {res.get('stage')}: {res.get('error')}")
        print("\n".join(res.get("log", [])))
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


if __name__ == "__main__":
    main()
