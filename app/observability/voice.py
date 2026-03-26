import json
from typing import Any, Dict


def voice_output_summary(last_chunk: str) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "audio": "",
        "end_interaction": False,
    }
    if not last_chunk:
        return summary
    try:
        parsed = json.loads(last_chunk)
        if isinstance(parsed, dict):
            summary["audio"] = parsed.get("audio") or ""
            summary["end_interaction"] = bool(parsed.get("end_interaction", False))
    except (json.JSONDecodeError, TypeError, ValueError):
        return summary
    return summary


def safe_update_observation(observation: Any, output: Dict[str, Any]) -> None:
    if observation is None or not hasattr(observation, "update"):
        return
    try:
        observation.update(output=output)
    except (TypeError, AttributeError):
        return
