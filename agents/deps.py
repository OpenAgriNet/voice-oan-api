from typing import Optional
from pydantic import BaseModel, Field, field_validator
from langcodes import Language


class FarmerContext(BaseModel):
    """Context for the farmer agent.
    
    Args:
        query (str): The user's question.
        lang_code (str): The language code of the user's question.
        session_id (str): The session ID for the conversation.
        moderation_str (Optional[str]): The moderation result of the user's question.


    Example:
        **User:** "What is the weather in Mumbai?"
        **Selected Language:** Hindi
        **Session ID:** "abc123"
        **Moderation Result:** "This is a valid agricultural question."
    """
    query: str = Field(description="The user's question.")
    lang_code: str = Field(description="The language code of the user's question.", default='hi')
    session_id: str = Field(description="The session ID for the conversation.")
    moderation_str: Optional[str] = Field(default=None, description="The moderation result of the user's question.")

    def update_moderation_str(self, moderation_str: str):
        """Update the moderation result of the user's question."""
        self.moderation_str = moderation_str

    def _language_string(self):
        """Get the language string for the voice agent."""
        if self.lang_code:
            return f"**Selected Language:** {Language.get(self.lang_code).display_name()}"
        else:
            return None
    
    def _query_string(self):
        """Get the query string for the voice agent."""
        return "**User:** " + '"' + self.query + '"'

    def _moderation_string(self):
        """Get the moderation string for the voice agent."""
        if self.moderation_str:
            return self.moderation_str
        else:
            return None
    
    def get_user_message(self):
        """Get the user message for the voice agent."""
        strings = [self._query_string(), self._language_string(), self._moderation_string()]
        return "\n".join([x for x in strings if x])