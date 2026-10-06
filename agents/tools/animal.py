"""
Tool for fetching animal details by tag number from PashuGPT-style APIs.
Backed by amulpashudhan.com.
"""
import json
import os
from typing import Any, Dict, Optional

from helpers.utils import get_logger

from agents.tools.farmer_animal_backends import (
    fetch_animal_amulpashudhan,
    normalize_tag,
)

logger = get_logger(__name__)


async def get_animal_by_tag(tag_no: str) -> str:
    """
    Fetch animal information by tag number. Returns details including breed,
    milking stage, pregnancy stage, lactation, date of birth, and last
    breeding/health activities.

    Args:
        tag_no: The tag number of the animal (required).

    Returns:
        str: Formatted JSON string with animal details, or a clear message if no data found.
             Handles API failures, 204 No Content, and empty responses.
    """
    tag = normalize_tag(tag_no)
    if not tag:
        return "Please provide a valid tag number."

    token1 = os.getenv("PASHUGPT_TOKEN")
    if not token1:
        logger.error("PASHUGPT_TOKEN is not set")
        raise ValueError("PASHUGPT_TOKEN environment variable must be set")

    animal: Optional[Dict[str, Any]] = None
    try:
        animal = await fetch_animal_amulpashudhan(tag, token1)
        if animal:
            logger.info(f"Animal data for tag {tag}: got from amulpashudhan")
    except Exception as e:
        logger.warning(f"amulpashudhan animal API error for tag {tag}: {e}")

    if not animal:
        logger.info(f"No animal data found for tag {tag}")
        return f"Animal details for tag {tag}:\n\nNo animal data found for this tag number."

    formatted = json.dumps(animal, indent=2, ensure_ascii=False)
    return f"Animal details for tag {tag}:\n\n{formatted}"
