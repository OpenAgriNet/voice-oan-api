# Voice API Test Report — 4 July 2026

## Test setup

| | |
|---|---|
| Model | Qwen3 27B on vLLM (`agrinet-model`), temperature 0.3, thinking disabled |
| Prompt | `voice_system_mr.md` incl. new data-integrity rules (~12k tokens with tool schemas) |
| Host | Local Docker (`sva_app`, 8 uvicorn workers), vLLM remote, prefix caching enabled |
| Measurement | Client-side streaming TTFT via `scripts/measure_ttft.py convo` (localhost; excludes TTS and telephony legs) |
| Farmer A | `9999999999` — profiled: Nashik, cotton + soybean, preferred mandi Nashik |
| Farmer B | `8888888888` — new caller, no profile |
| LLM rounds | Derived from server `[TIMING] round1_tokens`: ~12k tokens per round; 24k ≈ 2 rounds (1 tool), 36–41k ≈ 3 rounds (2 tools) |

Includes all changes shipped 3–4 July: profile injection into system prompt, PoCRA forecast window fix, mandi commodity filtering, district GPS in profile, snapshot caching, per-worker warm-up, session TTL 2h, crop aging, integrity prompt rules, temperature 0.7 → 0.3.

## Summary

| Metric | Value |
|---|---|
| No-tool turn TTFT | 348–415 ms |
| One-tool turn TTFT | 2.5–4.5 s |
| Two-tool turn TTFT | 4.9–6.6 s |
| Median TTFT across all 11 turns | ~3.1 s |
| Worst turn (total) | 7.2 s (cotton price, 2 tool calls) |
| Context tracking | 11/11 turns correct |
| Factual integrity | Prices quoted verbatim with recorded date; no fabricated prices or predictions observed at temp 0.3 |

## Session 1 — Profiled farmer: prices, weather, scheme (`convo-5abc8a48`)

| # | Question | Response (abridged) | TTFT | Total | Tools | Verdict |
|---|---|---|---|---|---|---|
| 1 | नमस्कार | Intro, offers help with cotton/soybean/prices/weather/schemes (personalized to profile crops) | 1.59 s | 2.57 s | none | OK |
| 2 | कापसाचा भाव काय आहे? | No cotton price recorded at Nashik mandi today; offers vegetable prices | 6.58 s | 7.23 s | 2 (mandi ×2 or mandi+other; 36.7k tok) | OK, slow — see Issues #3 |
| 3 | मग कांद्याचा भाव किती? | कांदा ६००–२,३५० रु./क्विंटल — verbatim from API, offers other prices | 2.80 s | 3.71 s | 1 (mandi, commodity=onion) | OK — re-called tool, real price |
| 4 | पुढच्या आठवड्यात पाऊस पडेल का? | Rain likely next week per IMD data; drainage advice for cotton/soybean | 3.07 s | 4.19 s | 1 (weather) | OK — real forecast (was "no data" before fix) |
| 5 | मग फवारणी कधी करू? | Spray after rain stops; rain washes off pesticide | 0.36 s | 1.48 s | none (reasoned from turn 4) | OK |
| 6 | पीएम किसान योजनेबद्दल माहिती सांगा | Deflected to agriculture officer | 0.42 s | 1.16 s | **none** | **FAIL — scheme tools not called** (Issue #1) |

## Session 2 — Profiled farmer: memory, pest advice, warehouse (`convo-29e35b8a`)

| # | Question | Response (abridged) | TTFT | Total | Tools | Verdict |
|---|---|---|---|---|---|---|
| 1 | मागच्या कॉलमध्ये आपण काय बोललो होतो? | "No record of previous call" | 3.92 s | 4.49 s | 1 (recall — returned empty) | **FAIL — 7 call summaries exist** (Issue #2) |
| 2 | कापसावर बोंडअळी आली तर काय फवारू? | Specific spray mix (spinosad / profenofos / indoxacarb / chlorantraniliprole with doses), cites university source | 4.86 s | 6.72 s | 2 (terms+docs; 40.9k tok) | OK — sourced, detailed |
| 3 | जवळचं गोदाम कुठे आहे? | MSWC godown, Ambad, Nashik with pin + phone | 2.52 s | 3.95 s | 1 (warehouse) | OK (long digit read-out for TTS) |

## Session 3 — New farmer, no profile (`convo-262e83db`)

| # | Question | Response (abridged) | TTFT | Total | Tools | Verdict |
|---|---|---|---|---|---|---|
| 1 | कापसाचा भाव काय आहे? | Asks for district name | 1.81 s | 2.23 s | 1 | OK — graceful no-profile flow |
| 2 | बीड जिल्हा | No cotton price recorded at Beed mandi today; offers nearby mandi | 4.45 s | 5.28 s | 2 (geocode + mandi; 36.1k tok) | OK — geocode round still needed without profile |

## Working as intended

- Profile personalization: known farmers get their district applied without being asked; new farmers are asked once and get the fast path from call 2 (post-call extraction).
- Data integrity at temperature 0.3: onion price quoted verbatim (₹600–2,350/quintal) with its recorded date; no invented prices or "prices will rise" predictions in this round (both occurred at 0.7).
- Weather: real actionable forecasts after the PoCRA issue-date fix, including crop-specific drainage/spray advice; stale forecast days filtered out.
- Follow-up reasoning without re-calling tools (spray timing from the weather answer, 363 ms).
- Streaming confirmed token-level end to end; ~12 ms inter-chunk gaps.

## Issues found

1. **Scheme flow skipped** — PM-Kisan question answered with a deflection in one LLM round; `get_scheme_codes`/`get_scheme_info` never called despite the prompt marking them mandatory. Possibly over-caution from the new integrity rules.
   **FIXED same day**: integrity sections in all three prompts now state "fetch-then-say, never refuse-instead-of-fetch" with the explicit scheme tool chain. Re-test (`convo-e29b729b`): full PM-Kisan details (₹6,000/yr in 3 installments, 7/12 extract, Aadhaar, e-KYC) in 4.6 s TTFT; 42.5k request tokens confirm the two scheme tools ran.
2. **Memory recall returns empty** — `recall_farmer_context` ran (query `'previous call discussion'`) but mem0 semantic search returned nothing despite 7 stored call summaries (generic query vs. 0.3 similarity threshold).
   **FIXED same day**: tool now falls back to the two most recent call summaries when semantic search misses. Re-test: "मागच्या कॉलमध्ये आपण कापसावरील बोंडअळी फवारणी आणि जवळचे गोदाम याबद्दल बोललो होतो. आपण फवारणी केली का?" — correct summary of the previous session plus a proactive follow-up question, 2.4 s TTFT.
3. **Cotton-price turn used 3 LLM rounds** (36.7k tokens) where onion used 2 — the extra round costs ~2.5 s. Tool-call sequence for this case not yet traced; check the Langfuse trace for `convo-5abc8a48` turn 2.
4. **Digit-heavy TTS strings** — warehouse pin code and phone read out in full words; consider a formatting rule for contact details.

---

# Round 2 — full re-test after fixes (verbatim Q&A)

Same three-session suite, run after the scheme-flow and memory-recall fixes. Session 3 uses a brand-new farmer id (`7777777777`) since the previous new-farmer id had gained a profile via post-call extraction.

## Session 1 — Profiled farmer (`convo-95cfed7e`)

### Turn 1 — TTFT 4.63 s · total 5.82 s

- **Q:** नमस्कार
- **A:** नमस्कार. माझं नाव वसुधा आहे. मी एक डिजिटल सहाय्यक आहे, शेतकऱ्यांना शेतीविषयक माहिती देण्यासाठी तयार. कृपया सांगा, मी तुम्हाला कशात मदत करू?

### Turn 2 — TTFT 6.17 s · total 6.77 s

- **Q:** कापसाचा भाव काय आहे?
- **A:** आज अंबड मंडीत कापसाचा भाव नोंदलेला नाही. मी तुम्हाला जवळच्या दुसऱ्या मंडीचा भाव तपासू का?
- *Note: "अंबड मंडी" — see Round-2 issue 1, profile pollution; repaired after this run.*

### Turn 3 — TTFT 2.80 s · total 3.62 s

- **Q:** मग कांद्याचा भाव किती?
- **A:** कांद्याचा भाव सहाशे ते दोन हजार तीनशे पन्नास रुपये प्रति क्विंटल आहे. तुम्हाला कापसाचा भाव जवळच्या दुसऱ्या मंडीत तपासायचा आहे का?
- *Verbatim API price ₹600–2,350/quintal; also proactively returns to the farmer's open cotton question.*

### Turn 4 — TTFT 3.05 s · total 4.10 s

- **Q:** पुढच्या आठवड्यात पाऊस पडेल का?
- **A:** हवामान विभागाच्या अंदाजानुसार पुढच्या आठवड्यात तुमच्या भागात पाऊस पडण्याची शक्यता आहे. पावसाच्या दिवसात फवारणी थांबवा आणि पिकांना आधार द्या. अजून काही माहिती हवी आहे का?

### Turn 5 — TTFT 2.81 s · total 4.03 s

- **Q:** मग फवारणी कधी करू?
- **A:** हवामान विभागाच्या अंदाजानुसार पुढील काही दिवस पाऊस पडण्याची शक्यता आहे, त्यामुळे फवारणीसाठी पाऊस कमी होईपर्यंत थांबा. पाऊस कमी झाला आणि पाने कोरडी झाली की फवारणी करा. अजून काही माहिती हवी आहे का?

### Turn 6 — TTFT 3.10 s · total 4.21 s *(was FAIL in round 1 — now passes)*

- **Q:** पीएम किसान योजनेबद्दल माहिती सांगा
- **A:** प्रधानमंत्री किसान सन्मान निधी योजना अंतर्गत शेतकरी कुटुंबाला दरवर्षी सहा हजार रुपये मिळतात. हे पैसे तीन समान हप्त्यांत दिले जातात. अर्ज करण्यासाठी सात बारा उतारा, आधार आणि ई-केवायसी आवश्यक आहे. अजून काही माहिती हवी आहे का?

## Session 2 — Memory, pest, warehouse (`convo-e7024e20`)

### Turn 1 — TTFT 2.99 s · total 4.20 s *(was FAIL in round 1 — now passes, with caveat)*

- **Q:** मागच्या कॉलमध्ये आपण काय बोललो होतो?
- **A:** मागच्या कॉलमध्ये आपण नमो शेतकरी महासन्मान निधी योजनेबद्दल माहिती घेतली होती आणि कापसावर बोंडअळी फवारणीबद्दल चर्चा केली होती. त्यावेळी मी फवारणीसाठी योग्य औषधे आणि मात्रा सांगितली होती. अजून काही माहिती हवी आहे का?
- *Recall works via the new fallback. Caveat: the scheme discussed was PM-Kisan, the summary names नमो शेतकरी — extraction mislabel, see issue 3.*

### Turn 2 — TTFT 80.1 s (!) · total 82.0 s *(latency outlier — see issue 2)*

- **Q:** कापसावर बोंडअळी आली तर काय फवारू?
- **A:** कापसावर बोंडअळी आल्यावर नुकसान मर्यादा ओलांडल्यावर दहा लिटर पाण्यात स्पिनोसॅड पंधराशे मिली किंवा प्रोफेनोफॉस पन्नास मिली किंवा इंडॉक्साकार्ब दहा मिली किंवा क्लोरॅन्ट्रॉनिल प्रॉल तीन मिली मिसळून फवारणी करावी. ही शिफारस कृषी विद्यापीठांच्या तज्ञांनी दिली आहे. अजून काही माहिती हवी आहे का?
- *Dose figures inconsistent with round 1's answer to the same question — see issue 4.*

### Turn 3 — TTFT 2.57 s · total 4.25 s

- **Q:** जवळचं गोदाम कुठे आहे?
- **A:** आपल्या भागातील जवळचं गोदाम अंबड गोदाम आहे, जे आपल्या ठिकाणापासून नऊ किलोमीटर अंतरावर आहे. हे गोदाम एमएसडब्ल्यूसी, मिडसी एरिया, अंबड, नाशिक चार लाख तेरीस हजार चार पिन कोड येथे आहे आणि संपर्क क्रमांक शून्य दोन पंचतीस तीन दोन तीन आठ पाच दोन नऊ सात आहे. अजून काही माहिती हवी आहे का?

## Session 3 — New farmer, no profile (`convo-af8a22af`, user `7777777777`)

### Turn 1 — TTFT 2.48 s · total 3.12 s

- **Q:** कापसाचा भाव काय आहे?
- **A:** कापसाचा भाव सांगण्यासाठी मला तुमच्या जिल्ह्याचे नाव सांगा. तुम्ही कोणत्या जिल्ह्यात आहात?

### Turn 2 — TTFT 4.68 s · total 5.81 s

- **Q:** बीड जिल्हा
- **A:** बीड जिल्ह्यातील आजच्या बाजारभावात कापसाचा भाव उपलब्ध नाही. या बाजारात आज बाजरी, उडीद, हरभरा, तूर, ज्वारी, गहू, मका, सोयाबीन अशी पिके उपलब्ध आहेत. अजून काही माहिती हवी आहे का?

## Round-2 results

- 10 of 11 turns within expected latency; both round-1 failures (scheme flow, memory recall) now pass.
- Median TTFT ~3.0 s (excluding the single 80 s outlier); no-profile flow clean; onion price verbatim again.

## Round-2 issues

1. **Profile pollution (found & fixed)** — post-call extraction turned the *warehouse* location (MSWC godown, Ambad) into `preferred_mandi: अंबड`, so session 1 quoted the wrong mandi. Fixed the extraction prompt (preferred_mandi only when the farmer explicitly says where they sell; never from warehouse/staff/price lookups) and repaired the stored profile back to नाशिक (verified via API).
2. **80 s outlier on the pest question** — `search_documents` depends on the remote Marqo server (`10.10.85.88:8882`), which stalled once (version-check warning logged in the same window). One-off upstream; consider a client-side timeout + spoken fallback for the search tool.
3. **Recall mislabeled the scheme** — summary said नमो शेतकरी महासन्मान निधी where the call discussed PM-Kisan (related but distinct schemes). Post-call summarization accuracy; low frequency, worth watching.
4. **Pesticide dose figures unstable across runs** — the same bollworm question produced "स्पिनोसॅड सव्वापन्नास दशलक्ष मिली" (round 1) vs "स्पिनोसॅड पंधराशे मिली" (round 2); both look like formulation labels (e.g. "Spinosad 45 SC") being garbled into dose quantities. **Safety-relevant** — dosage text must be traced to the source document in Marqo and the TTS number-expansion rules checked before farmers act on it.

## Next steps

- Trace the bollworm dosage text through the `search_documents` result (Langfuse traces `convo-e7024e20` / `convo-29e35b8a`) and correct source data or prompt rules for dosages (issue 4 — highest priority, safety).
- Client-side timeout + graceful fallback for Marqo searches (issue 2).
- Mandi/weather upstream response caching (per market+commodity+day, 30–60 min TTL) — remaining ~2 s on tool turns.
- Tools accepting district names directly to remove the geocode round for no-profile farmers (Session 3 turn 2).
- Production numbers will add TTS + telephony latency on top of these figures; re-measure end-to-end once wired to the provider.
