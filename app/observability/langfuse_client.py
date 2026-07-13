from contextlib import contextmanager, nullcontext
from typing import Any, Dict, Generator, Optional

from helpers.utils import get_logger

logger = get_logger(__name__)

_client = None


def get_langfuse():
    global _client
    if _client is not None:
        return _client

    try:
        from langfuse import get_client  # type: ignore
    except ImportError as e:  # pragma: no cover
        logger.warning("Langfuse SDK not available, skipping (err=%r)", e)
        return None

    try:
        # Uses LANGFUSE_* env vars configured for the SDK.
        _client = get_client()
    except Exception as e:
        logger.exception("Failed to init Langfuse client: %r", e)
        return None
    return _client


def _compact_kwargs(**kwargs: Any) -> Dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


@contextmanager
def safe_propagate_attributes(
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    version: Optional[str] = None,
) -> Generator[None, None, None]:
    if get_langfuse() is None:
        yield
        return
    try:
        from langfuse import propagate_attributes  # type: ignore

        with propagate_attributes(
            **_compact_kwargs(
                user_id=user_id,
                session_id=session_id,
                tags=tags,
                metadata=metadata,
                version=version,
            )
        ):
            yield
    except Exception:
        yield


def safe_start_observation(
    *,
    name: str,
    as_type: str = "span",
    input: Any = None,
    output: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
):
    # Note: Langfuse observations do not support tags — tags are trace-level only.
    # Use safe_propagate_attributes(tags=...) instead.
    client = get_langfuse()
    if client is None:
        return nullcontext(None)
    try:
        return client.start_as_current_observation(
            **_compact_kwargs(
                as_type=as_type,
                name=name,
                input=input,
                output=output,
                metadata=metadata,
                model=model,
            )
        )
    except Exception:
        return nullcontext(None)


def safe_flush() -> None:
    client = get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        pass


def configure_pydantic_ai_langfuse_tracing() -> None:
    """Use the process OpenTelemetry provider (Langfuse augments it at startup) for PydanticAI runs.

    Enables GenAI semconv v3 so tool spans carry ``gen_ai.tool.call.arguments`` / ``result`` and nest under
    agent runs. Call after :func:`get_langfuse` in application lifespan.
    """
    try:
        from opentelemetry import trace as otel_trace
        from pydantic_ai.agent import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings

        provider = otel_trace.get_tracer_provider()
        if provider is None:
            return
        Agent._instrument_default = InstrumentationSettings(
            tracer_provider=provider,
            include_content=True,
            version=3,
        )
    except Exception:
        logger.exception("Failed to configure PydanticAI instrumentation for Langfuse")


def safe_start_agent_observation(
    *,
    name: str,
    input: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """Langfuse ``agent`` observation; children include PydanticAI OTEL spans (invoke_agent, execute_tool, …).

    Tags are trace-level only in Langfuse; use safe_propagate_attributes(tags=...) for those.
    """
    client = get_langfuse()
    if client is None:
        return nullcontext(None)
    try:
        return client.start_as_current_observation(
            **_compact_kwargs(
                as_type="agent",
                name=name,
                input=input,
                metadata=metadata,
            )
        )
    except Exception:
        return nullcontext(None)
