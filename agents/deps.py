from pydantic import BaseModel, Field


class FarmerContext(BaseModel):
    """Context for the farmer agent.

    Args:
        query (str): The user's question.
        session_id (str): The session ID for the conversation.
        user_id (str): The user ID for the conversation.
        language_code (str | None): Backend-owned session language lock.

    Language is never accepted from the client. ``language_code`` is populated
    only from the backend's session lock.

    Example:
        **User:** "What is the weather in Mumbai?"
    """
    query: str = Field(description="The user's question.")
    session_id: str = Field(description="The session ID for the conversation.")
    user_id: str = Field(description="The user ID for the conversation.")
    language_code: str | None = Field(
        default=None, description="The backend-owned locked conversation language."
    )

    def _query_string(self):
        """Get the query string for the agrinet agent."""
        return "**User:** " + '"' + self.query + '"'

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        return self._query_string()
