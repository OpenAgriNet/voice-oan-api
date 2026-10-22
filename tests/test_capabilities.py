"""Offline tests: no model, Redis, business API, or production access required."""
import ast
import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from helpers.prompt_skills import prompt_environment, section_index, SECTION_CALL
from jinja2 import Environment, FileSystemLoader
from pydantic_ai import Agent, ModelRetry, Tool
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from agents.capabilities import (
    skill_capabilities, PROMPT_DIR, capability_prompt_context, capability_routing,
    prepare_capability_tools, validate_manifest,
)
from agents.capability_manifest import CAPABILITIES
from agents.capability_policy import ALL_TOOLS, CapabilityState, ToolPolicy, configured_tool_policy
from agents.deps import FarmerContext
from app.core.languages import LANGUAGES, parse_iso_language_header

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = section_index(PROMPT_DIR)
PROMPTS = sorted(p.stem for p in PROMPT_DIR.glob('voice_*.md'))
CONTEXT = dict(today_date='21 October 2026')


def deps_for(allowed=ALL_TOOLS, preloaded=None, language='en'):
    return FarmerContext(query='test', session_id='offline', language_code=language, user_id='offline',
                         capabilities=CapabilityState(policy=ToolPolicy(
                             allowed_tools=allowed, preloaded_tools=preloaded)))


def test_manifest_covers_registered_tools_exactly():
    tree = ast.parse((ROOT / 'agents/tools/__init__.py').read_text())
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'TOOLS' for t in n.targets))
    names = [call.args[0].id for call in assignment.value.elts]
    validate_manifest([SimpleNamespace(name=name) for name in names])
    with pytest.raises(ValueError):
        validate_manifest([SimpleNamespace(name=name) for name in names[:-1]])


def test_every_supported_language_has_a_prompt_and_manifest():
    prompts = {p.stem for p in PROMPT_DIR.glob('voice_*.md')}
    assert prompts == set(SECTIONS) == {f'voice_{language}' for language in LANGUAGES}
    for language in LANGUAGES:
        assert parse_iso_language_header(language) == language


@pytest.mark.parametrize('prompt_file', PROMPTS)
def test_default_and_explicit_all_loaded_prompts_match(prompt_file):
    env = prompt_environment(PROMPT_DIR)
    deps = deps_for()
    actual = env.get_template(f'{prompt_file}.md').render(
        **capability_prompt_context(deps, prompt_file, CONTEXT)) + capability_routing(deps)
    assert actual == env.get_template(f'{prompt_file}.md').render(**CONTEXT)




def test_allowlist_and_configuration(monkeypatch):
    with pytest.raises(ValueError):
        ToolPolicy(allowed_tools={'typo'})
    with pytest.raises(ValueError):
        ToolPolicy(allowed_tools=set(), preloaded_tools={'search_terms'})
    monkeypatch.setenv('CAPABILITY_ALLOWED_TOOLS', 'search_terms,search_documents')
    monkeypatch.setenv('CAPABILITY_PRELOADED_TOOLS', 'search_terms')
    policy = configured_tool_policy()
    assert policy.allowed_tools == {'search_terms', 'search_documents'}
    assert policy.preloaded_tools == {'search_terms'}
    monkeypatch.setenv('CAPABILITY_PRELOADED_TOOLS', 'all')
    assert configured_tool_policy().preloaded_tools == policy.allowed_tools
    monkeypatch.setenv('CAPABILITY_ALLOWED_TOOLS', 'none')
    assert configured_tool_policy().allowed_tools == set()




def test_restore_across_topic_switches_and_policy_changes():
    deps = deps_for(preloaded=set())
    returns = []
    for group in ['pmkisan', 'weather', 'soil_health', 'pmkisan']:
        result = {'capability': group, 'loaded_tools': sorted(CAPABILITIES[group])}
        deps.capabilities.loaded_tools.update(CAPABILITIES[group])
        returns.append(ToolReturnPart(tool_name='load_capability', content=result))
    history = [ModelRequest(parts=returns)]
    fresh = deps_for(preloaded=set())
    fresh.capabilities.restore(history)
    assert fresh.capabilities.visible == deps.capabilities.visible
    narrowed = deps_for(allowed={'weather_forecast'}, preloaded=set())
    narrowed.capabilities.restore(history)
    assert narrowed.capabilities.visible == {'weather_forecast'}
    another_session = deps_for(preloaded=set())
    assert not another_session.capabilities.visible


def test_default_tools_preserve_objects_order_and_hide_loader():
    tools = [SimpleNamespace(name=name) for name in sorted(ALL_TOOLS)]
    ctx = SimpleNamespace(deps=deps_for())
    assert asyncio.run(prepare_capability_tools(ctx, tools+[SimpleNamespace(name='load_capability')])) == tools
    ctx.deps = deps_for(allowed=set())
    assert asyncio.run(prepare_capability_tools(ctx, tools+[SimpleNamespace(name='load_capability')])) == []


def test_real_agent_loader_then_execution_and_existing_prepare_guard():
    calls = []
    def search_terms(query: str) -> str:
        calls.append(query)
        return 'rice'
    async def deny(ctx, definition):
        return None
    def search_documents(query: str) -> str:
        raise AssertionError('Existing prepare guard was bypassed')
    step = 0
    def model(messages, info):
        nonlocal step
        names = {tool.name for tool in info.function_tools}
        assert 'search_documents' not in names
        if step == 0:
            assert names == {'load_capability'}
            response = ModelResponse(parts=[ToolCallPart('load_capability', {'id': 'crop_advice'})])
        elif step == 1:
            assert names == {'search_terms', 'load_capability'}
            response = ModelResponse(parts=[ToolCallPart('search_terms', {'query': 'rice'})])
        else:
            response = ModelResponse(parts=[TextPart('done')])
        step += 1
        return response
    agent = Agent(FunctionModel(model), deps_type=FarmerContext,
                  tools=[Tool(search_terms), Tool(search_documents, prepare=deny)],
                  capabilities=[skill_capabilities, PrepareTools(prepare_capability_tools)])
    deps = deps_for(allowed={'search_terms', 'search_documents'}, preloaded=set())
    assert agent.run_sync('rice', deps=deps).output == 'done'
    assert calls == ['rice']


def test_unavailable_tool_is_not_executed_even_if_model_requests_it():
    def search_documents(query: str) -> str:
        raise AssertionError('Denied tool executed')
    step = 0
    def model(messages, info):
        nonlocal step
        assert not info.function_tools
        step += 1
        if step == 1:
            return ModelResponse(parts=[ToolCallPart('search_documents', {'query': 'rice'})])
        return ModelResponse(parts=[TextPart('unavailable')])
    agent = Agent(FunctionModel(model), deps_type=FarmerContext,
                  tools=[Tool(search_documents)], capabilities=[skill_capabilities, PrepareTools(prepare_capability_tools)])
    assert agent.run_sync('rice', deps=deps_for(allowed=set())).output == 'unavailable'


@pytest.mark.parametrize('prompt_file', PROMPTS)
def test_unloaded_workflow_is_removed_but_defaults_restore_it(prompt_file):
    env = prompt_environment(PROMPT_DIR)
    deps = deps_for(preloaded=set())
    small = env.get_template(f'{prompt_file}.md').render(
        **capability_prompt_context(deps, prompt_file, CONTEXT))
    full = env.get_template(f'{prompt_file}.md').render(**CONTEXT)
    assert len(small) < len(full)
    for group in CAPABILITIES:
        deps.capabilities.loaded_tools.update(CAPABILITIES[group])
    restored = env.get_template(f'{prompt_file}.md').render(
        **capability_prompt_context(deps, prompt_file, CONTEXT))
    assert restored == full


@pytest.fixture
def production_voice_module(monkeypatch):
    """Import the real agent wiring, replacing only network-backed dependencies."""
    def stub(name, **attrs):
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    env = prompt_environment(PROMPT_DIR)
    stub('helpers.utils',
         get_prompt=lambda name, context: env.get_template(name+'.md').render(**context),
         get_today_date_str=lambda: CONTEXT['today_date'], get_logger=logging.getLogger)
    stub('agents.models', LLM_AGRINET_MODEL=FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart('done')])))
    def business_tool() -> str:
        return 'offline tool result'
    stub('agents.tools', TOOLS=[Tool(business_tool, name=name) for name in sorted(ALL_TOOLS)])
    spec = importlib.util.spec_from_file_location('offline_voice_agent', ROOT / 'agents/voice.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('language', sorted(LANGUAGES))
def test_actual_agent_prompt_hook_in_every_language(production_voice_module, language):
    module = production_voice_module
    deps = deps_for(language=language)
    env = prompt_environment(PROMPT_DIR)
    expected = env.get_template(f'voice_{language}.md').render(**CONTEXT)
    assert module.get_voice_system_prompt(SimpleNamespace(deps=deps, messages=[])) == expected
    assert module.voice_agent.run_sync('test', deps=deps).output == 'done'


def test_actual_agent_lazy_load_restores_otp_workflow_after_topic_switch(production_voice_module):
    agent = production_voice_module.voice_agent
    def first_model(messages, info):
        names = {tool.name for tool in info.function_tools}
        if 'initiate_pm_kisan_status_check' not in names:
            return ModelResponse(parts=[ToolCallPart('load_capability', {'id': 'pmkisan'})])
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == 'initiate_pm_kisan_status_check' for p in returns):
            assert any(p.tool_name == 'load_capability' and p.content['instructions'] for p in returns)
            return ModelResponse(parts=[ToolCallPart('initiate_pm_kisan_status_check', {})])
        return ModelResponse(parts=[TextPart('done')])
    with agent.override(model=FunctionModel(first_model)):
        first = agent.run_sync('Check PM-KISAN', deps=deps_for(preloaded=set()))
    def weather_model(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert CAPABILITIES['pmkisan'] <= names
        if 'weather_forecast' not in names:
            return ModelResponse(parts=[ToolCallPart('load_capability', {'id': 'weather'})])
        return ModelResponse(parts=[TextPart('done')])
    with agent.override(model=FunctionModel(weather_model)):
        second = agent.run_sync('Now weather', deps=deps_for(preloaded=set()), message_history=first.all_messages())
    def otp_model(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert CAPABILITIES['pmkisan'] | CAPABILITIES['weather'] <= names
        return ModelResponse(parts=[TextPart('done')])
    with agent.override(model=FunctionModel(otp_model)):
        assert agent.run_sync('Back to my OTP', deps=deps_for(preloaded=set()),
                              message_history=second.all_messages()).output == 'done'
