"""Langfuse client and request-scoped tracing helpers for voice endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from langfuse import Langfuse, propagate_attributes

from app.config import settings

_langfuse: Optional[Langfuse] = None


def get_langfuse() -> Optional[Langfuse]:
    """Return a singleton Langfuse client, or None if keys are not configured."""
    global _langfuse
    public = settings.langfuse_public_key
    secret = settings.langfuse_secret_key
    if not public or not secret:
        return None
    if _langfuse is None:
        base = settings.langfuse_base_url or "https://cloud.langfuse.com"
        _langfuse = Langfuse(
            public_key=public,
            secret_key=secret,
            base_url=base,
            # Populate Langfuse "environment" field (not propagate_attributes).
            environment=settings.environment or "default",
        )
    return _langfuse


@contextmanager
def traced_voice_request(
    *,
    trace_name: str,
    session_id: str,
    user_id: Optional[str],
    tags: list[str],
    observation_name: str,
    trace_input: Any,
) -> Iterator[Optional[Any]]:
    """
    Root Langfuse observation for one voice agent run, with propagated session/user/tags.

    trace_input is sent as the observation input only (typically the user query string).

    Yields the observation object (with .update()) when Langfuse is configured, else None.
    """
    client = get_langfuse()
    if client is None:
        yield None
        return

    with propagate_attributes(
        trace_name=trace_name,
        session_id=session_id,
        user_id=user_id,
        tags=tags,
    ):
        with client.start_as_current_observation(
            name=observation_name,
            as_type="agent",
            input=trace_input,
        ) as obs:
            try:
                yield obs
            finally:
                client.flush()
