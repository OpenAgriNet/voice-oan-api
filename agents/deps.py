from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator
from langcodes import Language


class FarmerContext(BaseModel):
    """Context for the farmer agent.
    
    Args:
        query (str): The user's question.
        lang_code (str): The language code of the user's question.
        target_lang (str): The target language for the response (hi=Hindi, mr=Marathi, en=English).
        moderation_str (Optional[str]): The moderation result of the user's question.
        provider (Optional[str]): The provider for the voice service.
        session_id (Optional[str]): The session ID for the user.
        process_id (Optional[str]): The process ID for tracking and hold messages.


    Example:
        **User:** "What is the weather in Mumbai?"
        **Selected Language:** Marathi
        **Moderation Result:** "This is a valid agricultural question."
    """
    query: str = Field(description="The user's question.")
    lang_code: str = Field(description="The language code of the user's question.", default='mr')
    target_lang: str = Field(description="The target language for the response (hi=Hindi, mr=Marathi, en=English).", default='mr')
    moderation_str: Optional[str] = Field(default=None, description="The moderation result of the user's question.")
    provider: Optional[Literal['RAYA', 'RINGG']] = Field(default=None, description="The provider for the voice service - can be RAYA, RINGG, or None.")
    session_id: Optional[str] = Field(default=None, description="The session ID for the user.")
    process_id: Optional[str] = Field(default=None, description="The process ID for tracking and hold messages.")

    def update_moderation_str(self, moderation_str: str):
        """Update the moderation result of the user's question."""
        self.moderation_str = moderation_str

        
    def get_moderation_str(self) -> Optional[str]:
        """Get the moderation result of the user's question."""
        return self.moderation_str
    
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

    def _moderation_string(self):
        """Get the moderation string for the agrinet agent."""
        if self.moderation_str:
            return self.moderation_str
        else:
            return None
    # NOTE: Do not need to tell LLM which provider is used - we just need to store the provider value in deps.
    # def _provider_string(self):
    #     """Get the provider string for the agrinet agent."""
    #     if self.provider:
    #         return f"**Provider:** {self.provider}"
    #     else:
    #         return None

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        strings = [self._query_string(), self._language_string(),
         # self._provider_string(), 
         self._moderation_string()]
        return "\n".join([x for x in strings if x])