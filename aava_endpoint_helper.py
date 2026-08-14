import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

WORKFLOWS_BASE_URL = "https://aava-core-api-workflows-svc.redtree-f4541a84.eastus.azurecontainerapps.io"
AGENTS_BASE_URL = "https://aava-core-api-agents-svc.redtree-f4541a84.eastus.azurecontainerapps.io"
TOOLS_BASE_URL = "https://aava-core-api-tools-svc.redtree-f4541a84.eastus.azurecontainerapps.io"
ENV_PATH = Path(__file__).parent / ".env"


def load_aava_token(env_path: Path = ENV_PATH) -> str:
    token = os.environ.get("AAVA_TOKEN")
    if token:
        return token.strip()

    if not env_path.exists():
        raise FileNotFoundError(f"Could not find .env file at {env_path}")

    with env_path.open("r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^AAVA_TOKEN\s*=\s*(.+)$", line)
            if match:
                return match.group(1).strip().strip('"').strip("'")

    raise ValueError("AAVA_TOKEN was not found in .env or environment variables.")


def get_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fetch_json(url: str, headers: dict, timeout: int = 20) -> dict:
    response = requests.get(url, headers=headers, timeout=timeout, verify=False)
    response.raise_for_status()
    return response.json()


def get_tool(tool_id: str, headers: dict) -> dict:
    url = f"{TOOLS_BASE_URL}/tools/userTools?userToolId={tool_id}"
    return fetch_json(url, headers)


def get_agent(agent_id: str, headers: dict) -> dict:
    url = f"{AGENTS_BASE_URL}/agents?agentId={agent_id}"
    return fetch_json(url, headers)


def get_workflow(workflow_id: str, headers: dict) -> dict:
    url = f"{WORKFLOWS_BASE_URL}/workflows?workFlowId={workflow_id}"
    return fetch_json(url, headers)


def save_json(data: dict, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / filename
    with target.open("w", encoding="utf-8") as out_file:
        json.dump(data, out_file, indent=2)
    return target


def save_text(text: str, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / filename
    with target.open("w", encoding="utf-8") as out_file:
        out_file.write(text or "")
    return target


def normalize_name(name: str) -> str:
    if not name:
        return "unknown"
    value = str(name).strip()
    value = re.sub(r"[\\/*?\"<>|:]+", "_", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value[:80].strip("_") or "unknown"


def extract_tool_code(payload: dict) -> str:
    return payload.get("data", {}).get("userToolDetail", {}).get("toolConfigs", {}).get("tool_class_def", "")


def extract_agent_prompt(payload: dict) -> str:
    detail = payload.get("data", {}).get("agentDetail", {})
    return detail.get("description", "") or detail.get("instructions", "") or ""


def summarize_tool(payload: dict) -> str:
    detail = payload.get("data", {}).get("userToolDetail", {})
    return (
        f"Tool ID: {detail.get('id')}\n"
        f"Name: {detail.get('name')}\n"
        f"Description: {detail.get('description')}\n"
        f"Status: {detail.get('status')}\n"
        f"Category: {detail.get('category')}\n"
    )


def export_tool_structure(tool_payload: dict, output_dir: Path) -> None:
    detail = tool_payload.get("data", {}).get("userToolDetail", {})
    tool_id = detail.get("id")
    tool_name = normalize_name(detail.get("name", ""))
    tool_dir = output_dir / f"tool_{tool_id}_{tool_name}"
    save_json(tool_payload, tool_dir, f"tool_{tool_id}_{tool_name}.json")
    code = extract_tool_code(tool_payload)
    if code:
        save_text(code, tool_dir, f"code_{tool_name}.py")
    save_text(summarize_tool(tool_payload), tool_dir, f"tool_summary_{tool_id}.txt")


def export_agent_structure(agent_payload: dict, headers: dict, output_dir: Path) -> None:
    detail = agent_payload.get("data", {}).get("agentDetail", {})
    agent_id = detail.get("id") or agent_payload.get("data", {}).get("agentDetail", {}).get("agentId")
    agent_name = normalize_name(detail.get("name", ""))
    agent_dir = output_dir / f"agent_{agent_id}_{agent_name}"
    save_json(agent_payload, agent_dir, f"agent_{agent_id}_{agent_name}.json")
    prompt_text = extract_agent_prompt(agent_payload)
    if prompt_text:
        save_text(prompt_text, agent_dir, f"prompt_{agent_name}.txt")
    save_text(summarize_agent(agent_payload), agent_dir, f"agent_summary_{agent_id}.txt")

    agent_configs = detail.get("agentConfigs", {}) or {}
    tool_refs = (
        detail.get("toolRef")
        or detail.get("userToolRef")
        or agent_configs.get("toolRef")
        or agent_configs.get("userToolRef")
        or []
    )
    for ref in tool_refs:
        tool_id = ref.get("toolId")
        if tool_id:
            try:
                tool_payload = get_tool(str(tool_id), headers)
                export_tool_structure(tool_payload, agent_dir)
            except requests.exceptions.RequestException:
                continue
        elif ref.get("toolClassDef"):
            tool_dir = agent_dir / "tool_inline"
            save_text(ref.get("toolClassDef", ""), tool_dir, "tool_code.py")
            save_text(json.dumps(ref, indent=2), tool_dir, "tool_ref.json")


def export_workflow_structure(workflow_payload: dict, headers: dict, output_dir: Path) -> None:
    detail = workflow_payload.get("data", {}).get("workFlowDetail", {})
    workflow_id = detail.get("id")
    workflow_name = normalize_name(detail.get("name", ""))
    workflow_dir = output_dir / f"workflow_{workflow_id}_{workflow_name}"
    save_json(workflow_payload, workflow_dir, f"workflow_{workflow_id}_{workflow_name}.json")
    save_text(summarize_workflow(workflow_payload), workflow_dir, f"workflow_summary_{workflow_id}.txt")

    agents = workflow_payload.get("data", {}).get("workFlowDetail", {}).get("workflowAgents", [])
    for agent_item in agents:
        agent_id = agent_item.get("agentId")
        if agent_id:
            try:
                agent_payload = get_agent(str(agent_id), headers)
                export_agent_structure(agent_payload, headers, workflow_dir)
                continue
            except requests.exceptions.RequestException:
                pass

        # if agent data is embedded, export it directly
        embedded_agent_detail = agent_item.get("agentDetails", agent_item)
        if isinstance(embedded_agent_detail, dict):
            if agent_id:
                embedded_agent_detail["id"] = agent_id
            if agent_item.get("name"):
                embedded_agent_detail["name"] = agent_item.get("name")
        embedded_payload = {"data": {"agentDetail": embedded_agent_detail}}
        export_agent_structure(embedded_payload, headers, workflow_dir)


def summarize_agent(payload: dict) -> str:
    detail = payload.get("data", {}).get("agentDetail", {})
    agent_configs = detail.get("agentConfigs", {}) or {}
    tool_refs = (
        detail.get("toolRef")
        or detail.get("userToolRef")
        or agent_configs.get("toolRef")
        or agent_configs.get("userToolRef")
        or []
    )
    tool_info = ", ".join([str(item.get("toolId")) for item in tool_refs])
    return (
        f"Agent ID: {detail.get('id')}\n"
        f"Name: {detail.get('name')}\n"
        f"Role: {detail.get('role')}\n"
        f"Goal: {detail.get('goal')}\n"
        f"Status: {detail.get('status')}\n"
        f"Tool references: {tool_info or 'none'}\n"
    )


def summarize_workflow(payload: dict) -> str:
    detail = payload.get("data", {}).get("workFlowDetail", {})
    agent_list = detail.get("workflowAgents", [])
    agents = ", ".join([f"{item.get('agentId')}:{item.get('name')}" for item in agent_list])
    return (
        f"Workflow ID: {detail.get('id')}\n"
        f"Name: {detail.get('name')}\n"
        f"Description: {detail.get('description')}\n"
        f"Status: {detail.get('status')}\n"
        f"Agents: {agents or 'none'}\n"
        f"Created by: {detail.get('createdBy')}\n"
        f"Modified by: {detail.get('modifiedBy')}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="AAVA endpoint helper for tool/agent/workflow metadata.")
    parser.add_argument("command", choices=["tool", "agent", "workflow", "all"], help="Which endpoint to query")
    parser.add_argument("id", help="ID for the selected endpoint")
    parser.add_argument("--token", help="Bearer token to use instead of .env AAVA_TOKEN")
    parser.add_argument("--output-dir", default="aava_endpoint_outputs", help="Directory to save JSON responses")
    parser.add_argument("--no-save", action="store_true", help="Do not save the JSON response to disk")
    args = parser.parse_args()

    try:
        token = args.token.strip() if args.token else load_aava_token()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    headers = get_headers(token)
    output_dir = Path(args.output_dir)

    responses = {}
    try:
        if args.command in ["tool", "all"]:
            responses["tool"] = get_tool(args.id, headers)
            print("--- TOOL ---")
            print(summarize_tool(responses["tool"]))
            if not args.no_save:
                export_tool_structure(responses["tool"], output_dir)
                print(f"Exported tool structure to: {output_dir}")

        if args.command in ["agent", "all"]:
            responses["agent"] = get_agent(args.id, headers)
            print("--- AGENT ---")
            print(summarize_agent(responses["agent"]))
            if not args.no_save:
                export_agent_structure(responses["agent"], headers, output_dir)
                print(f"Exported agent structure to: {output_dir}")

        if args.command in ["workflow", "all"]:
            responses["workflow"] = get_workflow(args.id, headers)
            print("--- WORKFLOW ---")
            print(summarize_workflow(responses["workflow"]))
            if not args.no_save:
                export_workflow_structure(responses["workflow"], headers, output_dir)
                print(f"Exported workflow structure to: {output_dir}")

    except requests.exceptions.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as exc:
        print(f"Request error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
