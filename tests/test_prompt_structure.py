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
from helpers.prompt_skills import prompt_environment, section_index, SECTION_CALL, skill_catalog
from jinja2 import DictLoader, Environment, FileSystemLoader

from agents.capabilities import capability_prompt_context
from agents.capability_manifest import CAPABILITIES
from agents.capability_policy import ALL_TOOLS, CapabilityState, ToolPolicy

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / 'assets/prompts'
SECTIONS = section_index(PROMPTS)
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
        includes = SECTION_CALL.findall((PROMPTS / f'{prompt}.md').read_text())
        assert len(includes) == len(set(includes)) == len(sections)
        assert set(includes) == {s['id'] for s in sections}
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
    for group, package in skill_catalog().items():
        for name, spec in package['sections'].items():
            expected = set(spec.get('languages', LANGUAGES))
            actual = {prompt.rsplit('_', 1)[1] for prompt, sections in SECTIONS.items()
                      if any(s['id'] == f'{group}/{name}' for s in sections)}
            assert actual == expected, (group, name, actual, expected)
    for root in PROMPTS.glob(f'{AGENT}_*.md'):
        for line in root.read_text().splitlines():
            if line.startswith('|'):
                assert not any(f'`{tool}`' in line for tool in ALL_TOOLS), 'Routing belongs inside skills'
    files = {p.relative_to(PROMPTS) for p in base.rglob('*.md') if p.name not in {'README.md', 'SKILL.md'}}
    assert files == referenced, 'Unreferenced prompt assets or missing files'


@pytest.fixture(scope='module')
def previous_revision(tmp_path_factory):
    revision = os.environ.get('PROMPT_COMPARE_REF')
    if not revision:
        pytest.skip('Set PROMPT_COMPARE_REF for a one-off Git revision comparison')
    archive = subprocess.check_output(['git', 'archive', revision, 'assets/prompts'], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        sources = {m.name.removeprefix('assets/prompts/'): tar.extractfile(m).read().decode()
                   for m in tar.getmembers() if m.isfile()}
    directory = tmp_path_factory.mktemp('previous-prompts')
    for name, text in sources.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    previous = (json.loads(sources['capabilities/sections.json'])
                if 'capabilities/sections.json' in sources else section_index(directory))
    return prompt_environment(directory), previous



@pytest.mark.parametrize('prompt', sorted(SECTIONS))
def test_rendering_and_lazy_instructions_match_git_revision(prompt, previous_revision):
    before, old_sections = previous_revision
    after = prompt_environment(PROMPTS)
    old_bindings = {s['path']: s['tools'] for s in old_sections[prompt]}
    new_bindings = {s['path']: s['tools'] for s in SECTIONS[prompt]}
    assert old_bindings.items() <= new_bindings.items()
    added = set(new_bindings) - set(old_bindings)
    assert production_prompt(prompt) == before.get_template(
        f'{prompt}.md').render(**CONTEXT)
    selections = [frozenset(), ALL_TOOLS, *CAPABILITIES.values(),
                  *({tool} for tool in sorted(ALL_TOOLS))]
    # New extractions intentionally remove previously always-visible routing in
    # selective mode. Full rendering must still be byte-identical; native tests
    # independently verify each newly gated section in every language.
    for visible in selections if not added else [ALL_TOOLS]:
        deps = SimpleNamespace(capabilities=CapabilityState(policy=ToolPolicy(preloaded_tools=visible)))
        context = capability_prompt_context(deps, prompt, CONTEXT)
        actual = after.get_template(f'{prompt}.md').render(**context)
        expected = before.get_template(f'{prompt}.md').render(
            **CONTEXT, capability_enabled=lambda names: bool(set(names) & set(visible)))
        assert actual == expected, (prompt, sorted(visible))
    for section in SECTIONS[prompt]:
        text = after.get_template(section['path']).render(**CONTEXT)
        if section['path'] in added:
            assert text in before.get_template(f'{prompt}.md').render(**CONTEXT)
        else:
            assert text == before.get_template(section['path']).render(**CONTEXT)
