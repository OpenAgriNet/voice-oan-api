from pydantic import BaseModel, Field


class FarmerContext(BaseModel):
    """Context for the farmer agent.

    Args:
        query (str): The user's question.
        session_id (str): The session ID for the conversation.
        user_id (str): The user ID for the conversation.

    No language is carried here. The agent detects the conversation language from
    the farmer's own words and reports it on ``VoiceOutput.language``. Nothing in
    the user message names a language, deliberately — a "Selected Language" hint
    used to override what the farmer actually spoke.

    Example:
        **User:** "What is the weather in Mumbai?"
    """
    query: str = Field(description="The user's question.")
    session_id: str = Field(description="The session ID for the conversation.")
    user_id: str = Field(description="The user ID for the conversation.")
    # Per-turn id, forwarded to the mandi provider for request correlation.
    question_id: str = Field(default="", description="The question ID for this turn.")

    def _query_string(self):
        """Get the query string for the agrinet agent."""
        return "**User:** " + '"' + self.query + '"'

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        return self._query_string()
