from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator
from langcodes import Language


class FarmerContext(BaseModel):
    """Context for the farmer agent.
    
    Args:
        query (str): The user's question.
        lang_code (str): The language code of the user's question.
        target_lang (str): The target language for the response (gu=Gujarati, en=English).
        provider (Optional[str]): The provider for the voice service.
        session_id (Optional[str]): The session ID for the user.
        process_id (Optional[str]): The process ID for tracking and hold messages.


    Example:
        **User:** "What is the weather in Mumbai?"
        **Selected Language:** Gujarati
    """
    query: str = Field(description="The user's question.")
    lang_code: str = Field(description="The language code of the user's question.", default='gu')
    target_lang: str = Field(description="The target language for the response (gu=Gujarati, en=English).", default='gu')
    provider: Optional[Literal['RAYA']] = Field(default=None, description="The provider for the voice service - can be RAYA or None.")
    session_id: Optional[str] = Field(default=None, description="The session ID for the user.")
    process_id: Optional[str] = Field(default=None, description="The process ID for tracking and hold messages.")
    
    def _language_string(self):
        """Get the language string for the agrinet agent."""
        # Use target_lang if available, otherwise fall back to lang_code
        lang = self.target_lang or self.lang_code
        if lang:
            return f"**Selected Language:** {Language.get(lang).display_name()}"
        else:
            return None
    
    def _query_string(self):
        """Get the query string for the agrinet agent."""
        return "**User:** " + '"' + self.query + '"'

    # NOTE: Do not need to tell LLM which provider is used - we just need to store the provider value in deps.
    # def _provider_string(self):
    #     """Get the provider string for the agrinet agent."""
    #     if self.provider:
    #         return f"**Provider:** {self.provider}"
    #     else:
    #         return None

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        strings = [self._query_string(), self._language_string()]
        return "\n".join([x for x in strings if x])