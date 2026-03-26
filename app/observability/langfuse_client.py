import os
from contextlib import contextmanager, nullcontext
from typing import Any, Dict, Generator, Optional

from helpers.utils import get_logger

logger = get_logger(__name__)

_client = None


def langfuse_enabled() -> bool:
    """
    Langfuse is always enabled (no feature flag).

    Note: the Langfuse SDK still requires credentials; if keys are missing,
    initialization will fail gracefully and tracing becomes a no-op.
    """
    return True


def get_langfuse():
    global _client
    if _client is not None:
        return _client

    try:
        from langfuse import get_client  # type: ignore
    except Exception as e:  # pragma: no cover
        logger.warning("Langfuse SDK not available, skipping (err=%r)", e)
        _client = None
        return _client

    try:
        # get_client() uses LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL
        # and initializes the OpenTelemetry pipeline automatically.
        _client = get_client()
    except Exception as e:
        logger.exception("Failed to init Langfuse client: %r", e)
        _client = None
    return _client


@contextmanager
def safe_propagate_attributes(
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    version: Optional[str] = None,
) -> Generator[None, None, None]:
    """
    Best-effort propagation of common trace attributes to all child observations.
    """
    client = get_langfuse()
    if client is None:
        yield
        return

    try:
        from langfuse import propagate_attributes  # type: ignore

        kwargs: Dict[str, Any] = {}
        if user_id:
            kwargs["user_id"] = user_id
        if session_id:
            kwargs["session_id"] = session_id
        if tags:
            kwargs["tags"] = tags
        if metadata:
            kwargs["metadata"] = metadata
        if version:
            kwargs["version"] = version

        with propagate_attributes(**kwargs):
            yield
    except Exception:
        # Never fail the request due to observability.
        yield


def safe_start_observation(
    *,
    name: str,
    as_type: str = "span",
    input: Any = None,
    output: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
    model: Optional[str] = None,
):
    """
    Best-effort observation creation as a context manager.

    Returns a context manager that yields a Langfuse observation, or None if
    Langfuse is not available/configured.
    """
    client = get_langfuse()
    if client is None:
        return nullcontext(None)
    try:
        kwargs: Dict[str, Any] = {"as_type": as_type, "name": name}
        if input is not None:
            kwargs["input"] = input
        if output is not None:
            kwargs["output"] = output
        if metadata:
            kwargs["metadata"] = metadata
        if tags:
            kwargs["tags"] = tags
        if model:
            kwargs["model"] = model
        return client.start_as_current_observation(**kwargs)
    except Exception:
        return nullcontext(None)


def safe_flush() -> None:
    client = get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        # Never fail the request due to observability.
        return

