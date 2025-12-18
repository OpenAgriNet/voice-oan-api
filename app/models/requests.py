from pydantic import BaseModel, Field
from typing import Optional, Literal

class ChatRequest(BaseModel):
    query: str = Field(..., description="The user's chat query")
    session_id: Optional[str] = Field(None, description="Session ID for maintaining conversation context")
    source_lang: Literal['hi', 'mr', 'en'] = Field('mr', description="Source language code (hi=Hindi, mr=Marathi, en=English)")
    target_lang: str = Field('mr', description="Target language code")
    user_id: str = Field('anonymous', description="User identifier")
    provider: Optional[Literal['RAYA', 'RINGG']] = Field(None, description="Provider for the voice service - can be RAYA, RINGG, or None")
    process_id: Optional[str] = Field(None, description="Process ID for tracking and hold messages")

class TranscribeRequest(BaseModel):
    audio_content: str = Field(..., description="Base64 encoded audio content")
    service_type: Literal['bhashini', 'whisper'] = Field('bhashini', description="Transcription service to use")
    session_id: Optional[str] = Field(None, description="Session ID")

class SuggestionsRequest(BaseModel):
    session_id: str = Field(..., description="Session ID to get suggestions for")
    target_lang: str = Field('mr', description="Target language for suggestions")

class TTSRequest(BaseModel):
    text: str = Field(..., description="Text to convert to speech")
    lang_code: str = Field('mr', description="Language code for TTS")
    session_id: Optional[str] = Field(None, description="Session ID")
    service_type: Literal['bhashini', 'eleven_labs'] = Field('bhashini', description="TTS service to use") 