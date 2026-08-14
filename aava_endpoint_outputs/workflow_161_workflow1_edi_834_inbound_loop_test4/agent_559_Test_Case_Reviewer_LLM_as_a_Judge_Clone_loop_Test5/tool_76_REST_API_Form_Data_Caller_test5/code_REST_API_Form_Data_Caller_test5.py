import requests
from typing import Any, Dict, Type
from pydantic import BaseModel, Field, model_validator
from crewai.tools import BaseTool
import json
# Confidence threshold below which the workflow is triggered for re-execution.
# Must match the "approved: true if confidence >= N" line in the reviewer agent's
# prompt. 90 aligns with the reviewer's own "all four basics pass" band; its
# checks 5-8 are hard gates that cap the score at 85 and force a rework round.

_CONFIDENCE_THRESHOLD = 90

# Maximum number of rework rounds. Round 1 is the first re-trigger. Once the
# incoming round_no reaches this value the loop stops and escalates instead of
# posting another run.


_MAX_ROUNDS = 3

# Priority is always hardcoded; it is not received from the agent.
_PRIORITY = 1
# Network timeouts (connect, read) in SECONDS. Must stay well below the
# gateway/ingress ceiling so this client gives up first and returns a readable
# error instead of a truncated 504.
_TIMEOUT = (10, 30)


class RestApiFormDataCallerSchema(BaseModel):
    """Input schema for RestApiFormDataCaller."""
    form_data: Dict[str, Any] = Field(
        ...,
        description=(
            "The full workflow execution payload dict containing: "
            "pipelineId (int), userInputs (dict with keys "
            "{{tsInputJson_string_true}}, {{rvwFeedbackTxt_string_false}}). "
            "Do NOT include priority."
        ),
    )
    confidence_score: int = Field(
        ..., ge=0, le=100, description="Confidence score of the reviewer agent (0-100)."
    )
    pat_token: str = Field(
        ...,
        min_length=1,
        description="AAVA PAT bearer token to authorize the workflow API call.",
    )
    round_no: int = Field(
        0,
        ge=0,
        description=(
            "How many rework rounds have already run for this story. Copy the value "
            "of the roundNo input variable verbatim. Send 0, or omit this argument "
            "entirely, when that variable is empty or absent (that is the first pass)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _lift(cls, v):
        # The model sometimes nests confidence_score inside form_data. Lift it
        # out to the top level so validation succeeds instead of fighting it.
        if isinstance(v, dict) and "confidence_score" not in v:
            fd = v.get("form_data") or {}
            if "confidence_score" in fd:
                v["confidence_score"] = fd.pop("confidence_score")
        # Same for round_no, and coerce the blank/placeholder cases to 0 — form
        # data arrives as strings, and an unbound {{variable}} arrives as its own
        # literal name.
        if isinstance(v, dict):
            fd = v.get("form_data") or {}
            if "round_no" not in v and "round_no" in fd:
                v["round_no"] = fd.pop("round_no")
            raw = v.get("round_no")
            if raw is None or (isinstance(raw, str) and not raw.strip().isdigit()):
                v["round_no"] = 0
        return v


class RestApiFormDataCaller(BaseTool):
    """
    RestApiFormDataCaller - Receives workflow input data, the reviewer confidence
    score, and the current rework round. Triggers a workflow re-execution via the
    AAVA workflow API only when the confidence score is below threshold AND the
    rework limit has not been reached.
    """

    name: str = "REST API Form Data Caller"
    description: str = (
        "Receives workflow input data, the reviewer confidence score, a PAT bearer "
        "token, and the current rework round number. Triggers a workflow "
        "re-execution via the AAVA workflow API when the confidence score is below "
        "threshold and fewer than the maximum number of rework rounds have run. "
        "Stops and escalates once the rework limit is reached."
    )
    args_schema: Type[BaseModel] = RestApiFormDataCallerSchema

    def _run(
        self,
        form_data: Dict[str, Any],
        confidence_score: int,
        pat_token: str,
        round_no: int = 0,
    ) -> str:
        try:
            api_url = "https://aava-core-api-workflows-svc.redtree-f4541a84.eastus.azurecontainerapps.io/workflows/workflow-executions"

            headers = {
                "Authorization": f"Bearer {pat_token}",
            }

            if confidence_score <= 30:
                return (
                    "Error: confidence score is too low. Aborting - no valid reviewer "
                    "confidence was produced, so the workflow will not be triggered."
                )

            if confidence_score >= _CONFIDENCE_THRESHOLD:
                return (
                    f"Confidence score {confidence_score} meets threshold "
                    f"({_CONFIDENCE_THRESHOLD}). No workflow re-execution needed."
                )

            # ---- Rework limit -------------------------------------------------
            # round_no is how many rework rounds have ALREADY run. The run we are
            # about to start would be round_no + 1.
            try:
                current_round = int(round_no)
            except (TypeError, ValueError):
                current_round = 0
            next_round = current_round + 1

            if next_round > _MAX_ROUNDS:
                return (
                    f"Confidence score {confidence_score} is below threshold "
                    f"({_CONFIDENCE_THRESHOLD}), but {current_round} rework rounds "
                    f"have already run and the limit is {_MAX_ROUNDS}. STOPPING - no "
                    f"further workflow will be triggered. Escalate to a human "
                    f"reviewer with the latest feedback."
                )

            # Override priority with the hardcoded value (not from agent).
            form_data["priority"] = _PRIORITY

            # Normalize all values to strings for multipart form-data encoding.
            # Nested dicts/lists are JSON-serialized.
            def normalize_form_data(data: dict) -> dict:
                normalized = {}
                for key, value in data.items():
                    if isinstance(value, (dict, list)):
                        normalized[key] = json.dumps(value)
                    elif value is None:
                        normalized[key] = ""
                    else:
                        normalized[key] = str(value)
                return normalized

            # AAVA substitutes {{var}} tokens inside the prompt, so the reviewer's
            # userInputs keys arrive holding their own values. Re-key them here.
            # roundNo is stamped by this tool, never by the agent - that is what
            # makes the count reliable.

            ui = form_data.get("userInputs") or {}

            form_data["userInputs"] = {
                "tsInputJson_string_true": ui.get("tsInputJson", ""),
                "rvwFeedbackTxt_string_false": ui.get("rvwFeedbackTxt", ""),
                "roundNo_string_false": str(next_round),
                "reviewinputs": ui.get("reviewinputs", ""),
            }

            normalized = normalize_form_data(form_data)

            response = requests.post(
                api_url,
                files={k: (None, v) for k, v in normalized.items()},
                headers=headers,
                timeout=_TIMEOUT,
            )
            response.raise_for_status()

            try:
                body = response.json()
            except Exception:
                body = response.text

            return (
                f"Confidence score {confidence_score} is below threshold "
                f"({_CONFIDENCE_THRESHOLD}). Triggered rework round {next_round} of "
                f"{_MAX_ROUNDS}. Response: {body}"
            )

        except requests.RequestException as e:
            return f"Error during API call: {str(e)}"