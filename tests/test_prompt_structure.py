"""Prompt assets and optional byte-exact comparison against a chosen Git revision.

PROMPT_COMPARE_REF=<revision> python -m pytest tests/test_prompt_structure.py -q
No saved hashes, model calls, or external services are required.
"""
import ast
import asyncio
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import DictLoader, Environment, FileSystemLoader

from agents.capabilities import capability_prompt_context, load_capability
from agents.capability_manifest import CAPABILITIES
from agents.capability_policy import ALL_TOOLS, CapabilityState, ToolPolicy

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / 'assets/prompts'
SECTIONS = json.loads((PROMPTS / 'capabilities/sections.json').read_text())
AGENT = next(iter(SECTIONS)).rsplit('_', 1)[0]
LANGUAGES = {key.rsplit('_', 1)[1] for key in SECTIONS}
CONTEXT = dict(today_date='22 October 2026', crop_season='Rabi', last_weekday_table='Wednesday',
               vector_scheme_count=2, vector_schemes_bullets='- One\n- Two',
               vector_schemes_identifiers='one, two')


def production_prompt(prompt):
    # Exercise the real renderer without importing unrelated AWS/network dependencies.
    tree = ast.parse((ROOT / 'helpers/utils.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_prompt')
    namespace = dict(Environment=Environment, FileSystemLoader=FileSystemLoader, Dict=dict)
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'helpers/utils.py', 'exec'), namespace)
    return namespace['get_prompt'](prompt, CONTEXT, str(PROMPTS))


def test_prompt_inventory_and_language_coverage():
    assert set(SECTIONS) == {p.stem for p in PROMPTS.glob(f'{AGENT}_*.md')}
    base = PROMPTS / 'capabilities' / AGENT
    groups = {Path(s['path']).parts[2] for sections in SECTIONS.values() for s in sections}
    assert {p.name for p in base.iterdir() if p.is_dir()} == set(CAPABILITIES) | groups
    coverage = {}
    referenced = set()
    for prompt, sections in SECTIONS.items():
        lang = prompt.rsplit('_', 1)[1]
        includes = re.findall(r'{% include "([^"]+)" %}', (PROMPTS / f'{prompt}.md').read_text())
        assert len(includes) == len(set(includes)) == len(sections)
        assert set(includes) == {s['path'] for s in sections}
        for section in sections:
            path = Path(section['path'])
            assert path.parts[:2] == ('capabilities', AGENT)
            assert len(path.parts) == 5
            assert path.parts[2] in CAPABILITIES or path.parts[2] == '_shared'
            assert path.name.endswith((f'_{lang}.md', '_common.md'))
            assert section['tools'] and set(section['tools']) <= ALL_TOOLS
            text = (PROMPTS / path).read_text()
            assert text.strip() and 'preserve fragment whitespace' not in text
            coverage.setdefault(path.parent, set()).add(lang)
            referenced.add(path)
    for section, actual in coverage.items():
        # Inherited source difference, not a translation fallback.
        expected = {'en', 'hi', 'mai', 'or', 'pa'} if section.as_posix() == (
            'capabilities/agrinet/pmkisan/instalments') else LANGUAGES
        assert actual == expected, (section, actual, expected)
    files = {p.relative_to(PROMPTS) for p in base.rglob('*.md') if p.name != 'README.md'}
    assert files == referenced, 'Unreferenced prompt assets or missing files'


@pytest.fixture(scope='module')
def previous_revision():
    revision = os.environ.get('PROMPT_COMPARE_REF')
    if not revision:
        pytest.skip('Set PROMPT_COMPARE_REF for a one-off Git revision comparison')
    archive = subprocess.check_output(['git', 'archive', revision, 'assets/prompts'], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        sources = {m.name.removeprefix('assets/prompts/'): tar.extractfile(m).read().decode()
                   for m in tar.getmembers() if m.isfile()}
    return Environment(loader=DictLoader(sources), autoescape=False), json.loads(
        sources['capabilities/sections.json'])


@pytest.mark.parametrize('prompt', sorted(SECTIONS))
def test_rendering_and_lazy_instructions_match_git_revision(prompt, previous_revision):
    before, old_sections = previous_revision
    after = Environment(loader=FileSystemLoader(PROMPTS), autoescape=False)
    assert [s['tools'] for s in old_sections[prompt]] == [s['tools'] for s in SECTIONS[prompt]]
    assert production_prompt(prompt) == before.get_template(
        f'{prompt}.md').render(**CONTEXT)
    selections = [frozenset(), ALL_TOOLS, *CAPABILITIES.values(),
                  *({tool} for tool in sorted(ALL_TOOLS))]
    for visible in selections:
        deps = SimpleNamespace(capabilities=CapabilityState(policy=ToolPolicy(preloaded_tools=visible)))
        context = capability_prompt_context(deps, prompt, CONTEXT)
        actual = after.get_template(f'{prompt}.md').render(**context)
        expected = before.get_template(f'{prompt}.md').render(
            **CONTEXT, capability_enabled=lambda names: bool(set(names) & set(visible)))
        assert actual == expected, (prompt, sorted(visible))
    deps = SimpleNamespace(capabilities=CapabilityState(policy=ToolPolicy(preloaded_tools=set())))
    capability_prompt_context(deps, prompt, CONTEXT)
    for group, tools in CAPABILITIES.items():
        result = asyncio.run(load_capability(SimpleNamespace(deps=deps), group))
        expected = '\n'.join(before.get_template(s['path']).render(**CONTEXT)
                             for s in old_sections[prompt] if tools.intersection(s['tools']))
        assert result['instructions'] == expected, (prompt, group)
