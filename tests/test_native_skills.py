"""Native Pydantic loading against the real packages, without external calls."""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from agents.capabilities import capability_prompt_context, prepare_capability_tools, skill_capabilities
from agents.capability_manifest import CAPABILITIES
from agents.capability_policy import ALL_TOOLS, CapabilityState, ToolPolicy
from helpers.prompt_skills import AGENT, PROMPT_DIR, prompt_environment, section_index

CONTEXT = dict(today_date='22 October 2026', crop_season='Rabi', last_weekday_table='Wednesday',
               vector_scheme_count=2, vector_schemes_bullets='- One\n- Two', vector_schemes_identifiers='one, two')


def deps(allowed=ALL_TOOLS, preloaded=frozenset()):
    return SimpleNamespace(capabilities=CapabilityState(policy=ToolPolicy(
        allowed_tools=allowed, preloaded_tools=preloaded)))


def agent_for(model, prompt_file=f'{AGENT}_en'):
    def business_tool() -> str:
        return 'offline result'
    agent = Agent(model, tools=[Tool(business_tool, name=n) for n in sorted(ALL_TOOLS)],
                  capabilities=[skill_capabilities, PrepareTools(prepare_capability_tools)])

    @agent.instructions
    def instructions(ctx):
        return prompt_environment().get_template(prompt_file+'.md').render(
            **capability_prompt_context(ctx.deps, prompt_file, CONTEXT))
    return agent


def script(*responses):
    iterator = iter(responses)
    return FunctionModel(lambda messages, info: next(iterator, ModelResponse(parts=[TextPart('done')])))


def call(name):
    return ModelResponse(parts=[ToolCallPart('load_capability', {'id': name})])


@pytest.mark.parametrize('prompt_file', sorted(section_index()))
def test_native_loader_delivers_exact_localized_sections(prompt_file):
    env = prompt_environment()
    for name, tools in CAPABILITIES.items():
        result = agent_for(script(call(name)), prompt_file).run_sync('test', deps=deps())
        returned = next(p for m in result.all_messages() for p in m.parts
                        if isinstance(p, ToolReturnPart) and p.tool_name == 'load_capability')
        expected = '\n'.join(env.get_template(s['path']).render(**CONTEXT)
                             for s in section_index()[prompt_file] if tools.intersection(s['tools']))
        assert returned.content.get('instructions', '') == expected.strip(), (prompt_file, name)


def test_all_preloaded_preserves_prompt_and_hides_loader():
    def model(messages, info):
        assert {t.name for t in info.function_tools} == ALL_TOOLS
        assert info.instructions == prompt_environment().get_template(f'{AGENT}_en.md').render(**CONTEXT)
        return ModelResponse(parts=[TextPart('done')])
    agent_for(FunctionModel(model)).run_sync('test', deps=deps(preloaded=None))


def test_serialized_topic_switch_history_restores_and_obeys_new_allowlist():
    agent = agent_for(script(call('pmkisan'), call('weather'), call('soil_health')))
    result = agent.run_sync('status, weather, soil, then OTP', deps=deps())
    history = ModelMessagesTypeAdapter.validate_json(result.all_messages_json())
    expected = CAPABILITIES['pmkisan'] | CAPABILITIES['weather'] | CAPABILITIES['soil_health']
    fresh = deps()
    fresh.capabilities.restore(history)
    assert fresh.capabilities.visible == expected
    # Applications restore before trimming. Losing the loader exchange must not
    # silently lose the active workflow during that same request.
    def check(messages, info):
        assert expected <= {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart('done')])
    agent_for(FunctionModel(check)).run_sync('OTP', deps=fresh, message_history=[])
    narrowed = deps(allowed={'weather_forecast'})
    def denied(messages, info):
        assert {t.name for t in info.function_tools} == {'weather_forecast'}
        return ModelResponse(parts=[TextPart('done')])
    agent_for(FunctionModel(denied)).run_sync('continue', deps=narrowed, message_history=history)
    assert not deps().capabilities.visible


def test_partial_preload_and_shared_tool_have_one_schema(monkeypatch):
    # A shared tool may belong to two workflows, but remains one implementation.
    monkeypatch.setitem(CAPABILITIES, 'weather', CAPABILITIES['weather'] | {'forward_geocode'})
    requested = {'forward_geocode', 'weather_forecast'}
    seen = []
    def model(messages, info):
        names = [t.name for t in info.function_tools]
        assert names.count('forward_geocode') == 1
        seen.append(set(names))
        return call('weather') if len(seen) == 1 else ModelResponse(parts=[TextPart('done')])
    agent_for(FunctionModel(model)).run_sync('weather', deps=deps(requested, {'forward_geocode'}))
    assert 'weather_forecast' not in seen[0]
    assert requested <= seen[1]


def test_native_streaming_load_then_execution_then_text():
    async def run():
        step = 0
        async def stream(messages, info):
            nonlocal step
            step += 1
            if step == 1:
                yield {0: DeltaToolCall(name='load_capability', json_args='{"id":"weather"}', tool_call_id='load')}
            elif step == 2:
                assert 'weather_forecast' in {t.name for t in info.function_tools}
                yield {0: DeltaToolCall(name='weather_forecast', json_args='{}', tool_call_id='weather')}
            else:
                yield 'Forecast '
                yield 'answer'
        agent = agent_for(FunctionModel(stream_function=stream))
        async with agent.run_stream('weather', deps=deps()) as result:
            chunks = [c async for c in result.stream_text(delta=True)]
        assert ''.join(chunks) == 'Forecast answer'
        assert step == 3
    asyncio.run(run())
