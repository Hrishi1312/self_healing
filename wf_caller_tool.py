import requests
from typing import Any, Dict, Type
from pydantic import BaseModel, Field, model_validator
from crewai.tools import BaseTool
import json

# Confidence threshold below which the workflow is triggered for re-execution.
_CONFIDENCE_THRESHOLD = 80

# Priority is always hardcoded; it is not received from the agent.
_PRIORITY = 1


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

    @model_validator(mode="before")
    @classmethod
    def _lift(cls, v):
        # The model sometimes nests confidence_score inside form_data. Lift it
        # out to the top level so validation succeeds instead of fighting it.
        if isinstance(v, dict) and "confidence_score" not in v:
            fd = v.get("form_data") or {}
            if "confidence_score" in fd:
                v["confidence_score"] = fd.pop("confidence_score")
        return v


class RestApiFormDataCaller(BaseTool):
    """
    RestApiFormDataCaller - Receives workflow input data and the reviewer
    confidence score, checks the confidence against the threshold, and triggers
    a workflow re-execution via the AAVA workflow API if it is below the
    threshold. If the score meets or exceeds the threshold, no API call is made.
    """

    name: str = "REST API Form Data Caller"
    description: str = (
        "Receives workflow input data, the reviewer confidence score, and a PAT "
        "bearer token. Triggers a workflow re-execution via the AAVA workflow "
        "API when the confidence score is below threshold."
    )
    args_schema: Type[BaseModel] = RestApiFormDataCallerSchema

    def _run(self, form_data: Dict[str, Any], confidence_score: int, pat_token: str) -> str:
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
                "reviewinputs": ui.get("reviewinputs", ""),
            }

            normalized = normalize_form_data(form_data)

            response = requests.post(
                api_url,
                files={k: (None, v) for k, v in normalized.items()},
                headers=headers,
            )
            response.raise_for_status()

            try:
                return (
                    f"Confidence score {confidence_score} is below threshold "
                    f"({_CONFIDENCE_THRESHOLD}). Workflow triggered successfully. "
                    f"Response: {response.json()}"
                )
            except Exception:
                return (
                    f"Confidence score {confidence_score} is below threshold "
                    f"({_CONFIDENCE_THRESHOLD}). Workflow triggered successfully. "
                    f"Response: {response.text}"
                )

        except requests.RequestException as e:
            return f"Error during API call: {str(e)}"