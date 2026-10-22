"""Exercise the production deadline wrapper with real Pydantic event streams."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.models.function import FunctionModel


def wrapper(agent, timeout):
    source = Path(__file__).resolve().parents[1] / 'app/services/voice.py'
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run_events_with_timeout')
    namespace = dict(asyncio=asyncio, voice_agent=agent, VOICE_USE_CASE='voice',
                     get_registry=lambda: SimpleNamespace(timeout=lambda _: timeout))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[node.name]


def test_actual_deadline_wrapper_emits_result_and_closes_stream():
    async def run():
        async def stream(messages, info):
            yield 'Hello '
            yield 'farmer'
        consume = wrapper(Agent(FunctionModel(stream_function=stream)), 10)
        events = [e async for e in consume(user_prompt='hello')]
        results = [e.result for e in events if isinstance(e, AgentRunResultEvent)]
        assert len(results) == 1 and results[0].output == 'Hello farmer'
        assert results[0].usage.output_tokens > 0
    asyncio.run(run())


def test_actual_deadline_wrapper_cancels_stalled_model():
    async def run():
        closed = asyncio.Event()
        async def stream(messages, info):
            try:
                yield 'Starting'
                await asyncio.Event().wait()
            finally:
                closed.set()
        consume = wrapper(Agent(FunctionModel(stream_function=stream)), 0.1)
        with pytest.raises(TimeoutError):
            async for _ in consume(user_prompt='hello'):
                pass
        await asyncio.wait_for(closed.wait(), 1)
    asyncio.run(run())
