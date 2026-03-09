"""
Feedback tool for collecting call feedback (like/dislike) and improvement suggestions before ending the interaction.
"""
import os
from typing import Literal, Optional

import requests
from pydantic import BaseModel, Field
from pydantic_ai.tools import RunContext

from agents.deps import FarmerContext
from helpers.telemetry import TelemetryRequest, create_feedback_event
from helpers.utils import get_logger

logger = get_logger(__name__)

TELEMETRY_API_URL = os.getenv(
    "TELEMETRY_API_URL"
)


class FeedbackInput(BaseModel):
    """Input for submitting call feedback. Call this tool only after the user has given a feedback type (and optionally what went wrong or how to improve)."""

    feedback_type: Literal["like", "dislike"] = Field(
        ...,
        description="The type of feedback: 'like' for positive feedback, 'dislike' for negative feedback. Set only when the user has stated their feedback.",
    )
    feedback_text: str = Field(
        default=None,
        description="What went wrong or how we can improve the experience, in the user's words. Leave empty if the user did not provide this or said nothing/skip.",
    )


def submit_feedback(ctx: RunContext[FarmerContext], feedback: FeedbackInput) -> str:
    """
    Submit call feedback before ending the interaction. Use this tool ONLY when:
    (1) The farmer has indicated they want to end the call, and
    (2) You have asked for a feedback type and the farmer has given a feedback type, and
    (3) You have asked what went wrong or how to improve (the farmer may skip or say nothing).
    Call this tool with the feedback type and any improvement text the user provided, then  thank them for the feedback and give your closing response and set end_interaction to true.
    """
    session_id = ctx.deps.session_id
    user_id = ctx.deps.user_id or "guest"
    logger.info(
        "Voice feedback: session_id=%s, user_id=%s, feedback_type=%s, feedback_text=%s",
        session_id,
        user_id,
        feedback.feedback_type,
        feedback.feedback_text or "(none)",
    )

    feedback_type = feedback.feedback_type
    feedback_text = (
        feedback.feedback_text or f"Feedback type: {feedback.feedback_type}"

    ).strip()

    # Build and send Ekstep telemetry (OE_ITEM_RESPONSE with type=Feedback)
    try:
        event = create_feedback_event(
            session_id=session_id,
            feedback_type=feedback_type,
            feedback_text=feedback_text,
            uid=user_id,
            #rating=feedback.rating,
        )
        telemetry_request = TelemetryRequest(events=[event])
        payload = telemetry_request.model_dump(mode="json")
        response = requests.post(
            TELEMETRY_API_URL,
            headers={
                "Accept": "*/*",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "dataType": "json",
            },
            json=payload,
            timeout=(30, 60),
        )
        if response.status_code == 200:
            logger.info("Feedback telemetry sent successfully")
        else:
            logger.warning(
                "Feedback telemetry returned status %s: %s",
                response.status_code,
                response.text[:200],
            )
    except Exception as e:
        logger.exception("Failed to send feedback telemetry: %s", e)

    return "Feedback recorded. Thank you for your feedback. Thank you for calling the Bharat VISTAAR helpline. Have a good day."
