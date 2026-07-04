from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from contextlib import asynccontextmanager
import logging

load_dotenv()

logging.getLogger("marqo").setLevel(logging.ERROR)

# Import all routers
from app.routers import voice, voice_bhili, health, voice_webhook

async def _warm_lazy_clients():
    """Initialize heavy lazily-created clients (Qdrant profile store, mem0) at
    startup so a farmer's first call on each worker doesn't pay the init cost."""
    import asyncio
    import os
    import time
    from helpers.utils import get_logger

    log = get_logger("warmup")
    t0 = time.perf_counter()
    loop = asyncio.get_event_loop()
    try:
        from app.services.profile import profile_store
        await loop.run_in_executor(None, profile_store._get_client)
    except Exception:
        log.warning("profile store warm-up failed", exc_info=True)
    try:
        from app.services.memory import memory_service
        await loop.run_in_executor(None, memory_service._get_client)
    except Exception:
        log.warning("memory service warm-up failed", exc_info=True)
    log.info(
        "Lazy clients warmed up in %dms (pid=%s)",
        int((time.perf_counter() - t0) * 1000),
        os.getpid(),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events for startup and shutdown"""
    # Startup
    print(f"🚀 {settings.app_name} starting up...")
    print(f"📍 Environment: {settings.environment}")
    print(f"🔧 Debug mode: {settings.debug}")
    print(f"🌐 CORS origins: {settings.allowed_origins}")
    import asyncio
    asyncio.create_task(_warm_lazy_clients())  # fire-and-forget; don't block startup
    yield
    # Shutdown
    print(f"🛑 {settings.app_name} shutting down...")

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

app.include_router(voice.router, prefix=settings.api_prefix)
app.include_router(voice_bhili.router, prefix=settings.api_prefix)
app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(voice_webhook.router, prefix=settings.api_prefix) 