"""Fetch workflow or standalone agent execution logs from AAVA and save them
for analysis.

Usage:
    python aava_logs_helper.py <executionId>                    # workflow execution logs
    python aava_logs_helper.py <executionId> --type agent        # standalone agent execution logs
    python aava_logs_helper.py <executionId> --output-dir logs

Saves the raw JSON response (data.<workflow|agent>ExecutionLogs[].logs, each a
JSON string event) plus a readable .md transcript grouped by event type —
mirroring the AAVA UI's "Agent Activity Logs" export — under --output-dir.
Each rendered block keeps both the event's own `timestamp` and the API's
`createAt` receipt time, and secrets (LLM api_key/tokens) are redacted before
being written out.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

from aava_endpoint_helper import AGENTS_BASE_URL, WORKFLOWS_BASE_URL, get_headers, load_aava_token

# Possible top-level keys holding the log entry list, across the different
# /logs endpoints (workflow execution vs. standalone agent execution).
_LOG_LIST_KEYS = ("workflowExecutionLogs", "agentExecutionLogs", "executionLogs")

# Keys that hold secrets and must never be written to the readable transcript.
_SECRET_KEYS = {
    "api_key",
    "pat_token",
    "authorization",
    "token",
    "access_token",
    "ado_pat",
    "client_secret",
    "password",
    "secret",
    "secret_key",
}

# Text-level safety net: secrets can also show up as plain text inside a prompt
# string (e.g. a {{template_var}} substituted with a JSON blob like
# \"ado_pat\": \"...\"), where they are not reachable via key-based dict
# redaction. Matches the key name plus any quote/backslash noise around it,
# then redacts only the value that follows.
_SECRET_TEXT_PATTERN = re.compile(
    r"("
    + "|".join(re.escape(k) for k in sorted(_SECRET_KEYS, key=len, reverse=True))
    + r")(\\*\"){0,4}\s*:\s*(\\*\"){0,4}"
    r"([A-Za-z0-9_\-\.\/\+=]{8,})",
    re.IGNORECASE,
)


def _scrub_embedded_secrets(text: str) -> str:
    """Redact secret-looking values embedded as plain text within a larger string."""
    return _SECRET_TEXT_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2) or ''}{m.group(3) or ''}***REDACTED***", text)

# event_type -> (icon+label used as the markdown heading).
_EVENT_HEADINGS = {
    "WORKFLOW_SETUP_STARTED": "🔧 Workflow Setup Started",
    "WORKFLOW_SETUP_COMPLETED": "✅ Workflow Setup Completed",
    "AGENT_CREATED": "🧩 Agent Registered",
    "WORKFLOW_STARTED": "🚀 Pipeline Execution Started",
    "WORKFLOW_KICKOFF_STARTED": "🚀 Workflow Kickoff Started",
    "KNOWLEDGE_RETRIEVAL_STARTED": "📚 Knowledge Retrieval Started",
    "KNOWLEDGE_QUERY_STARTED": "🔍 Knowledge Query Started",
    "KNOWLEDGE_QUERY_COMPLETED": "✅ Knowledge Query Completed",
    "KNOWLEDGE_RETRIEVAL_COMPLETED": "✅ Knowledge Retrieval Completed",
    "TASK_STARTED": "📝 Task Started",
    "AGENT_STARTED": "🤖 Agent Started",
    "LLM_STARTED": "🧠 LLM Call Started",
    "LLM_COMPLETED": "🧠 LLM Call Completed",
    "LLM_FAILED": "❌ LLM Call Failed",
    "TOOL_STARTED": "🛠️ Tool Initialized",
    "TOOL_COMPLETED": "🛠️ Tool Completed",
    "AGENT_STEP": "👣 Agent Step",
    "AGENT_COMPLETED": "✅ Agent Finished",
    "WORKFLOW_PROGRESS": "📈 Progress",
    "TASK_COMPLETED": "🎯 Task Finished",
    "WORKFLOW_KICKOFF_COMPLETED": "🏁 Workflow Kickoff Completed",
    "WORKFLOW_COMPLETED": "🎉 Workflow Completed",
}

# Large fields that repeat verbatim across every event tied to the same
# agent/task run. Deduped after the first full render to keep the transcript
# readable; the raw JSON still has every copy if byte-for-byte fidelity is needed.
_DEDUPE_FIELDS = (
    "task",
    "description",
    "expected_output",
    "agent_backstory",
    "agent_goal",
    "tools",
    "knowledge_bases",
    "agent_llm",
)


def get_execution_logs(execution_id: str, headers: dict, timeout: int = 30) -> dict:
    url = f"{WORKFLOWS_BASE_URL}/workflows/workflow-executions/{execution_id}/logs"
    response = requests.get(url, headers=headers, timeout=timeout, verify=False)
    response.raise_for_status()
    return response.json()


def get_agent_execution_logs(execution_id: str, headers: dict, timeout: int = 30) -> dict:
    url = f"{AGENTS_BASE_URL}/agents/execute/{execution_id}/logs"
    response = requests.get(url, headers=headers, timeout=timeout, verify=False)
    response.raise_for_status()
    return response.json()



def _redact(value):
    """Recursively strip secret-bearing keys (api_key, tokens, ...) from a value."""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _SECRET_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _find_log_list(payload: dict) -> list:
    """Locate the log-entry list under any of the known response shapes
    (workflow execution logs vs. standalone agent execution logs)."""
    data = payload.get("data") or {}
    for key in _LOG_LIST_KEYS:
        if data.get(key):
            return data[key]
        if payload.get(key):
            return payload[key]
    return []


def _parse_log_entries(payload: dict) -> list[dict]:
    """Decode each logs[].logs JSON string into an event dict."""
    entries = _find_log_list(payload)
    events = []
    for entry in entries:
        raw = entry.get("logs") if isinstance(entry, dict) else entry
        if not raw:
            continue
        try:
            event = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        event["_received_at"] = entry.get("createAt") if isinstance(entry, dict) else None
        events.append(event)
    return events


def _redact_raw_payload(payload: dict) -> dict:
    """Return a deep copy of the raw payload with secrets redacted inside each
    JSON-string-encoded log entry (the entries are not plain nested dicts, so
    the generic _redact() walk can't reach the tool_args fields directly)."""
    payload = json.loads(json.dumps(payload))  # cheap deep copy
    entries = _find_log_list(payload)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("logs")
        if not isinstance(raw, str):
            continue
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        entry["logs"] = json.dumps(_redact(event), ensure_ascii=False)
    return payload


def _fmt_value(value, max_len: int = 20000) -> str:
    """Render a scalar/dict/list field as inline text or a fenced JSON block."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(_redact(value), indent=2, ensure_ascii=False)
    if len(text) > max_len:
        text = text[:max_len] + f"\n... [truncated, {len(text) - max_len} more chars — see raw JSON]"
    return text


def _render_event(event: dict, seen_task_hash: dict) -> str:
    event_type = event.get("event_type") or event.get("type") or "UNKNOWN"
    heading = _EVENT_HEADINGS.get(event_type, event_type)
    ts = event.get("timestamp", "")
    received_at = event.get("_received_at", "")

    lines = [f"### {heading}", f"_Event time: {ts} · Logged at: {received_at}_"]

    agent_name = event.get("agent_name")
    agent_role = event.get("agent_role")
    if agent_name:
        lines.append(f"**Agent:** {agent_name}")
    if agent_role:
        lines.append(f"**Agent Role:** {agent_role}")

    if event.get("message"):
        lines.append(f"**Message:** {event['message']}")

    # Dedupe the large, repeated agent/task metadata blocks per agent run.
    task_text = event.get("task") or event.get("description")
    dedupe_key = event.get("agent_id")
    is_repeat = False
    if task_text and dedupe_key is not None:
        task_hash = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
        if seen_task_hash.get(dedupe_key) == task_hash:
            is_repeat = True
        else:
            seen_task_hash[dedupe_key] = task_hash

    if event.get("agent_goal") and not is_repeat:
        lines.append(f"**Agent Goal:** {event['agent_goal']}")
    if event.get("agent_backstory") and not is_repeat:
        lines.append(f"**Agent Backstory:** {event['agent_backstory']}")
    if event.get("expected_output") and not is_repeat:
        lines.append(f"**Expected Output:**\n{_fmt_value(event['expected_output'])}")
    if task_text:
        if is_repeat:
            lines.append("**Task Prompt:** _(same as previous block for this agent — see above)_")
        else:
            lines.append(f"**Task Prompt:**\n{_fmt_value(task_text)}")

    if event.get("tool_name"):
        lines.append(f"**Tool:** {event['tool_name']}")
    if event.get("tool_args") is not None:
        lines.append(f"**Tool Arguments (JSON):**\n```json\n{_fmt_value(event['tool_args'])}\n```")
    if event.get("started_at") or event.get("finished_at"):
        lines.append(f"**Tool Duration:** {event.get('started_at', '?')} → {event.get('finished_at', '?')}")
    if event.get("from_cache") is not None:
        lines.append(f"**From Cache:** {event['from_cache']}")

    for field, label in (("model", "Model"), ("call_type", "Call Type")):
        if event.get(field):
            lines.append(f"**{label}:** {event[field]}")
    if any(k in event for k in ("total_tokens", "prompt_tokens", "completion_tokens")):
        lines.append(
            "**Tokens:** total={} prompt={} completion={}".format(
                event.get("total_tokens", "?"), event.get("prompt_tokens", "?"), event.get("completion_tokens", "?")
            )
        )
    if isinstance(event.get("tokens"), dict):
        t = event["tokens"]
        lines.append(
            "**Tokens:** total={} prompt={} completion={} cost=${}".format(
                t.get("total", "?"), t.get("prompt", "?"), t.get("completion", "?"), t.get("cost", "?")
            )
        )
    if "output_length" in event:
        lines.append(f"**Output Length:** {event['output_length']} chars")
    for field, label in (("total_cost", "Total Cost"), ("input_cost", "Input Cost"), ("output_cost", "Output Cost")):
        if field in event:
            lines.append(f"**{label}:** ${event[field]}")

    if event.get("error"):
        lines.append(f"**Error:** {event['error']}")

    if event.get("progress"):
        p = event["progress"]
        lines.append(f"**Progress:** {p.get('pct', '?')}% ({p.get('agents_completed', '?')}/{p.get('agents_total', '?')} agents)")

    for field, label in (("thought", "Thought"), ("tool", "Tool Used"), ("tool_input", "Tool Input")):
        if event.get(field):
            lines.append(f"**{label}:** {event[field]}")
    if event.get("text"):
        lines.append(f"**Step Output:**\n{_fmt_value(event['text'])}")

    if event.get("output"):
        lines.append(f"**Output:**\n{_fmt_value(event['output'])}")

    return "\n\n".join(lines)


def render_markdown_transcript(payload: dict) -> str:
    """Build a UI-style, chronologically ordered transcript from the raw log payload."""
    events = _parse_log_entries(payload)
    if not events:
        return json.dumps(_redact(payload), indent=2, ensure_ascii=False)

    seen_task_hash: dict = {}
    blocks = [_render_event(event, seen_task_hash) for event in events]
    return "\n\n---\n\n".join(blocks)


def save_logs(payload: dict, execution_id: str, output_dir: Path, log_type: str = "workflow") -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m%d_%H%M")
    prefix = "execution" if log_type == "workflow" else f"{log_type}_execution"
    json_path = output_dir / f"{prefix}_{execution_id}_{stamp}.json"
    md_path = output_dir / f"{prefix}_{execution_id}_{stamp}.md"

    raw_text = json.dumps(_redact_raw_payload(payload), indent=2)
    md_text = render_markdown_transcript(payload)

    json_path.write_text(_scrub_embedded_secrets(raw_text), encoding="utf-8")
    md_path.write_text(_scrub_embedded_secrets(md_text), encoding="utf-8")

    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch AAVA workflow or agent execution logs.")
    parser.add_argument("execution_id", help="Execution ID (runId) to fetch logs for")
    parser.add_argument(
        "--type",
        choices=["workflow", "agent"],
        default="workflow",
        help="Whether execution_id refers to a workflow execution or a standalone agent execution (default: workflow)",
    )
    parser.add_argument("--token", help="Bearer token to use instead of .env AAVA_TOKEN")
    parser.add_argument("--output-dir", default="logs", help="Directory to save the logs")
    args = parser.parse_args()

    try:
        token = args.token.strip() if args.token else load_aava_token()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    headers = get_headers(token)

    try:
        if args.type == "agent":
            payload = get_agent_execution_logs(args.execution_id, headers)
        else:
            payload = get_execution_logs(args.execution_id, headers)
    except requests.exceptions.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as exc:
        print(f"Request error: {exc}", file=sys.stderr)
        return 1

    json_path, md_path = save_logs(payload, args.execution_id, Path(args.output_dir), log_type=args.type)
    print(f"Saved raw JSON to: {json_path}")
    print(f"Saved readable transcript to: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
