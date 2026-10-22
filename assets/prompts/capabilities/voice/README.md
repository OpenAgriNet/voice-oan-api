# voice capability prompts

Browse **capability → section → language**. Filenames retain the section name for
search and editor tabs. Main `voice_<language>.md` templates retain global rules
and the original include order; directory order never controls rendering.

The tool groups in `agents/capability_manifest.py` and the tool/section mappings in
`../sections.json` still control loading. Shared sections have one source, not
copies per capability. Folder ownership does not change tool permissions.

## Capabilities

- [schemes](schemes/README.md) — 2 tools
- [pmkisan](pmkisan/README.md) — 5 tools
- [pmfby](pmfby/README.md) — 6 tools
- [soil_health](soil_health/README.md) — 1 tool
- [crop_advice](crop_advice/README.md) — 4 tools
- [weather](weather/README.md) — 1 tool
- [location](location/README.md) — 2 tools
- [mandi](mandi/README.md) — 2 tools
- [feedback](feedback/README.md) — 1 tool

## Language coverage

Supported prompt languages: `bn`, `en`, `gu`, `hi`, `kn`, `ml`, `mr`, `od`, `pa`, `ta`, `te`.

“Localized files” means separate language assets, not a translation-quality score.
“Common” means the same instruction source is used for every language.

| Section | Coverage |
|---|---|
| [feedback/submission](feedback/submission/) | Localized files, all 11 languages |
| [mandi/prices](mandi/prices/) | Localized files, all 11 languages |
| [pmfby/grievance](pmfby/grievance/) | Localized files, all 11 languages |
| [pmfby/grievance_routing](pmfby/grievance_routing/) | Localized files, all 11 languages |
| [pmfby/status](pmfby/status/) | Localized files, all 11 languages |
| [pmkisan/grievance](pmkisan/grievance/) | Localized files, all 11 languages |
| [pmkisan/status](pmkisan/status/) | Localized files, all 11 languages |
| [schemes/information](schemes/information/) | Localized files, all 11 languages |
| [schemes/pmkisan_release_date](schemes/pmkisan_release_date/) | Localized files, all 11 languages |
| [soil_health/crop_suitability](soil_health/crop_suitability/) | Localized files, all 11 languages |
| [soil_health/results](soil_health/results/) | Localized files, all 11 languages |
| [weather/forecast](weather/forecast/) | Localized files, all 11 languages |

## Editing and checks

Edit a section in its language file; keep shared content in one place. Do not
change the tool allowlist, grouping, or include order as a side effect of file moves.
Markdown trailing blank lines are significant: Jinja strips one final newline from
each template. The fragments retain the original rendered separators without
whitespace-marker comments. Crop advice and location retain their existing
main-prompt instructions and tool descriptions; no new prompt text is invented.

Run the permanent structure and capability tests:

```bash
python -m pytest tests/test_prompt_structure.py tests/test_capabilities.py -q
```

For a structural migration, choose the Git revision immediately before the change:

```bash
PROMPT_COMPARE_REF=<revision> python -m pytest tests/test_prompt_structure.py -q
```

This compares full prompts, no preloads, each capability, each individual tool,
and lazy-loaded instructions in every language, including whitespace. No frozen
hash files are needed. Without an explicit revision, only the Git-comparison cases
are skipped; permanent inventory and behavioral checks still run.
