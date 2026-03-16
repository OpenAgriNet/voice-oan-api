from typing import Optional
from pydantic import BaseModel, Field
from langcodes import Language


class FarmerContext(BaseModel):
    """Context for the farmer agent.
    
    Args:
        query (str): The user's question.
        lang_code (str): The language code of the user's question.
        session_id (str): The session ID for the conversation.


    Example:
        **User:** "What is the weather in Mumbai?"
        **Selected Language:** Hindi
    """
    query: str = Field(description="The user's question.")
    lang_code: str = Field(description="The language code of the user's question.", default='hi')
    session_id: str = Field(description="The session ID for the conversation.")
    user_id: str = Field(description="The user ID for the conversation.")
    selected_language: Optional[str] = Field(default=None, description="Language chosen by the user via set_language tool call ('en' or 'hi').")

    def _language_string(self):
        """Get the language string for the agrinet agent."""
        if self.lang_code and self.lang_code in ("en", "hi"):
            return f"**Selected Language:** {Language.get(self.lang_code).display_name()}"
        else:
            return None
    
    def _query_string(self):
        """Get the query string for the agrinet agent."""
        return "**User:** " + '"' + self.query + '"'

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        strings = [self._query_string(), self._language_string()]
        return "\n".join([x for x in strings if x])