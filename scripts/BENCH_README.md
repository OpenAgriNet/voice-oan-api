# Voice model bench harness

Voice half of the harness used for the 2026-08-01 guided-decoding experiment
(amul-oan-api#181, #203). Chat-side runners and the pairwise judge live in
`amul-oan-api/scripts/`.

| file | what it does |
|---|---|
| `run_bench_voice.py` | Runs questions through `voice_agent` directly — no FastAPI, Redis or JWT. Optional vLLM guided decoding. |
| `run_bench_voice_parity.py` | Above + `--inject-rules` (the 33 `GU_PREFERRED_TRANSLATION_RULES` into the agent prompt, verified present in the sent messages) and `--apply-map` (deterministic forbidden-term replacement). |
| `guided_patch.py` | Port of `bharat-oan-api#142`. Byte-identical to the chat copy. |
| `rubric_score.py` / `report.py` | Mechanical scoring and side-by-side tables. |
| `gender_check.py` | Coverage of `GU_FEMININE_SELF_REFERENCE_REPLACEMENTS` against real output. |
| `posthoc_map.py` | Applies the forbidden map to existing results. |

Run with **cwd = repo root** — assets load via relative paths. Requires
`BARE_GU_NATIVE=1` and the gu-native voice prompt to exercise the no-sandwich path.

See `amul-oan-api/scripts/BENCH_README.md` for the full list of traps (silently-ignored
`guided_regex` on vLLM 0.15.1, judge position bias, per-row script scoring,
silent degradation under an over-tight constraint).
