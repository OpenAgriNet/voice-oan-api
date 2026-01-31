import os
from dotenv import load_dotenv

load_dotenv()

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
    base_url = (
        os.getenv("LANGFUSE_BASE_URL")
        or (settings.langfuse_base_url if settings.langfuse_base_url else None)
        or "https://cloud.langfuse.com"
    )

    # Optional: set OpenTelemetry resource so service.name appears in Langfuse metadata
    tracer_provider = None
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource

        tracer_provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": release,
                    "deployment.environment": environment,
                }
            )
        )
    except ImportError:
        pass

    from langfuse import Langfuse

    Langfuse(
        public_key=langfuse_public_key,
        secret_key=langfuse_secret_key,
        base_url=base_url,
        release=release,
        environment=environment,
        tracer_provider=tracer_provider,
    )
    has_otel_exporter = True

# Enable Pydantic AI instrumentation if at least one exporter is configured
if has_otel_exporter:
    from pydantic_ai.agent import Agent
    Agent.instrument_all()
