"""Langfuse tracing for PydanticAI tool calls via event_stream_handler."""

from __future__ import annotations

from typing import Any, AsyncIterable, Optional

try:
    # pydantic-ai has had a few breaking renames/moves across releases.
    # Keep imports soft so the API can start even if the runtime version differs
    # from what's pinned in requirements.txt.
    from pydantic_ai.messages import (  # type: ignore
        AgentStreamEvent,
        FunctionToolResultEvent,
        PartStartEvent,
        RetryPromptPart,
        ToolCallPart,
    )

    try:
        from pydantic_ai.messages import BuiltinToolCallPart  # type: ignore
    except ImportError:  # pragma: no cover
        BuiltinToolCallPart = None  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    import pydantic_ai.messages as _messages  # type: ignore

    AgentStreamEvent = getattr(_messages, "AgentStreamEvent", Any)
    PartStartEvent = getattr(_messages, "PartStartEvent", Any)
    FunctionToolResultEvent = getattr(
        _messages, "FunctionToolResultEvent", getattr(_messages, "ToolResultEvent", Any)
    )
    RetryPromptPart = getattr(_messages, "RetryPromptPart", Any)
    ToolCallPart = getattr(_messages, "ToolCallPart", getattr(_messages, "BaseToolCallPart", Any))
    BuiltinToolCallPart = getattr(_messages, "BuiltinToolCallPart", None)
from pydantic_ai.tools import RunContext

from app.langfuse_client import get_langfuse


async def langfuse_event_stream_handler(
    run_context: RunContext[Any],
    stream: AsyncIterable[Any],
) -> None:
    """
    Create Langfuse child spans for tool calls during a run.

    This relies on an existing active Langfuse/OpenTelemetry context (e.g. the request-level
    observation created in `traced_voice_request`).
    """
    client = get_langfuse()
    if client is None:
        async for _ in stream:
            pass
        return

    # tool_call_id -> span context manager __enter__ result (LangfuseTool / LangfuseSpan wrapper)
    active_tools: dict[str, Any] = {}

    def _trace_context_from_deps() -> Optional[dict[str, str]]:
        deps = getattr(run_context, "deps", None)
        trace_id = getattr(deps, "langfuse_trace_id", None)
        parent_span_id = getattr(deps, "langfuse_root_observation_id", None)
        if trace_id and parent_span_id:
            return {"trace_id": trace_id, "parent_span_id": parent_span_id}
        return None

    def _best_trace_context() -> Optional[dict[str, str]]:
        # Prefer current context (works when OTel context propagation is intact),
        # otherwise fall back to deps-provided IDs (robust across async tasks).
        try:
            trace_id = client.get_current_trace_id()
            parent_span_id = client.get_current_observation_id()
            if trace_id and parent_span_id:
                return {"trace_id": trace_id, "parent_span_id": parent_span_id}
        except Exception:  # pragma: no cover
            pass
        return _trace_context_from_deps()

    async for event in stream:
        # Tool call start comes through PartStartEvent with ToolCallPart/BuiltinToolCallPart.
        tool_call_parts: tuple[type[Any], ...] = (ToolCallPart,) + (
            (BuiltinToolCallPart,) if BuiltinToolCallPart is not None else ()
        )

        part = getattr(event, "part", None)
        if (part is not None) and (
            (isinstance(event, PartStartEvent) and isinstance(part, tool_call_parts))
            or (
                # Fallback for versions where the event/part classes differ:
                # rely on the minimal tool-call interface we use below.
                hasattr(part, "tool_call_id") and hasattr(part, "tool_name")
            )
        ):
            tool_call_id = getattr(part, "tool_call_id", None)
            tool_name = getattr(part, "tool_name", None)
            if not tool_call_id or not tool_name:
                continue

            # Defensive: if we already have an active span for this id, don't double-open.
            if tool_call_id in active_tools:
                continue

            tool_input: Any = None
            if getattr(part, "args", None):
                args_as_dict = getattr(part, "args_as_dict", None)
                tool_input = args_as_dict() if callable(args_as_dict) else part.args

            cm = client.start_as_current_observation(
                name=tool_name,
                as_type="tool",
                input=tool_input,
                trace_context=_best_trace_context(),
            )
            # _AgnosticContextManager supports __enter__/__exit__.
            obs = cm.__enter__()
            active_tools[tool_call_id] = (cm, obs)
            continue

        # Tool result comes through FunctionToolResultEvent (tool return or retry prompt).
        if isinstance(event, FunctionToolResultEvent) or hasattr(event, "result"):
            result_part = getattr(event, "result", None)
            if result_part is None:
                continue
            tool_call_id = getattr(result_part, "tool_call_id", None)
            if not tool_call_id:
                continue

            entry: Optional[tuple[Any, Any]] = active_tools.pop(tool_call_id, None)
            if entry is None:
                continue

            cm, obs = entry
            try:
                if isinstance(result_part, RetryPromptPart):
                    # Tool errored / requested retry; store error text on span.
                    obs.update(level="ERROR", status_message=result_part.model_response())
                else:
                    model_response_object = getattr(result_part, "model_response_object", None)
                    if callable(model_response_object):
                        obs.update(output=model_response_object())
                    else:
                        obs.update(output=getattr(result_part, "content", None))
            finally:
                cm.__exit__(None, None, None)

    # If the stream ends while tool spans are still open, close them so they appear
    # as completed observations in Langfuse.
    for tool_call_id, (cm, obs) in list(active_tools.items()):
        try:
            obs.update(level="ERROR", status_message="Tool call ended without a result event.")
        finally:
            cm.__exit__(None, None, None)
            active_tools.pop(tool_call_id, None)

    try:
        client.flush()
    except Exception:  # pragma: no cover
        pass

