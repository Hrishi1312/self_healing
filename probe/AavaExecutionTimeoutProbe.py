import json
import time
from datetime import datetime, timezone
from typing import Type

from pydantic import BaseModel, Field
from crewai.tools import BaseTool

# ─────────────────────────────────────────────────────────────────────────────
# AavaExecutionTimeoutProbe
#
# A deliberate long-runner. It burns wall-clock time, emits a timestamped
# heartbeat as it goes, and then raises. Its only purpose is to find out where
# a run actually dies: inside this tool, or at the platform's execution ceiling.
#
# Why it exists: AAVA activity logs carry no timestamps of their own, and a
# 600-second pipeline ceiling has been observed killing runs while the agents
# are configured with maxExecutionTime 3600. This probe produces its own
# timestamps so that gap can be measured rather than guessed.
#
# HOW TO READ THE RESULT
#   - Probe runs the full duration and you see the raised error  -> the tool's
#     own limit was reached first. The platform did not interfere.
#   - Run dies with "Execution timed out after Ns" and you never see the raised
#     error -> the PLATFORM killed it at N seconds. That N is the real ceiling.
#
# So: set timeout_seconds ABOVE the suspected ceiling to measure the ceiling,
# and BELOW it to confirm the tool's own error surfaces to the agent.
#
# NOTE ON HOUSE CONVENTION: tools in this codebase normally return
# json.dumps({"error": ...}) rather than raising, because a raised exception is
# opaque to the agent. This tool raises on purpose — observing how a raised
# exception propagates is the point. Set raise_on_completion=false to get the
# conventional JSON-return behaviour instead.
# ─────────────────────────────────────────────────────────────────────────────

# Default run duration in SECONDS. 1200 = 20 minutes.
_DEFAULT_TIMEOUT_SECONDS = 1200

# How often to emit a heartbeat, in SECONDS.
_DEFAULT_HEARTBEAT_SECONDS = 30

# Hard safety rail. Refuse to run longer than this no matter what is passed in,
# so a bad input cannot pin a worker for hours.
_MAX_TIMEOUT_SECONDS = 3600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AavaExecutionTimeoutProbeSchema(BaseModel):
    """Input schema for AavaExecutionTimeoutProbe."""

    timeout_seconds: int = Field(
        _DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        le=_MAX_TIMEOUT_SECONDS,
        description=(
            "How long the probe should run, in SECONDS. Default 1200 (20 minutes). "
            "Maximum 3600. Pass the value of the timeoutSeconds input variable when "
            "one is supplied; otherwise omit this argument and the default is used."
        ),
    )
    heartbeat_seconds: int = Field(
        _DEFAULT_HEARTBEAT_SECONDS,
        ge=1,
        le=600,
        description=(
            "How often the probe reports progress, in SECONDS. Default 30. Lower "
            "values give finer resolution on where a run was cut off."
        ),
    )
    raise_on_completion: bool = Field(
        True,
        description=(
            "When true (the default) the probe raises a RuntimeError once the "
            "duration elapses, so you can observe how a raised exception surfaces. "
            "When false it returns a normal JSON result instead."
        ),
    )
    label: str = Field(
        "",
        description=(
            "Optional free-text label echoed into every heartbeat and into the "
            "final message, so one probe run can be told apart from another."
        ),
    )


class AavaExecutionTimeoutProbe(BaseTool):
    """
    AavaExecutionTimeoutProbe - runs for a configurable number of seconds,
    emitting timestamped heartbeats, then raises a RuntimeError. Used to locate
    the real execution ceiling of an AAVA workflow run.
    """

    name: str = "Aava Execution Timeout Probe"
    description: str = (
        "Runs for a configurable duration (default 20 minutes) while emitting "
        "timestamped heartbeats, then raises an error. Use it to find out how "
        "long a workflow run is actually allowed to last before the platform "
        "stops it. Takes timeout_seconds, heartbeat_seconds, raise_on_completion "
        "and an optional label. Call it exactly once."
    )
    args_schema: Type[BaseModel] = AavaExecutionTimeoutProbeSchema

    def _run(self, **kwargs) -> str:
        # CrewAI sometimes nests the real arguments under a "kwargs" key.
        a = kwargs.get("kwargs", kwargs)

        try:
            timeout_seconds = int(a.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout_seconds = _DEFAULT_TIMEOUT_SECONDS
        try:
            heartbeat_seconds = int(a.get("heartbeat_seconds") or _DEFAULT_HEARTBEAT_SECONDS)
        except (TypeError, ValueError):
            heartbeat_seconds = _DEFAULT_HEARTBEAT_SECONDS

        timeout_seconds = max(1, min(timeout_seconds, _MAX_TIMEOUT_SECONDS))
        heartbeat_seconds = max(1, min(heartbeat_seconds, 600))

        raise_on_completion = a.get("raise_on_completion", True)
        if isinstance(raise_on_completion, str):
            raise_on_completion = raise_on_completion.strip().lower() not in ("false", "0", "no", "")

        label = str(a.get("label") or "probe")

        started_wall = _now()
        started = time.monotonic()
        beats = []

        print(f"[PROBE] {started_wall} label={label} START "
              f"timeout_seconds={timeout_seconds} heartbeat_seconds={heartbeat_seconds}")

        while True:
            elapsed = time.monotonic() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                break
            time.sleep(min(heartbeat_seconds, remaining))
            elapsed = int(time.monotonic() - started)
            beat = f"{_now()} elapsed={elapsed}s"
            beats.append(beat)
            # Printed so it appears in stdout-backed logs; the return value and
            # the raised message are the reliable signals.
            print(f"[PROBE] {beat} label={label} remaining={max(0, timeout_seconds - elapsed)}s")

        elapsed_total = int(time.monotonic() - started)
        finished_wall = _now()

        summary = {
            "label": label,
            "outcome": "completed_full_duration",
            "requested_timeout_seconds": timeout_seconds,
            "elapsed_seconds": elapsed_total,
            "heartbeat_seconds": heartbeat_seconds,
            "heartbeats_emitted": len(beats),
            "started_utc": started_wall,
            "finished_utc": finished_wall,
            "first_heartbeat": beats[0] if beats else None,
            "last_heartbeat": beats[-1] if beats else None,
            "interpretation": (
                "The probe ran its full requested duration without being stopped, "
                "so the platform execution ceiling is greater than "
                f"{elapsed_total} seconds."
            ),
        }

        if raise_on_completion:
            raise RuntimeError(
                "[PROBE] DELIBERATE FAILURE after "
                f"{elapsed_total}s (requested {timeout_seconds}s) label={label} "
                f"started={started_wall} finished={finished_wall} "
                f"heartbeats={len(beats)} — the probe reached its own limit, which "
                "means the platform did NOT stop the run first."
            )

        return json.dumps(summary)
