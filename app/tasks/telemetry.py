"""
Tasks for sending telemetry data.
"""
from dotenv import load_dotenv
from typing import Dict

from helpers.telemetry import post_telemetry_payload
from helpers.utils import get_logger

load_dotenv()

logger = get_logger(__name__)


async def send_telemetry(telemetry_data: Dict) -> Dict:
    """
    Background task to send telemetry events to the API (with retries on failure).

    Args:
        telemetry_data: The telemetry data to send

    Returns:
        Dict containing status code and response, or {"error": "..."} if all retries failed.
    """
    response = post_telemetry_payload(telemetry_data)
    if response is None:
        return {"error": "Telemetry POST failed after retries or TELEMETRY_API_URL unset"}
    result = {
        "status_code": response.status_code,
        "response": response.json() if response.status_code == 200 else response.text,
    }
    logger.info("Telemetry sent successfully: %s", result)
    return result 