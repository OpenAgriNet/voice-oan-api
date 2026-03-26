from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from app.config import settings
from contextlib import asynccontextmanager

load_dotenv()

# Import all routers
from app.routers import health, openai
from app.observability.langfuse_client import get_langfuse, safe_flush

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events for startup and shutdown"""
    # Startup
    print(f"🚀 {settings.app_name} starting up...")
    print(f"📍 Environment: {settings.environment}")
    print(f"🔐 Auth enabled: {settings.auth_enabled}")
    print(f"🔧 Debug mode: {settings.debug}")
    print(f"🌐 CORS origins: {settings.allowed_origins}")

    # Best-effort: initialize Langfuse + enable PydanticAI OpenTelemetry instrumentation.
    try:
        get_langfuse()
    except Exception:
        pass
    # Optional: PydanticAI auto-instrumentation can create additional low-level
    # model/chat spans (including terminal empty assistant chunks in streaming).
    # Keep it opt-in to avoid noisy traces unless explicitly requested.
    should_instrument_pydantic = (
        os.getenv("LANGFUSE_PYDANTIC_INSTRUMENTATION", "false").lower()
        in ("1", "true", "yes")
    )
    if should_instrument_pydantic:
        try:
            from pydantic_ai.agent import Agent  # type: ignore

            Agent.instrument_all()
        except Exception:
            # Never block startup due to observability.
            pass
    yield
    # Shutdown
    print(f"🛑 {settings.app_name} shutting down...")
    safe_flush()

# Create FastAPI app with settings
app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    description="AI-powered Voice Assistant API for Agricultural Support",
    lifespan=lifespan
)

# Add CORS middleware with enhanced settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=settings.allowed_credentials,
    allow_methods=settings.allowed_methods,
    allow_headers=settings.allowed_headers,
)


@app.get("/")
async def root():
    """Root endpoint with app information"""
    return {
        "app": settings.app_name,
        "environment": settings.environment,
        "debug": settings.debug,
        "api_prefix": settings.api_prefix
    }

# Include all routers with API prefix from settings

app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(openai.router, prefix=settings.api_prefix) 