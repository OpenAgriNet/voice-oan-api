# Voice OAN API

An AI-powered agricultural assistant API that provides real-time, streaming responses to agricultural queries. Supports multiple languages (Gujarati, English) and offers a wide range of agricultural services including weather forecasts, market prices, scheme information, and more.

## Technology Stack

- **Framework**: FastAPI, Uvicorn, Gunicorn
- **AI/LLM**: Pydantic AI, OpenAI/Azure OpenAI/vLLM
- **Cache & Search**: Redis, Marqo (Vector Search)
- **Database**: PostgreSQL (SQLAlchemy)
- **Authentication**: JWT (PyJWT)
- **Monitoring**: Logfire
- **Infrastructure**: Docker, Supervisor
- **Language**: Python 3.10+

### Prerequisites

- Docker and Docker Compose
- Python 3.8+ (for local development)

### Docker Setup

#### 1. Create a Docker Network
```bash
docker network create svanetwork
```

#### 2. Run Redis Stack
```bash
docker run -d --name redis-stack --network svanetwork -p 6379:6379 -p 8001:8001 redis/redis-stack:latest
```

#### 3. Run Marqo (Vector Search)
```bash
docker run --name marqo -p 8882:8882 \
    -e MARQO_MAX_CONCURRENT_SEARCH=50 \
    -e VESPA_POOL_SIZE=50 \
    marqoai/marqo:latest
```

#### 4. Start the Application
```bash
docker compose up --build --force-recreate --detach
```

#### 5. View Logs
```bash
docker logs -f sva_app
```

### Stop the Application
```bash
docker compose down --remove-orphans
```

## Development

### Installation

1. Clone the repository
2. Install dependencies:
```bash
pip install -r requirements.txt
```

### Running Locally

Run the application locally using the setup instructions above.

## API Contract

Primary endpoint: `GET /api/voice`

Query parameters currently expected by the backend:
- `query`: latest user utterance text
- `session_id`: stable conversation identifier; callers must reuse it across reconnects/retries for the same call
- `source_lang`: current input language, currently `gu` or `en`
- `target_lang`: response language
- `user_id`: caller identifier; phone number is used to resolve farmer context
- `provider`: optional provider marker, currently `RAYA`
- `process_id`: provider-side request/call fragment identifier used for hold/nudge correlation

Response contract:
- `text/plain`
- streamed assistant text chunks
- output is optimized for phone/TTS delivery
- no feedback question is appended at the tail of the stream

Authentication contract:
- bearer JWT required outside development
- JWT subject and claims are treated as caller identity context

## Partner Heat-Alert Webhook

Inbound partner alerts are accepted at:

`POST /api/webhooks/alerts`

The alert kind is not in the URL. `notification_type` in the body is required.
Today the only value is `heat`. Input is case-insensitive and stored as lowercase.
A missing or unknown value returns `422`.

Auth:
- `Authorization: Bearer <token>` (a static shared secret, not a JWT)
- token value must match `WEBHOOK_SHARED_TOKEN`
- missing/invalid token → `401`
- token unset in env → `503` (fail closed)

Request body uses the partner's wire keys (including spaces/casing), for example:
`alertID`, `deviceid`, `tag no`, `Pashuaadhar No`, `AI Window start_time`.
`farmer_contact`, `Pashuaadhar No`, `tag no`, and `booking_url` must be non-empty
because they are sent to the single-farmer Sarlaben WhatsApp API. A missing value
returns `422` before the event is stored.

For each newly stored alert, the service calls
`POST <SARLABEN_WHATSAPP_BASE_URL>/api/whatsapp/heat-alert` with this mapping:
- `farmer_contact` → `mobile`
- `Pashuaadhar No` → `animalNo`
- `tag no` → `tagNo`
- `booking_url` → `bookingLink`

The database idempotency key (`alertID`, `farmer_contact`) also prevents a webhook
retry from sending the same WhatsApp message twice. The webhook returns `503` when
the WhatsApp API key is missing and `502` when the provider does not accept the
message; in either case the new database row is rolled back so a retry can try again.

Successful response (`202 Accepted`):
```json
{
  "accepted": true,
  "id": "<uuid>",
  "received_at": "<iso8601-utc>"
}
```

Persistence:
- rows are stored in a dedicated Postgres database (`WEBHOOK_DB_URL`)
- table: `heat_alert_webhook_events` (see `migrations/webhook/001_init.sql`)
- full payload is kept in `payload_raw` (JSONB) plus selected searchable columns
- DB missing/unconfigured → `503`

Retention:
- background worker hard-deletes rows older than `WEBHOOK_RETENTION_HOURS` (default `24`)
- cadence: `WEBHOOK_CLEANUP_INTERVAL_SECONDS` (default `900`)
- worker is a no-op when `WEBHOOK_DB_URL` is unset

Env reference: `example.webhook.env`

### vm2 deploy checklist

1. Create a dedicated Postgres DB/user on vm2 for webhook storage.
2. Apply the schema migration before deploying the matching application code:
   - fresh database: `psql "$WEBHOOK_DSN" -f migrations/webhook/001_init.sql`
   - existing database: `psql "$WEBHOOK_DSN" -f migrations/webhook/003_booking_url.sql`
3. Set runtime env on this service:
   - `WEBHOOK_SHARED_TOKEN=<shared-secret>`
   - `WEBHOOK_DB_URL=postgresql+asyncpg://USER:PASS@VM2_HOST:5432/DB`
   - `SARLABEN_WHATSAPP_API_KEY=<api-key-issued-by-GISPL>`
   - optional: `SARLABEN_WHATSAPP_BASE_URL` (defaults to `https://comm.amul.in/sarlaben`)
   - optional: `SARLABEN_WHATSAPP_TIMEOUT_SECONDS` (defaults to `15`)
   - optional: `WEBHOOK_DB_POOL_SIZE`, `WEBHOOK_RETENTION_HOURS`, `WEBHOOK_CLEANUP_INTERVAL_SECONDS`
4. Restart the API process and confirm startup log includes webhook cleanup worker start (or the "not started: WEBHOOK_DB_URL not configured" skip line).
5. Smoke test:
   ```bash
   SMOKE_ALERT_ID="smoke-booking-url-$(date +%s)"
   SMOKE_TIMESTAMP="$(date -u +'%Y-%m-%dT%H:%M:%S.000Z')"
   SMOKE_RESPONSE_FILE="$(mktemp)"
   : "${SMOKE_FARMER_MOBILE:?set a safe test WhatsApp recipient}"
   : "${SMOKE_ANIMAL_NO:?set a test animal number}"
   : "${SMOKE_TAG_NO:?set a test tag number}"
   : "${SMOKE_BOOKING_URL:?set a test booking URL}"

   SMOKE_HTTP_STATUS="$(curl -sS -o "$SMOKE_RESPONSE_FILE" -w '%{http_code}' \
     -X POST "$BASE_URL/api/webhooks/alerts" \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer $WEBHOOK_SHARED_TOKEN" \
     -d "{
       \"alertID\": \"$SMOKE_ALERT_ID\",
       \"deviceid\": \"SMOKE-BOOKING-URL\",
       \"alert_type\": \"Amber\",
       \"notification_type\": \"heat\",
       \"AI Window start_time\": \"$SMOKE_TIMESTAMP\",
       \"AI Window end_time\": \"$SMOKE_TIMESTAMP\",
       \"timestamp\": \"$SMOKE_TIMESTAMP\",
       \"farmer_contact\": \"$SMOKE_FARMER_MOBILE\",
       \"Pashuaadhar No\": \"$SMOKE_ANIMAL_NO\",
       \"tag no\": \"$SMOKE_TAG_NO\",
       \"booking_url\": \"$SMOKE_BOOKING_URL\"
     }")"

   cat "$SMOKE_RESPONSE_FILE"
   test "$SMOKE_HTTP_STATUS" = "202"
   ```
6. Verify both persisted copies of the URL using the same shell variables:
   ```bash
   SMOKE_MATCHES="$(psql "$WEBHOOK_DSN" -Atc "
     SELECT count(*)
     FROM heat_alert_webhook_events
     WHERE alert_id = '$SMOKE_ALERT_ID'
       AND booking_url = '$SMOKE_BOOKING_URL'
       AND payload_raw->>'booking_url' = '$SMOKE_BOOKING_URL';
   ")"

   test "$SMOKE_MATCHES" = "1"
   echo "booking_url persistence smoke test passed"
   rm -f "$SMOKE_RESPONSE_FILE"
   ```
7. Confirm the smoke row disappears after the configured retention period.

## FE / Telephony Contract

This service is built for an interruptible phone flow where the frontend/provider may reconnect repeatedly during one logical call.

Caller requirements:
- reuse the same `session_id` for all retries/reconnects of the same call
- send a fresh request when audio capture or network resumes
- preserve `process_id` semantics expected by the provider integration

Backend behavior:
- Redis stores message history per `session_id`
- Redis also stores active request ownership per `session_id`
- if a newer request for the same `session_id` arrives, older in-flight streams stop before writing stale history or sending extra nudges
- client disconnects are treated as termination for the current in-flight request, not for the whole conversation

Current voice behavior:
- the assistant should stay brief and conversational
- output should avoid markdown, bracketed repeats, and punctuation-heavy formatting
- Gujarati responses should remain respectful and gender-neutral for the caller
- Sarlaben self-reference should stay feminine in Gujarati
- generic wait nudges should be short and neutral, for example "I'm getting back to you, please wait"

## Translation Pipeline

When `ENABLE_TRANSLATION_PIPELINE=true`:
- Gujarati input is pretranslated to English before agent execution
- agent reasoning runs in English
- output is translated back to the requested target language in streamed batches
- OpenAI pretranslation is attempted first
- TranslateGemma is the fallback path if OpenAI returns empty output or times out

Relevant tuning env vars:
- `OPENAI_PRETRANSLATION_MODEL` default: `gpt-5-mini`
- `OPENAI_PRETRANSLATION_TIMEOUT_SECONDS` default: `4.0`
- `ENABLE_VOICE_NUDGES` default: `true`; controls timeout/tool-call hold nudges for voice requests. Set to `false` to temporarily disable nudges.
- `NUDGE_TIMEOUT_SECONDS` default: `3.0`
- `SESSION_OWNER_TTL_SECONDS` default: `120`
- `SESSION_OWNER_REFRESH_INTERVAL_SECONDS` default: `15`

## Voice Tracing

Voice request tracing is enabled by default with `ENABLE_VOICE_TRACING=true`.
When Langfuse is configured, each streamed voice request opens one root
`agent_journey` observation and nests moderation, translation, agent, and tool
spans below it. The service also emits one structured `VOICE_TRACE_SUMMARY`
log line per request when `VOICE_TRACE_LOG_SUMMARY=true`.

By default Langfuse gets the caller's hash as `user_id`, not their phone, and the
caller's words only as their length and hash. `VOICE_TRACE_USER_ID_MODE=phone`
sends the phone instead, and `VOICE_TRACE_TEXT_MODE` `preview_hash` or `full`
puts the words in Langfuse, so set them only where Langfuse is approved to hold
them and its retention and access policy is documented. The trace metadata
always carries `user_id_hash`. The summary log line never carries the words.

Tracing env vars:
- `ENABLE_VOICE_TRACING` default: `true`
- `VOICE_TRACE_LOG_SUMMARY` default: `true`
- `VOICE_TRACE_TEXT_MODE` default: `none` (length and hash only); supported values are `none`, `preview_hash` and `full`
- `VOICE_TRACE_USER_ID_MODE` default: `hash`; supported values are `hash` and `phone`
- `VOICE_TRACE_PREVIEW_CHARS` default: `120`

Latency definitions:
- `ttft_ms`: time from backend request start to the first yielded response chunk
- `ttfr_ms`: time from backend request start to the first non-nudge assistant text yielded to the client

Default traces do not store full caller text. They store character counts,
SHA-256 hashes, and short previews so production traces remain useful without
persisting complete utterances.

## Testing

Fast local regression suite:
```bash
pytest tests/test_voice_fixes.py tests/test_translation_vocabulary.py tests/test_voice_regressions_apr11_12.py -q
```

Integration regressions with real model calls and streamed voice behavior:
```bash
VOICE_PIPELINE_INTEGRATION=1 pytest tests/test_voice_regressions_apr11_12_integration.py -q
```

For the real model-backed checks in that file, set the relevant endpoints/keys first:
- `OPENAI_API_KEY`
- `TRANSLATEGEMMA_27B_BASE_ENDPOINT` — the SINGULAR nginx LB endpoint (it fans out to
  replicas server-side). Post-translation flows through the unified llm_core config
  chain (`Step.POST_TRANSLATION = [TranslateGemma(LB), managed-LLM overflow]`).
  `TRANSLATEGEMMA_27B_BASE_ENDPOINTS` (plural client-side list) is DEPRECATED and no
  longer read by code — any value is ignored.

Live vLLM pretranslation glossary regressions across every active glossary row:
```bash
PRETRANSLATION_GLOSSARY_INTEGRATION=1 \
PRETRANSLATION_PROVIDER=vllm \
INFERENCE_ENDPOINT_URL=http://YOUR_VLLM_HOST/v1 \
PRETRANSLATION_MODEL=YOUR_MODEL_NAME \
pytest tests/test_pretranslation_glossary_vllm_integration.py -q -rs
```

This suite intentionally makes one real pretranslation model call per glossary
entry and fails fast unless `PRETRANSLATION_PROVIDER=vllm` is set before test
collection.

## Maintenance

### Clean Up Docker Volumes
```bash
docker system prune -a --volumes
```

### Restart Services
```bash
docker compose down --remove-orphans
docker compose up --build --force-recreate --detach
```
