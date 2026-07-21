from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from contextlib import asynccontextmanager
from app.tasks.scheme_scheduler import start_scheme_scheduler, stop_scheme_scheduler
from app.tasks.farmer_refresh_worker import start_farmer_refresh_worker, stop_farmer_refresh_worker
# P2 health poller: active LB /health probe feeding the per-endpoint breaker.
# start_/stop_ are no-ops unless HEALTH_POLLER_ENABLED (flag-off boot is untouched).
from app.tasks.health_poller import start_health_poller, stop_health_poller

load_dotenv()

# Configure observability (Logfire and/or Langfuse) before other imports that use it
import app.observability  # noqa: F401, E402

# Import all routers
from app.routers import  voice, health

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events for startup and shutdown"""
    # Startup
    print(f"🚀 {settings.app_name} starting up...")
    print(f"📍 Environment: {settings.environment}")
    print(f"🔧 Debug mode: {settings.debug}")
    print(f"🌐 CORS origins: {settings.allowed_origins}")
    # Load prompt templates into memory (no disk I/O at request time)
    from helpers.utils import load_prompt_templates
    load_prompt_templates(settings.base_dir / "assets" / "prompts")
    # Unified LLM pipeline (the only model-selection path): synthesize/validate the
    # config and run the resolvability self-check (logs the resolved per-step
    # provider/model/endpoint; non-fatal). An unbuildable config (E) fails the boot
    # fast; a self-check/configure edge case never blocks startup.
    from app.llm_core import runtime as _llm_runtime
    try:
        _llm_runtime.configure()
    except (_llm_runtime.PipelineConfigError, _llm_runtime.BootRefused):
        # Fail-fast at boot: an unbuildable pipeline config (E, e.g. an anthropic
        # tier on a RAW_OPENAI step) OR an intentional REQUIRE_OVERFLOW_ARMED
        # hard-gate must stop startup, not crash per-request / ship dark.
        raise
    except Exception as _llm_exc:  # pragma: no cover - defensive
        print(f"⚠️  llm_core configure skipped: {_llm_exc}")
    await start_scheme_scheduler()
    await start_farmer_refresh_worker()
    await start_health_poller()
    yield
    # Shutdown
    await stop_health_poller()
    await stop_farmer_refresh_worker()
    await stop_scheme_scheduler()
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

@app.get("/metrics")
async def metrics():
    """Prometheus exposition for the unified LLM pipeline (plain text, no auth,
    scraped internally). render() is a no-op safe stub when prometheus_client is
    absent, so this route works whether or not the dependency is installed."""
    from app import metrics as _metrics
    body, content_type = _metrics.render()
    return Response(content=body, media_type=content_type)

# Include all routers with API prefix from settings

app.include_router(voice.router, prefix=settings.api_prefix)
app.include_router(health.router, prefix=settings.api_prefix)
