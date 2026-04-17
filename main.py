from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from contextlib import asynccontextmanager, suppress

load_dotenv()

from app.routers import health, openai
from app.observability.langfuse_client import (
    configure_pydantic_ai_langfuse_tracing,
    get_langfuse,
    safe_flush,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events for startup and shutdown"""
    print(f"🚀 {settings.app_name} starting up...")
    print(f"📍 Environment: {settings.environment}")
    print(f"🔐 Auth enabled: {settings.auth_enabled}")
    print(f"🔧 Debug mode: {settings.debug}")
    print(f"🌐 CORS origins: {settings.allowed_origins}")

    # Initialize Langfuse early so tracing is ready; align PydanticAI OTEL with the same provider.
    get_langfuse()
    with suppress(Exception):
        configure_pydantic_ai_langfuse_tracing()
    yield
    print(f"🛑 {settings.app_name} shutting down...")
    safe_flush()

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    description="AI-powered Voice Assistant API for Agricultural Support",
    lifespan=lifespan
)

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

app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(openai.router, prefix=settings.api_prefix) 