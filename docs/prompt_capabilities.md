# Prompt skills

All 24 business tools and 11 supported languages are packaged here. All
permitted tools remain preloaded by default. Tool implementations, schemas,
consent guards and API contracts are unchanged.

## Authoring

Browse `assets/prompts/capabilities/voice/<skill>/SKILL.md`. Its YAML frontmatter
is the single source for tool bindings and localized sections. Edit workflow text
inside its section/language files. `_shared` owns cross-workflow instructions.

A root prompt is a layout: `{{ skill_section('pmkisan/status') }}` renders the
section selected by language and policy in its original position. It does not
repeat the tool list. `helpers/prompt_skills.py` derives the inventory from the
packages and layouts; there is no hand-maintained `sections.json`. `README.md`
files are navigation, not another source of tool bindings.

To add a tool, register its implementation once in `agents/tools`, reference its
name in the appropriate skill's `tools`, and add its usage guidance in every
supported language. The same tool may be referenced by multiple skills. It has
one schema and one executor, not duplicated implementations. A section normally
uses its package's tool bindings; `when_any` references other skill bindings for
shared sections. Explicit `tools` is only for a narrower section-level condition.
`languages` declares inherited section differences; missing translations never
silently fall back to English. Existing common KHC/AgriStack English files remain
explicitly common, not claimed as translations.

## Loading policy

```dotenv
CAPABILITY_ALLOWED_TOOLS=all
CAPABILITY_PRELOADED_TOOLS=all
```

Both accept `all`, `none`, or comma-separated exact tool names. Preloads must be a
subset of allowed tools. `all` for preload means all permitted tools. Unknown names
fail validation. The allowlist is a ceiling, including tools loaded later.

`FarmerContext.capabilities.policy` is the future request-level integration point;
no new public API parameter is added. With selective preloading, Pydantic's native
`load_capability({"id": "pmkisan"})` supplies workflow instructions and exposes
only permitted members on the next step. Loading is not a business action.

Preloaded instructions retain their original prompt placement. Deferred guidance
comes from the same files. Within a run the base prompt's tool selection is stable;
new guidance is returned by the loader. Shared sections are delivered once in that
run. Loaded workflows remain available after topic switches; there is no automatic
eviction. Successful native and legacy loader history restores state before history
trimming. A narrower current policy always wins. Shared business tools are registered
once and filtered with `PrepareTools`, preserving their own prepare/consent hooks.

## Verification

```bash
python -m pytest tests/test_capabilities.py tests/test_native_skills.py tests/test_prompt_structure.py -q
PROMPT_COMPARE_REF=<pre-structural-commit> python -m pytest tests/test_prompt_structure.py -q
```

CI checks all languages, package/section inventory and tool coverage. Native-loading
tests cover preload defaults, allowlists, shared tools, history serialization,
topic switches, tool guards and streaming. Git comparisons check full and selective
rendering, including whitespace; Pydantic itself strips the outside whitespace of
loaded instruction messages. No permanent prompt hashes are stored.

These offline checks do not establish model quality or latency. Paired inference
and judging must use `oan-evaluation` against an isolated model endpoint before
claiming no regression.
