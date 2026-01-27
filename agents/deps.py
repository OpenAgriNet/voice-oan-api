from typing import Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, field_validator
from langcodes import Language


class FarmerContext(BaseModel):
    """Context for the farmer agent.
    
    Args:
        query (str): The user's question.
        lang_code (str): The language code of the user's question.
        target_lang (str): The target language for the response (gu=Gujarati, en=English).        
        farmer_info (Optional[Dict[str, Any]]): Farmer's personal details and animals from JWT token.
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
    farmer_info: Optional[Dict[str, Any]] = Field(default=None, description="Farmer's personal details and animals from JWT token.")

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

    def get_farmer_context_string(self) -> Optional[str]:
        """Format farmer context information from JWT token for the system prompt."""
        if not self.farmer_info:
            return None
        
        def format_value(value: Any, indent: int = 0) -> str:
            """Recursively format values for display."""
            indent_str = "  " * indent
            if isinstance(value, dict):
                if not value:
                    return "{}"
                lines = []
                for k, v in value.items():
                    formatted_value = format_value(v, indent + 1)
                    if isinstance(v, (dict, list)) and v:
                        lines.append(f"{indent_str}- **{k}:**\n{formatted_value}")
                    else:
                        lines.append(f"{indent_str}- **{k}**: {formatted_value}")
                return "\n".join(lines)
            elif isinstance(value, list):
                if not value:
                    return "[]"
                lines = []
                for i, item in enumerate(value):
                    formatted_item = format_value(item, indent + 1)
                    if isinstance(item, (dict, list)) and item:
                        lines.append(f"{indent_str}- Item {i + 1}:\n{formatted_item}")
                    else:
                        lines.append(f"{indent_str}- {formatted_item}")
                return "\n".join(lines)
            else:
                return str(value)
        
        # Format the entire farmer_info dict
        formatted_context = format_value(self.farmer_info, indent=0)
        return formatted_context

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        strings = [self._query_string(), self._language_string()]
        return "\n".join([x for x in strings if x])