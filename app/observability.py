import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Track if any OTEL exporter is configured.
has_otel_exporter = False

# Conditionally configure Langfuse if env vars are set.
langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")
langfuse_client = None

if langfuse_public_key and langfuse_secret_key:
    try:
        from app.config import settings

        # Labels for Langfuse: identify all traces as from this service.
        release = (
            os.getenv("LANGFUSE_RELEASE")
            or settings.langfuse_release
            or "voice-oan-api"
        )
        environment = (
            os.getenv("LANGFUSE_TRACING_ENVIRONMENT")
            or settings.langfuse_environment
            or settings.environment
            or "voice-development"
        )
        # Langfuse SDK v4 reads LANGFUSE_BASE_URL. Keep LANGFUSE_HOST as a
        # backward-compatible input because older deployments may still set it.
        host = (
            os.getenv("LANGFUSE_BASE_URL")
            or os.getenv("LANGFUSE_HOST")
            or (settings.langfuse_base_url if settings.langfuse_base_url else None)
            or "https://cloud.langfuse.com"
        )

        os.environ.setdefault("LANGFUSE_BASE_URL", host)

        print(
            f"Langfuse initializing: host={host}, release={release}, environment={environment}",
            flush=True,
        )

        from langfuse import get_client

        langfuse_client = get_client()

        # Boot-time connectivity probe. This is advisory ONLY: a single Langfuse
        # blip during a deploy used to hard-gate `has_otel_exporter` for the whole
        # lifetime of the process, silently disabling pydantic-ai instrumentation
        # until the next restart. The OTEL exporter retries on its own background
        # thread and drops spans on queue overflow, so enabling it optimistically
        # can never add latency to or fail a farmer-facing request.
        try:
            if langfuse_client.auth_check():
                print("Langfuse initialized successfully - authentication verified", flush=True)
            else:
                print(
                    "Langfuse auth_check failed at boot - enabling tracing anyway "
                    "(the exporter retries; a transient blip must not disable tracing "
                    "for the life of the process)",
                    flush=True,
                )
        except Exception as auth_exc:  # pragma: no cover - never fail boot on telemetry
            print(
                f"Langfuse auth_check errored at boot ({auth_exc}) - enabling tracing anyway",
                flush=True,
            )
        has_otel_exporter = True
    except ImportError as e:
        print(f"Langfuse package not available - tracing disabled ({e})", flush=True)
    except Exception as e:  # pragma: no cover - telemetry must never break startup
        print(f"Langfuse initialization failed - tracing disabled ({e})", flush=True)
        langfuse_client = None
else:
    print(
        "Langfuse not configured - LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set",
        flush=True,
    )

# Enable Pydantic AI instrumentation if at least one exporter is configured.
if has_otel_exporter:
    from pydantic_ai.agent import Agent

    Agent.instrument_all()
    print("Pydantic AI instrumentation enabled", flush=True)


def get_langfuse_client():
    """Return the configured Langfuse client, or None when tracing is disabled."""
    return langfuse_client


async def flush_tracing(timeout: float = 5.0) -> bool:
    """Best-effort flush of buffered Langfuse/OTEL spans, e.g. on SIGTERM.

    Without this, spans still sitting in the batch queue are lost every time the
    container is stopped or rolled — which is exactly when you most want the
    trailing traces. Runs the (blocking) SDK flush on a worker thread and gives
    up after ``timeout`` seconds so shutdown can never hang. Returns True when
    the flush completed within the budget.

    Only ever called from the shutdown path, never from a request.
    """
    if langfuse_client is None:
        return False

    flush = getattr(langfuse_client, "flush", None)
    if not callable(flush):
        return False

    import asyncio

    def _flush() -> None:
        try:
            flush()
        except Exception as exc:  # pragma: no cover - telemetry must never raise
            logger.warning("Langfuse flush failed: %s", exc)

    try:
        await asyncio.wait_for(asyncio.to_thread(_flush), timeout=timeout)
        logger.info("Langfuse spans flushed on shutdown")
        return True
    except asyncio.TimeoutError:
        # The worker thread is left running; it is a daemon-side best effort and
        # must not delay process exit any further.
        logger.warning("Langfuse flush timed out after %.1fs on shutdown", timeout)
        return False
    except Exception as exc:  # pragma: no cover
        logger.warning("Langfuse flush errored on shutdown: %s", exc)
        return False


@contextmanager
def start_observation(
    name: str,
    *,
    input: Any | None = None,
    output: Any | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
    as_type: str = "span",
) -> Iterator[Any | None]:
    """Start a Langfuse observation when the client is configured.

    Generic helpers default to a span. Callers should pass as_type="generation"
    only for model calls so Langfuse renders model latency and token metadata
    correctly.
    """
    if langfuse_client is None:
        yield None
        return

    with langfuse_client.start_as_current_observation(
        name=name,
        as_type=as_type,
        input=input,
        output=output,
        model=model,
        metadata=metadata or {},
    ) as observation:
        yield observation
