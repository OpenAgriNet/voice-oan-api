from pydantic import BaseModel, Field
from typing import Optional, Literal

class ChatRequest(BaseModel):
    query: str = Field(..., description="The user's chat query")
    session_id: Optional[str] = Field(None, description="Session ID for maintaining conversation context")
    source_lang: Literal['hi', 'mr', 'en'] = Field('mr', description="Source language code (hi=Hindi, mr=Marathi, en=English)")
    target_lang: str = Field('mr', description="Target language code")
    user_id: str = Field('anonymous', description="User identifier")
    provider: Optional[Literal['RAYA']] = Field(None, description="Provider for the voice service - can be RAYA or None")
    process_id: Optional[str] = Field(None, description="Process ID for tracking and hold messages") 