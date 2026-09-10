from pydantic import BaseModel, Field


class FarmerContext(BaseModel):
    """Context for the farmer agent.
    
    Args:
        query (str): The user's question.
        session_id (str): The session ID for the conversation.
        user_id (str): The user ID for the conversation.
        language_code (str): Internal language selected from X-Language.

    ``language_code`` is validated from Sarvam's ISO 639-1 X-Language header at
    the API boundary.

    Example:
        **User:** "What is the weather in Mumbai?"
    """
    query: str = Field(description="The user's question.")
    session_id: str = Field(description="The session ID for the conversation.")
    user_id: str = Field(description="The user ID for the conversation.")
    language_code: str = Field(
        description="Language selected from the validated X-Language header."
    )
    
    def _query_string(self):
        """Get the query string for the agrinet agent."""
        return "**User:** " + '"' + self.query + '"'

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        return self._query_string()
