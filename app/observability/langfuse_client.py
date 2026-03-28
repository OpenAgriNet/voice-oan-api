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
    tags: Optional[list[str]] = None,
    model: Optional[str] = None,
):
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
                tags=tags,
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
