# Import all routers to make them available when importing from app.routers
# This allows main.py to do: from app.routers import chat, transcribe, suggestions, tts
from . import health
from . import openai