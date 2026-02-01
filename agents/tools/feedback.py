"""
Feedback-related tools and utilities.
"""
from typing import Literal
from pydantic_ai import RunContext
from helpers.utils import get_logger
from agents.deps import FarmerContext

logger = get_logger(__name__)


def signal_conversation_state(
    ctx: RunContext[FarmerContext],
    event: Literal["conversation_closing", "user_frustration", "in_progress"],
) -> str:
    """
    Signal the current conversation state. Call this when you detect the farmer is wrapping up
    the call (conversation_closing) or showing frustration (user_frustration). Use in_progress
    for normal ongoing conversation.

    Call conversation_closing when:
    - Task completion – you have finished answering and the farmer's need is met
    - Farmer declines further help – says "ના" / "No" to "તમને બીજી કોઈ માહિતી જોઈએ છે?" / "Do you need any other information?"
    - Farmer says they are done, thanks, goodbye, or wants to end the call
    - After you've given the closing line and farmer acknowledges

    Call user_frustration when:
    - Farmer corrects you ("ના તે નથી", "that's not what I meant")
    - Farmer repeats the same request
    - Farmer seems confused or unhappy with the response

    Args:
        ctx: Run context with session info
        event: One of conversation_closing, user_frustration, in_progress

    Returns:
        Confirmation string (not shown to user)
    """
    logger.info(f"Conversation state signaled: {event}")
    return f"State {event} recorded."
