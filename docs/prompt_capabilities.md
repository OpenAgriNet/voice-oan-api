# Voice prompt capabilities

Based on `bh-voice-prod` commit `3de657fae5883909e392091fb76d57753a9547b1`.
All **24 registered tools** and **11 languages** are covered: English, Hindi,
Bengali, Gujarati, Kannada, Malayalam, Marathi, Odia (`od`), Punjabi, Tamil, Telugu.
This supersedes the capability port based on the unrelated voice `bh-dev` branch.

By default all tools are preloaded, no loader is exposed, and every prompt renders
exactly as before, including whitespace. Production model routing/fallback, plain-text
output, hold/delay messages, OTP behavior, and feedback-driven closing are unchanged.

## Configuration

Set these in the application's environment or `.env`:

```dotenv
CAPABILITY_ALLOWED_TOOLS=all
CAPABILITY_PRELOADED_TOOLS=all
```

Each accepts `all`, `none`, or comma-separated **exact tool names**. The allowed
list is the hard ceiling for tools that can ever load in this session. Preloaded
tools must be a subset. `all` for preload means all allowed tools. Invalid names
and invalid subsets raise errors instead of silently exposing extra tools.

Example: allow crop retrieval, PM-KISAN status, and feedback, but defer PM-KISAN:

```dotenv
CAPABILITY_ALLOWED_TOOLS=search_terms,search_documents,initiate_pm_kisan_status_check,check_pm_kisan_status_with_otp,submit_feedback
CAPABILITY_PRELOADED_TOOLS=search_terms,search_documents,submit_feedback
```

`allowed - preloaded` is the set loadable later. The model calls `load_capability`
with a group from `agents/capability_manifest.py`. It receives the original workflow
instructions and enables only that group's permitted tools on the next model step.
Loading does not execute a business action or send/verify an OTP.

Groups: schemes, PM-KISAN, PMFBY, soil health, crop advice, weather, location,
mandi, feedback. Location helpers are separately configurable; allow them when a
workflow needs geocoding. No implicit prerequisite can bypass the allowlist.
Keep `submit_feedback` permitted (and preferably preloaded) for normal call ending:
this existing production tool is what sets `end_interaction=True`. Excluding it
intentionally removes that closing action; the loader does not bypass the policy.

Loaded tools remain available throughout the session, including topic switches.
Successful loader results restore the state from full history before trimming.
A narrower current policy overrides previously loaded names. State is session-local.

Future per-request selection belongs at `FarmerContext.capabilities.policy`, using
`ToolPolicy(allowed_tools=..., preloaded_tools=...)`. No new public API parameters
are wired yet. This is tool availability, not a replacement for authorization.

## Prompt preservation and testing

Workflow sections were moved verbatim into `assets/prompts/capabilities/`, with
conditional includes in their original positions. Shared safety, language, routing,
and cross-workflow rules remain visible. A short availability rule in selective
mode overrides references to tools that are not permitted. Some shared references
intentionally remain; this is not a translation or workflow-rewriting change.

`sections.json` maps blocks to tools. Any loaded member includes the original block;
loader results also return the matching instructions. `original_sha256.json` anchors
reconstruction to the original production templates, so lost or changed wording is
caught for every language. Deliberate future prompt edits require updating that
preservation baseline explicitly. Tool schemas/descriptions are unchanged.

With the repo's pinned Pydantic AI 1.2.1 and compatible OpenTelemetry installed:

```bash
python -m pytest tests/test_capabilities.py tests/test_model_registry.py -q
```

Checks include full registry/language coverage, exact prompt reconstruction/rendering,
allowlist enforcement, per-session state, existing prepare guards, and deterministic
offline load→execute and topic-switch tests through the actual voice agent wiring.
No model server is needed. Model accuracy, selective-mode latency and a deployed
HTTP/telephony E2E require separate testing; these unit checks do not establish them.
