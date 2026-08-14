import requests
from typing import Any, Dict, Type
from pydantic import BaseModel, Field, model_validator
from crewai.tools import BaseTool
from datetime import datetime, timezone
import json
import time

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

# Prefix on every log line this tool emits. Grep the activity log for it to get
# one line per decision, in order, with timestamps.
_LOG_TAG = "[AAVA-LOOP]"


def _now() -> str:
    """UTC timestamp. The platform activity log carries no timestamps of its
    own, so this is the only wall-clock reference available when reviewing a run."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(decision: str, **fields: Any) -> str:
    """One compact, greppable line. Kept short on purpose: this string lands in
    the calling agent's context, and a verbose blob risks being echoed into the
    agent's own output."""
    parts = [f"{_LOG_TAG} ts={_now()}", f"decision={decision}"]
    parts += [f"{k}={v}" for k, v in fields.items() if v is not None and v != ""]
    return " ".join(parts)


def _execution_id(body: Any) -> str:
    """Pull the child execution id out of the trigger response so a rework run
    can be correlated back to the run that started it. Key name varies, so try
    the known shapes before giving up."""
    if isinstance(body, dict):
        for key in ("execution_id", "executionId", "executionID", "id"):
            if body.get(key):
                return str(body[key])
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("execution_id", "executionId", "executionID", "id"):
                if data.get(key):
                    return str(data[key])
    return "unknown"


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

    Every exit path returns one compact log line prefixed with [AAVA-LOOP],
    carrying a UTC timestamp, the decision, the inputs that drove it, and the
    child execution id when a run was started.
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
        started = time.monotonic()
        try:
            api_url = "https://aava-core-api-workflows-svc.redtree-f4541a84.eastus.azurecontainerapps.io/workflows/workflow-executions"

            headers = {
                "Authorization": f"Bearer {pat_token}",
            }

            # ---- Measure the payload so ballooning is visible in the log ------
            ui = form_data.get("userInputs") or {}
            ts_in = str(ui.get("tsInputJson", "") or "")
            fb_in = str(ui.get("rvwFeedbackTxt", "") or "")
            scenario_count = ts_in.count('"scenarioId"')
            sizes = {
                "scenarios": scenario_count,
                "scenario_chars": len(ts_in),
                "feedback_chars": len(fb_in),
            }

            try:
                current_round = int(round_no)
            except (TypeError, ValueError):
                current_round = 0
            next_round = current_round + 1

            if confidence_score <= 30:
                return _log(
                    "ABORT_LOW_CONFIDENCE",
                    confidence=confidence_score,
                    threshold=_CONFIDENCE_THRESHOLD,
                    round=f"{current_round}/{_MAX_ROUNDS}",
                    note="score_too_low_to_be_a_real_verdict",
                    **sizes,
                )

            if confidence_score >= _CONFIDENCE_THRESHOLD:
                return _log(
                    "APPROVED_STOP",
                    confidence=confidence_score,
                    threshold=_CONFIDENCE_THRESHOLD,
                    round=f"{current_round}/{_MAX_ROUNDS}",
                    note="met_threshold_no_rework_needed",
                    **sizes,
                )

            if next_round > _MAX_ROUNDS:
                return _log(
                    "LIMIT_REACHED",
                    confidence=confidence_score,
                    threshold=_CONFIDENCE_THRESHOLD,
                    round=f"{current_round}/{_MAX_ROUNDS}",
                    note="rework_limit_reached_escalate_to_human",
                    **sizes,
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
            form_data["userInputs"] = {
                "tsInputJson_string_true": ts_in,
                "rvwFeedbackTxt_string_false": fb_in,
                "roundNo_string_false": str(next_round),
                "reviewinputs": ui.get("reviewinputs", ""),
            }

            normalized = normalize_form_data(form_data)

            post_started = time.monotonic()
            response = requests.post(
                api_url,
                files={k: (None, v) for k, v in normalized.items()},
                headers=headers,
                timeout=_TIMEOUT,
            )
            post_ms = int((time.monotonic() - post_started) * 1000)
            response.raise_for_status()

            try:
                body = response.json()
            except Exception:
                body = response.text

            return _log(
                "REWORK_TRIGGERED",
                confidence=confidence_score,
                threshold=_CONFIDENCE_THRESHOLD,
                round=f"{next_round}/{_MAX_ROUNDS}",
                pipeline=form_data.get("pipelineId"),
                child_execution_id=_execution_id(body),
                http_status=response.status_code,
                post_ms=post_ms,
                total_ms=int((time.monotonic() - started) * 1000),
                **sizes,
            )

        except requests.Timeout:
            return _log(
                "API_TIMEOUT",
                confidence=confidence_score,
                round=f"{round_no}/{_MAX_ROUNDS}",
                timeout_s=f"{_TIMEOUT[0]}c/{_TIMEOUT[1]}r",
                total_ms=int((time.monotonic() - started) * 1000),
                note="client_gave_up_before_gateway",
            )
        except requests.RequestException as e:
            return _log(
                "API_ERROR",
                confidence=confidence_score,
                round=f"{round_no}/{_MAX_ROUNDS}",
                total_ms=int((time.monotonic() - started) * 1000),
                error=str(e)[:160].replace(" ", "_"),
            )
