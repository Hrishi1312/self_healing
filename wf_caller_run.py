"""Standalone, runnable version of the RestApiFormDataCaller tool.
 
Removes the crewai / AVASecret dependencies so it can be executed directly:
 
    python wf_caller_run.py
 
Set DRY_RUN = True to print the outgoing payload instead of calling the API.
"""
 
import json
import os
import requests
 
 
def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a .env file next to this script into os.environ.
 
    Existing environment variables take precedence and are not overwritten.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
 
 
_load_dotenv()
 
# Confidence threshold below which the workflow is triggered for re-execution.
_CONFIDENCE_THRESHOLD = 80
 
# Priority is always hardcoded; it is not received from the agent.
_PRIORITY = 1
 
# When True, prints the normalized payload instead of making the HTTP call.
DRY_RUN = False
 
_API_URL = (
    "https://aava-core-api-workflows-svc.redtree-f4541a84.eastus."
    "azurecontainerapps.io/workflows/workflow-executions"
)
 
 
def _get_pat_token() -> str:
    """Read the PAT token from the environment (AAVA_PAT_TOKEN)."""
    token = os.environ.get("AAVA_TOKEN", "")
    if not token and not DRY_RUN:
        raise RuntimeError(
            "AAVA_TOKEN environment variable is not set. "
            "Set it or enable DRY_RUN."
        )
    return token
 
 
def _normalize_form_data(data: dict) -> dict:
    """Normalize all values to strings for multipart form-data encoding.
 
    Nested dicts/lists are JSON-serialized.
    """
    normalized = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            normalized[key] = json.dumps(value)
        elif value is None:
            normalized[key] = ""
        else:
            normalized[key] = str(value)
    return normalized
 
 
def call_workflow(form_data: dict, confidence_score: int) -> str:
    """Check the confidence score and trigger a workflow re-execution if low."""
    try:
        if confidence_score <= 30:
            return (
                "Error: confidence score is too low. Aborting - no valid reviewer "
                "confidence was produced, so the workflow will not be triggered."
            )

        pat_token = _get_pat_token()
        headers = {"Authorization": f"Bearer {pat_token}"}
 
        if confidence_score >= _CONFIDENCE_THRESHOLD:
            return (
                f"Confidence score {confidence_score} meets threshold "
                f"({_CONFIDENCE_THRESHOLD}). No workflow re-execution needed."
            )
 
        # Override priority with the hardcoded value (not from agent).
        form_data["priority"] = _PRIORITY
 
        # AAVA substitutes {{var}} tokens inside the prompt, so the reviewer's
        # userInputs keys arrive holding their own values. Re-key them here.
        # Map the reviewer agent's userInputs directly to the workflow variables.
        # The reviewer prompt always sends:
        #   - tsInputJson
        #   - rvwFeedbackTxt
        #   - reviewinputs (optional)
        #
        # Convert them into the workflow variable names expected by AAVA.
        ui = form_data.get("userInputs") or {}
 
        form_data["userInputs"] = {
            "tsInputJson_string_true": ui.get("tsInputJson", ""),
            "rvwFeedbackTxt_string_false": ui.get("rvwFeedbackTxt", ""),
            "reviewinputs": ui.get("reviewinputs", "")
        }
 
        normalized = _normalize_form_data(form_data)
 
        if DRY_RUN:
            return (
                f"[DRY_RUN] Confidence score {confidence_score} is below "
                f"threshold ({_CONFIDENCE_THRESHOLD}). Would POST to {_API_URL}\n"
                f"Payload:\n{json.dumps(normalized, indent=2)}"
            )
 
        response = requests.post(
            _API_URL,
            files={k: (None, v) for k, v in normalized.items()},
            headers=headers,
        )
        response.raise_for_status()
 
        try:
            body = response.json()
        except Exception:
            body = response.text
 
        return (
            f"Confidence score {confidence_score} is below threshold "
            f"({_CONFIDENCE_THRESHOLD}). Workflow triggered successfully. "
            f"Response: {body}"
        )
 
    except requests.RequestException as e:
        return f"Error during API call: {str(e)}"
 
 
def main() -> None:
    ts_input_json = (
        '[{"scenarioId":"TS_006","title":"Use Eligibility Date for future new '
        'enrollment regardless of Assessment Date","acceptanceCriteriaRef":"AC3 '
        '- Given an inbound EDI 834 file, if the member\u2019s eligibility span '
        'has not yet begun, the Eligibility Date (DTP*356/DTP*348) populates as '
        'the transaction effective date.","type":"Positive","description":"'
        'Validate that for an inbound new enrollment where the member '
        'eligibility span has not yet begun, the transaction effective date in '
        'Facets is populated with the Eligibility Date even when Assessment Date '
        'differs.","priority":"High"},{"scenarioId":"TS_017","title":"Restrict '
        'Maintenance Date usage to specified current eligibility change '
        'scenario","acceptanceCriteriaRef":"AC7 - Given an inbound EDI 834 file, '
        'if the member\u2019s eligibility span has already begun and any '
        'relevant fields have changed but Assessment Date has NOT changed, the '
        'Maintenance Date (DTP*303) populates as the transaction effective '
        'date.","type":"Negative","description":"Validate that Maintenance Date '
        'is used only for the specified scenario of current eligibility with '
        'relevant field changes and unchanged Assessment Date, and not for '
        'other inbound change conditions.","priority":"High"}]'
    )
    rvw_feedback_txt = (
        "Fix traceability and grounding: TC_002 is mapped to TS_017/AC7 but "
        "actually tests a future-eligibility negative case closer to AC6/TS_015, "
        "and both cases need leaner scenario-focused preconditions/steps instead "
        "of excessive environment/setup detail."
    )
 
    form_data = {
        "pipelineId": 163,
        "userInputs": {
            "tsInputJson": ts_input_json,
            "rvwFeedbackTxt": rvw_feedback_txt,
        },
    }
 
    confidence_score = 58  # below threshold -> triggers re-execution
 
    print(call_workflow(form_data, confidence_score))
 
 
if __name__ == "__main__":
    main()