from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
import time

class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = Field(default="gpt-3.5-turbo", description="Model name")
    messages: List[Message] = Field(..., description="List of messages in the conversation")
    stream: Optional[bool] = Field(default=True, description="Whether to stream the response")
    temperature: Optional[float] = Field(default=1.0, description="Sampling temperature")
    max_tokens: Optional[int] = Field(default=None, description="Maximum tokens to generate")
    user: Optional[str] = Field(default=None, description="User ID for tracking")
    tenant_id: Optional[str] = Field(default=None, description="Tenant ID for multi-tenancy")

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    
class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Optional[Dict[str, int]] = None
