# Demo mock mode — plan

Goal: let a demo caller exercise the full Sarlaben surface (farmer identity, herd,
milk collection, AI booking, vet booking, schemes, loan) without touching real
Amul systems.

Target: Fifth Elephant demo table, Friday 31 July 2026.
Demo caller: **`user_id = 8035454078`** (Plivo callerId `918035454078`, RAYA strips
the country code).

## ⚠️ This ships to PRODUCTION

The browser demo dials a Plivo DID → `Raya Pbx` inbound trunk → RAYA → **prod**
voice backend (`env=voice-production`, confirmed via session
`87fb3046-8153-43b3-9b18-1007133b36b4`). RAYA cannot repoint the UAT DIDs to dev.

Therefore a branch merged only to `amul-dev` **will not affect the demo**. To reach
Friday this must be released to prod. That is the central risk and the reason
everything below is gated to a single user id and defaults OFF.

## Why this is a safety fix, not a nicety

`create_ai_call` and `create_health_call` are real `POST`s
(`agents/tools/farmer_animal_backends.py:339,373` → `CreateAICall`,
`CreateHealthCall`). Unmocked, every attendee who asks to book a technician
**dispatches a real AI technician or vet** against a farmer who does not exist.
See [[project_voice_ai_call_booking_placeholder_bug]] — booking already 500s
39–81% of the time in prod because the agent invents placeholder codes when farmer
context is missing, which is exactly the demo caller's situation.

## Scope: only three tools need code

| Tool | Source | Approach |
|---|---|---|
| `get_farmer_profile` / `get_herd_summary` / `list_animal_tags` | farmer cache | **Redis seed, no code** |
| `get_union_scheme_data` | scheme cache namespace | **Redis seed, no code** |
| `check_loan_eligibility` | reads deps | **Redis seed, no code** |
| `search_documents` / `search_terms` | marqo / static | **nothing — already correct** |
| `get_farmer_milk_collection_details` | live API | mock |
| `create_ai_call` | **live POST (writes)** | mock |
| `create_health_call` | **live POST (writes)** | mock |

Search stays real on purpose: it is read-only, it works, and the RAG answer is the
most interesting thing to show.

## Design

**New:** `agents/tools/demo_fixtures.py` — fixture payloads plus one guard.

```python
DEMO_MOCK_ENABLED  = env "DEMO_MOCK_ENABLED"  default "false"
DEMO_MOCK_USER_IDS = env "DEMO_MOCK_USER_IDS" default ""   # comma-separated

def is_demo_caller(ctx) -> bool:
    """True only when the flag is on AND the caller is explicitly listed."""
```

Matches house flag style in `app/config.py` (`X_ENABLED` → `{"1","true","yes","on"}`).

**Edit:** three tools gain a four-line early return at the top:

```python
if is_demo_caller(ctx):
    return demo_fixtures.milk_collection_summary()      # or ai_call / health_call
```

Nothing else changes. With the flag off, or for any other caller, the diff is inert
— the guard returns `False` before any fixture is touched.

**Fixtures should be realistic, not obviously fake** — plausible litres/fat/SNF
across ~30 days with a believable payment total; booking confirmations that echo
back a technician name and slot. They read aloud as a real answer.

### Redis seed (separate from the code change)

```
sva-cache-farmer:d5ea7d79a02d1ae17648deea45f9a3b6873bec5140692b4d55a52359326620eb
                 ^ sha256("8035454078")
```

A `FarmerDataEnvelope` with `lookupStatus:"found"`, a synthetic farmer, 3–4 animals
carrying `lastBreedingActivity` / `lastCalvingDate` / `milkingStage`, and a populated
`aiTechnicians` list. 7-day TTL, self-expiring. **Prod Redis.**

Use a **synthetic** name/society — a real farmer's records should not be read aloud
to conference attendees.

## Branch and release

1. Branch `feat/demo-mock-mode` off **`amul-dev`** (voice repo convention).
2. Implement + unit tests, merge to `amul-dev`, verify on dev VM5 by POSTing
   `/api/voice` directly with `user_id=8035454078` — no telephony needed.
3. Cherry-pick to the prod branch, release via
   `~/amul-infra/scripts/run-voice-oan-api-build.sh` (Argo kaniko → `kubectl set
   image`, tag `amul-prod-<sha>`).
4. Set `DEMO_MOCK_ENABLED=true` and `DEMO_MOCK_USER_IDS=8035454078` in the prod
   secret, restart pods.
5. Seed prod Redis.
6. Place one call per language and confirm each tool returns its fixture.

⚠️ The voice repo has **no CI** and 34 failing tests on `amul-dev`
([[project_voice_technician_context_bugs]]) — the new tests must be run locally and
the existing failures not used as an excuse to skip them.

## Tests

- `is_demo_caller` false when flag off, false for a non-listed caller, true only for
  both — this is the one that matters.
- Each mocked tool returns its fixture for the demo caller and **still calls the
  real path** for anyone else (assert the HTTP client is invoked).
- Fixture formatting survives `_format_milk_collection_summary`.

## Rollback

`DEMO_MOCK_ENABLED=false` + pod restart. No redeploy, no revert. The Redis entry
expires on its own after 7 days, or `DEL` the key.

## Risks

| Risk | Mitigation |
|---|---|
| Prod release the day before a public demo | tiny diff, default off, single-user gate, env-var rollback |
| Flag left on after the demo | teardown checklist; add a calendar reminder for 1 Aug |
| Fixtures leak to real callers | guard requires exact `user_id` match; covered by test |
| Demo traffic pollutes prod analytics | unavoidable — tell whoever reads voice metrics that `8035454078` is demo traffic |

## Alternative if a prod release is not acceptable

Seed Redis only (no code). Farmer identity, herd, breeding history, schemes and loan
all work. Milk returns empty. **Booking must then be avoided entirely**, because
unmocked it writes to real systems — which means steering attendees away from it,
which is not controllable at a demo table.

Given that, the code change is the *safer* option, not the riskier one.
