import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Track if any OTEL exporter is configured
has_otel_exporter = False

# Conditionally configure Logfire if token is set
if os.getenv("LOGFIRE_TOKEN"):
    import logfire
    logfire.configure(scrubbing=False)
    has_otel_exporter = True

# Conditionally configure Langfuse if env vars are set
langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")
langfuse_client = None

if langfuse_public_key and langfuse_secret_key:
    from app.config import settings

    # Labels for Langfuse: identify all traces as from this service
    release = (
        os.getenv("LANGFUSE_RELEASE")
        or settings.langfuse_release
        or "voice-oan-api"
    )
    environment = (
        os.getenv("LANGFUSE_TRACING_ENVIRONMENT")
        or settings.langfuse_environment
        or settings.environment
        or "production"
    )
    # SDK v3 uses LANGFUSE_HOST env var, but we also support LANGFUSE_BASE_URL
    host = (
        os.getenv("LANGFUSE_HOST")
        or os.getenv("LANGFUSE_BASE_URL")
        or (settings.langfuse_base_url if settings.langfuse_base_url else None)
        or "https://cloud.langfuse.com"
    )
    
    # Set environment variables for SDK v3 auto-configuration
    os.environ.setdefault("LANGFUSE_HOST", host)

    print(f"🔍 Langfuse initializing: host={host}, release={release}, environment={environment}", flush=True)

    from langfuse import get_client

    langfuse_client = get_client()
    
    # Verify connection
    if langfuse_client.auth_check():
        print("✅ Langfuse initialized successfully - authentication verified", flush=True)
        has_otel_exporter = True
    else:
        print("❌ Langfuse authentication failed - traces will not be sent", flush=True)
else:
    print("ℹ️  Langfuse not configured - LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set", flush=True)

# Enable Pydantic AI instrumentation if at least one exporter is configured
if has_otel_exporter:
    from pydantic_ai.agent import Agent
    Agent.instrument_all()
    print("📊 Pydantic AI instrumentation enabled", flush=True)
