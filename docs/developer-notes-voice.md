# Developer Notes — Voice Assistant (Vasudha / MahaVistaar Voice API)

Last updated: 4 July 2026. Companion documents: [voice-testing-report-2026-07-04.md](voice-testing-report-2026-07-04.md).

---

## 1. System overview

```
Farmer (phone) ── telephony provider (RAYA/RINGG: STT + TTS)
                        │  GET /api/voice/?query=...&session_id=...&source_lang=...&target_lang=...
                        ▼
FastAPI (8 uvicorn workers, Docker `sva_app`, port 8003)
  ├─ app/routers/voice.py        streaming endpoint, JWT→hashed user_id, per-session lock
  ├─ app/services/voice.py       stream_voice_message: profile load → prompt injection → agent stream
  ├─ agents/voice.py             pydantic-ai Agent (temperature 0.3, thinking off)
  │    └─ agents/tools/*         mandi, weather, search (Marqo), schemes, warehouse,
  │                              geocode, staff contact, memory recall, profile update
  ├─ app/utils.py                Redis session history (2 h TTL), trim/clean helpers
  ├─ app/services/profile.py     structured FarmerProfile (Qdrant, permanent)
  ├─ app/services/memory.py      mem0 call summaries (Qdrant, permanent)
  └─ app/langfuse_client.py      tracing
        │
        ├─ vLLM: Qwen3 27B (`agrinet-model`), OpenAI-compatible, prefix caching ON
        ├─ Azure OpenAI gpt-4.1: error fallback only (FallbackModel)
        ├─ Redis (`redis-stack`): session history, snapshot cache, nudge state
        ├─ Qdrant: `vistaar_farmer_profiles` (point lookup), mem0 collection (semantic)
        └─ Upstream: BAP/Beckn (PoCRA weather + mandi), Marqo doc search, Mapbox geocode
```

A "turn" = one GET request. The response is a **token stream** (`text/plain`); the telephony provider TTS-es chunks as they arrive. Multi-turn state lives in Redis keyed by `session_id`; long-term farmer memory lives in Qdrant keyed by sha256(phone).

## 2. Turn lifecycle (what happens on every request)

1. Router resolves `user_id` (JWT `sub` phone → sha256; dev mode allows `?user_id=`), cancels the session's inactivity timer, loads history from Redis (seeds system prompt + welcome pair for new sessions).
2. `stream_voice_message`:
   - **Call start only** (history ≤ 3 messages): load profile snapshot — Redis cache first (5 min TTL), else Qdrant profile, else mem0 summary — and **append it to the session's cached system prompt** (see learning #1), persist.
   - Clean orphaned tool calls, trim history to `VOICE_HISTORY_MAX_TOKENS` (default 24k, system prompt included; tool calls from past turns stripped).
   - Run `voice_agent.run_stream()`; yield text deltas straight to the HTTP response.
   - On `UsageLimitExceeded`: retry once on Azure with 8k history.
3. `finally`: log `[TIMING]` lines, update Langfuse (actual system prompt incl. profile), persist history.
4. 20 s (env `CALL_INACTIVITY_TIMEOUT`) after the last turn, the inactivity timer fires post-call extraction: one structured mem0 call summary + structured profile merge (both on the same vLLM).

## 3. Memory & personalization design

Two layers, deliberately different:

| Layer | Store | Shape | Loaded | Expires |
|---|---|---|---|---|
| Structured profile | Qdrant point (id = uuid5(user_id)), no embeddings | `FarmerProfile`: location, GPS, crops (+sowing date→stage), irrigation, schemes, open_threads, notes | Every call start, injected into system prompt | Never (crops age out of the *rendering* seasonally) |
| Episodic memory | mem0 + Qdrant, embedded | One structured summary per call (Topics/Questions/Advice/Follow-up), stored verbatim (`infer=False`) | On demand via `recall_farmer_context` tool | Never |

Design decisions and why:

- **Snapshot goes in the system prompt, not a tool.** Personalization must be unconditional — the model can't be trusted to *decide* to fetch it. Tools are for depth (past advice details), the prompt is for identity (name, place, crops, open follow-ups).
- **Profile snapshot includes district GPS** (36-district static map) so tools get coordinates without a geocode round.
- **Crop aging** (`_crop_age_state`): in-season → show stage ("day 30, ~vegetative"); past harvest → "sown LAST season; ask, do not assume"; >2 seasons/1 year → hidden. Prevents "near harvest" forever and turns staleness into a re-engagement question.
- **Open threads** drive the proactive greeting ("मागच्या वेळी बोंडअळीबद्दल बोललो — फवारणी केली का?"). Post-call extraction populates them.
- **Recall fallback**: open-ended "what did we discuss?" queries often miss the 0.3 similarity threshold; the tool falls back to the two most recent call summaries so a farmer with history never hears "no record".
- **Extraction guardrails**: the extractor is told `preferred_mandi` only when the farmer explicitly says where they *sell* — it once promoted a warehouse address (MSWC Ambad) into preferred mandi (see learning #8).

## 4. Latency engineering

Measured envelope (localhost, warm caches; TTS/telephony not included):

| Turn type | TTFT | Where time goes |
|---|---|---|
| No tool | ~350 ms | prefix-cached prefill + first token |
| One tool | 2.5–4.5 s | tool-call gen + upstream API (~2 s) + re-prefill |
| Two tools | 4.9–6.6 s | two upstream round-trips |
| Fresh call, first turn | +0.5–1 s | snapshot load (cache miss) |

Token budget per LLM round ≈ 12k (system prompt ~10k incl. tool schemas + trimmed history). Every extra tool round is +12k tokens and +1.5–2.5 s. `[TIMING] round1_tokens req=` divided by 12k ≈ number of LLM rounds that turn.

What actually moved the needle (in impact order):

1. **vLLM prefix caching** — the ~10k-token system prompt is byte-identical across all sessions per language; cached prefill is the difference between 8 s and 0.35 s TTFT. Keep the prompt prefix stable; append per-session content (profile) at the END of the system prompt. `scripts/measure_ttft.py vllm --runs 2` verifies caching (run2 ≪ run1).
2. **Kill unnecessary LLM rounds** — GPS in the snapshot removed the geocode round (3→2 rounds, 38k→24k tokens). Cheaper than any infra change.
3. **Shrink tool outputs** — mandi returned the full 65-item catalog (~5.6k chars) for every question; filtered by `commodity` it's ~500 chars. Tool-result tokens are the only part of the prompt prefix caching can never help with.
4. **Move init off the request path** — per-worker lazy clients (Qdrant/mem0) now warm at startup (`lifespan`); profile snapshot cached in Redis 5 min.
5. **Anti-buffering headers** (`X-Accel-Buffering: no`) so nginx/ingress can't batch the stream.

Still open: upstream response caching (mandi/weather per market+commodity+day, 30–60 min TTL) — the last ~2 s of tool turns.

Fixed 5 Jul: Marqo search now has a hard timeout (`MARQO_SEARCH_TIMEOUT`, default 10 s) and returns a graceful "service unavailable, offer staff contact" instruction instead of `ModelRetry` — a down Marqo previously produced 3 minutes of dead air (2 × ~75 s of client-internal connection retries). Same day: prompt rules for pending questions (short answer after the bot asks a question continues the SAME task/tool — a bare "नाशिक" after a weather question was being routed to staff contact) and unclear STT input (ask to repeat, don't re-read the previous answer); removed prompt examples that themselves contained a fabricated price ("₹18–22/kg") and a price prediction — the model had been echoing them verbatim.

## 5. Learnings (the expensive ones)

1. **pydantic-ai skips system-prompt functions when `message_history` is non-empty.** Only `SystemPromptPart`s carrying a `dynamic_ref` get re-rendered; plain parts are used as-is and `@agent.system_prompt(dynamic=True)` never runs. Our seeded history has a plain system part → anything you want the model to see must be **written into the cached history** (`inject_profile_into_history`), not put in deps. This silently disabled personalization for weeks.
2. **PoCRA forecast API filters on the forecast *issue* date, not the forecast dates.** Requesting [today → +5d] returns nothing unless today's bulletin is already published. Request [today−5d → tomorrow], keep the newest bulletin, drop past days. Farmers got "no forecast available" for months while data existed.
3. **Temperature is a tool-reliability knob, not just a style knob.** At 0.7 the call-tool-vs-answer-from-memory decision was a measured coin flip (same onion question: one run hallucinated "₹18–22/kg, prices will rise", the next fetched the real ₹600–2,350). At 0.3 the flip-flop disappeared. Don't use 0.0 — Qwen3 repetition-loops under greedy decoding.
4. **Integrity rules must say "fetch-then-say", not just "don't guess".** After adding "only state facts from tool results", the model started *refusing* scheme questions instead of calling the scheme tools. Prohibitions need the affirmative companion: if you don't have it, go get it.
5. **Token counts are the cheapest diagnostic.** `req=` per turn ÷ 12k ≈ LLM rounds. 38k on a "one-tool" turn exposed the hidden geocode round; 12k on a "scheme" turn exposed the skipped tool chain. Read tokens before reading transcripts.
6. **Voice output needs supply-side limits.** Given a 40-name commodity list, the model read all 40 aloud (~30 s of TTS) with garbled ad-hoc translations. Cap lists in tool results (12 names) and say "do NOT read this list aloud" in the result itself. What's in context is what gets spoken.
7. **Everything the model sees gets recycled.** Tool results from earlier turns are reused to answer follow-ups (good: 350 ms answers; bad: it once answered "onion not available" from a stale names-only list instead of re-fetching). Tool-result text should state its own limits ("prices NOT shown — call again").
8. **Post-call extraction can pollute the profile.** It promoted a *warehouse* location to `preferred_mandi`, and the bot started quoting the wrong mandi on the next call. Extraction prompts need explicit negative examples; profile writes deserve a review pass in Langfuse (`profile.extract_and_merge` logs what it merges).
9. **Safety: RAG dosage numbers garble.** The same bollworm question produced "स्पिनोसॅड सव्वापन्नास दशलक्ष मिली" and "स्पिनोसॅड पंधराशे मिली" on different runs — formulation labels ("Spinosad 45 SC") mangled into quantities. Unresolved; trace the Marqo source text before farmers act on dosages. Numeric agronomy needs verbatim-quote rules or structured source data.
10. **Postman/proxies make real streaming look fake.** Postman shows the body only when the stream ends (TTFB 275 ms, "download" 8 s = the streaming window). Use `curl -N` or `scripts/measure_ttft.py`. In production, nginx `proxy_buffering` does the same thing to real clients — hence the `X-Accel-Buffering: no` header.
11. **8 uvicorn workers = 8 of everything per-process.** Lazy client init multiplied by workers caused random 8–16 s first turns until warm-up moved to `lifespan`. Still open: the per-session `asyncio.Lock` is per-worker — concurrent turns for one session on different workers can race history writes (needs a Redis lock).
12. **Local venv ≠ container.** venv has pydantic-ai 0.2.4, requirements/Docker pin 1.80.0. Code is volume-mounted (`.:/app`) — `docker restart sva_app` applies changes; host-side scripts that import `agents.tools` fail on the old venv (stub the package or run inside the container: `docker exec sva_app python ...`).

## 6. Reference

### TTLs and stores

| Data | Store | TTL | Invalidation |
|---|---|---|---|
| Session history | Redis `{session_id}__SVA` | 2 h (`SESSION_CACHE_TTL_SECONDS`) | overwrite per turn |
| Profile snapshot (rendered) | Redis `profile_snapshot_{user_id}` | 5 min | on profile save/delete |
| Farmer profile | Qdrant `vistaar_farmer_profiles` | permanent | tools / DELETE endpoint |
| Call summaries | Qdrant (mem0) | permanent | DELETE endpoint |
| Session lock / inactivity timer | process memory | — | per-worker only (known gap) |

### Key environment variables

`LLM_PROVIDER=vllm` · `LLM_AGRINET_MODEL_NAME` · `VLLM_OPENAI_BASE_URL` (or derived from `VLLM_AGRINET_MODEL_URL`) · `INFERENCE_API_KEY` · `AZURE_OPENAI_{ENDPOINT,API_KEY,DEPLOYMENT_NAME}` (fallback) · `VOICE_HISTORY_MAX_TOKENS` (24k) · `SESSION_CACHE_TTL_SECONDS` (7200) · `CALL_INACTIVITY_TIMEOUT` (20 s here; 60 s default) · `QDRANT_HOST/PORT` · `MEMORY_EMBED_MODEL` (OpenAI text-embedding-3-small) · `BAP_ENDPOINT` + `POCRA_BPP_*` (weather/mandi) · `JWT_PHONE_CLAIM` (default `sub`)

### Endpoints

- `GET /api/voice/` — the voice turn (streaming). Dev: `?user_id=<phone>`; prod: Bearer JWT with phone claim.
- `GET|DELETE /api/voice/profile?phone=` — inspect/delete structured profile (internal).
- `GET|DELETE /api/voice/memories?phone=` — inspect/delete mem0 summaries (internal).
- `POST /api/voice/call-ended` (webhook) — authoritative post-call trigger; inactivity timer is the dev fallback.

### Test & diagnostic commands

```bash
# multi-turn conversation, per-turn TTFT + full replies
venv/bin/python scripts/measure_ttft.py convo "कापसाचा भाव काय आहे?" "आणि कांद्याचा?"

# end-to-end latency distribution / buffering detector
venv/bin/python scripts/measure_ttft.py api --query "नमस्कार" --runs 3 [--same-session]

# raw model prefill + prefix-cache verdict (loads .env)
venv/bin/python scripts/measure_ttft.py vllm --runs 3

# server-side stage timings and round counts
docker logs sva_app --since 10m | grep TIMING
# llm_ttft = agent start → first delta; round1_tokens req≈12k per LLM round

# real streaming check without the script
curl -N "http://localhost:8003/api/voice/?query=नमस्कार&session_id=t1&source_lang=mr&target_lang=mr&user_id=9999999999"

# apply code changes (code is volume-mounted)
docker restart sva_app
```

### File map (voice path)

| File | Role |
|---|---|
| `app/routers/voice.py` | endpoint, auth, session lock, streaming headers |
| `app/services/voice.py` | turn orchestration, snapshot cache, profile injection, timing logs, Langfuse |
| `app/utils.py` | history cache/trim/clean, `inject_profile_into_history`, TTLs |
| `agents/voice.py` | Agent config (temperature, limits), `build_voice_system_prompt` |
| `agents/models.py` | vLLM + Azure fallback wiring |
| `agents/deps.py` | `FarmerContext` (per-turn deps) |
| `agents/tools/mandi.py` | commodity filter + alias map + voice-safe rendering |
| `agents/tools/weather.py` | issue-date window + newest-bulletin/future-days trim |
| `agents/tools/memory_tool.py` | recall + recent-summaries fallback |
| `app/services/profile.py` | FarmerProfile schema, GPS map, crop aging, extraction, snapshot cache key |
| `app/services/memory.py` | mem0 config, call summarization |
| `assets/prompts/voice_system_{mr,hi,en}.md` | system prompts incl. integrity rules |
| `scripts/measure_ttft.py` | latency/context test harness |
| `main.py` | app wiring + per-worker client warm-up |

### Next steps (carried from testing)

- Dosage integrity for pest advice (safety; trace Marqo source, add verbatim-quote rule).
- Marqo search timeout + spoken fallback.
- Upstream mandi/weather response cache (30–60 min).
- District-name parameters on tools (removes geocode round for no-profile farmers).
- Redis-based cross-worker session lock.
- Re-measure end-to-end once wired to telephony (adds STT/TTS legs on top of all numbers here).
