from pydantic import BaseModel, Field
from typing import List, Optional, Literal

class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = Field(default="bharatvistaar-voice", description="Model name")
    messages: List[Message] = Field(..., description="List of messages in the conversation")
    stream: Optional[bool] = Field(default=True, description="Whether to stream the response")
    temperature: Optional[float] = Field(default=1.0, description="Sampling temperature")
    max_tokens: Optional[int] = Field(default=None, description="Maximum tokens to generate")
    user: Optional[str] = Field(default=None, description="User ID for tracking")
    tenant_id: Optional[str] = Field(default=None, description="Tenant ID for multi-tenancy")
