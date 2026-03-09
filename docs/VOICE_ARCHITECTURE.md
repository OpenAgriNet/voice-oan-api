# Voice API – Architecture & Flow

## Components (simple)

```mermaid
%%{init: {'theme':'base', 'themeVariables': { 'primaryColor':'#e3f2fd', 'primaryBorderColor':'#1565c0', 'lineColor':'#37474f', 'secondaryColor':'#f3e5f5', 'tertiaryColor':'#e8f5e9' }}}%%
flowchart LR
    subgraph Entry["Entry"]
        VP["Voice Provider<br>(Samvaad / client)"]
        Auth["Auth<br>(JWT when enabled)"]
    end

    subgraph API["API Layer"]
        Router["Router"]
        OpenAISvc["OpenAI Service<br>SSE + history"]
        VoiceSvc["Voice Service<br>stream + parse"]
    end

    subgraph Core["Core"]
        Agent["Voice Agent<br>(pydantic-ai)"]
        Tools["Tools<br>schemes, search, mandi, etc."]
    end

    subgraph External["External"]
        LLM["LLM<br>(Azure / vLLM)"]
        Redis[("Redis<br>session history")]
    end

    VP --> Auth
    Auth --> Router
    Router --> OpenAISvc
    OpenAISvc --> VoiceSvc
    VoiceSvc --> Agent
    Agent --> LLM
    Agent --> Tools
    OpenAISvc --> Redis
    VoiceSvc --> Redis

    style VP fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    style Auth fill:#fff8e1,stroke:#f9a825,color:#e65100
    style Router fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    style OpenAISvc fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    style VoiceSvc fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    style Agent fill:#e0f7fa,stroke:#0097a7,color:#006064
    style Tools fill:#e0f7fa,stroke:#0097a7,color:#006064
    style LLM fill:#fff3e0,stroke:#ef6c00,color:#e65100
    style Redis fill:#ffebee,stroke:#c62828,color:#b71c1c
```

| Component | What it does |
|-----------|----------------|
| **Voice Provider** | Client (e.g. Samvaad) that sends requests and plays TTS from streamed audio. |
| **Auth** | Optional JWT validation (Bearer token). When `AUTH_ENABLED` is true, request must include valid JWT. |
| **Router** | Receives POST (body: messages; headers: X-Tenant-ID, X-User-ID, X-Session-ID, X-Language), validates, forwards to OpenAI Service. |
| **OpenAI Service** | Loads/saves session history (Redis), calls Voice Service, wraps reply in OpenAI SSE format. |
| **Voice Service** | Builds context, trims history, runs agent stream; parses streaming JSON and yields `audio` chunks. |
| **Voice Agent** | LLM agent with system prompt (en/hi/other) and structured output (audio, end_interaction, language). |
| **Tools** | Scheme info, PM-Kisan, PMFBY, grievance, search, weather, mandi, commodity, feedback. |
| **LLM** | Azure OpenAI or vLLM (configurable). |
| **Redis** | Stores conversation history per session. |

---

## High-level architecture

```mermaid
%%{init: {'theme':'base', 'themeVariables': { 'primaryColor':'#e3f2fd', 'primaryBorderColor':'#1565c0', 'lineColor':'#37474f' }}}%%
flowchart TB
    VP["Voice Provider"] --> Auth["Auth<br>(JWT)"]
    Auth --> Router["Router"]
    Router --> OpenAISvc["OpenAI Service"]
    OpenAISvc --> VoiceSvc["Voice Service"]
    VoiceSvc --> Agent["Voice Agent"]
    Agent --> LLM["LLM"]
    Agent --> Tools["Tools"]
    Agent --> Out["VoiceOutput<br>(audio, end_interaction, language)"]
    OpenAISvc --> Redis[("Redis")]
    VoiceSvc --> Redis

    style VP fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    style Auth fill:#fff8e1,stroke:#f9a825,color:#e65100
    style Router fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    style OpenAISvc fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    style VoiceSvc fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    style Agent fill:#e0f7fa,stroke:#0097a7,color:#006064
    style LLM fill:#fff3e0,stroke:#ef6c00,color:#e65100
    style Tools fill:#e0f7fa,stroke:#0097a7,color:#006064
    style Out fill:#e8eaf6,stroke:#3f51b5,color:#1a237e
    style Redis fill:#ffebee,stroke:#c62828,color:#b71c1c
```

## Request flow (streaming)

```mermaid
%%{init: {'theme':'base', 'themeVariables': { 'actorBkg':'#e3f2fd', 'actorBorder':'#1565c0', 'actorTextColor':'#0d47a1', 'signalColor':'#37474f', 'signalTextColor':'#263238', 'labelBoxBkgColor':'#f5f5f5', 'labelBoxBorderColor':'#616161', 'activationBkgColor':'#e0f7fa', 'activationBorderColor':'#0097a7' }}}%%
sequenceDiagram
    participant VP as Voice Provider
    participant Auth
    participant R as Router
    participant S as OpenAI Service
    participant V as Voice Service
    participant Redis
    participant A as Voice Agent
    participant LLM
    participant T as Tools

    VP->>Auth: Request (Bearer JWT when AUTH_ENABLED)
    Auth->>Auth: Validate JWT (or skip if auth disabled)
    Auth->>R: Request (messages + headers)

    R->>R: Validate headers & messages
    R->>S: generate_openai_stream(...)

    S->>Redis: get session history
    Redis-->>S: history or welcome messages

    S->>V: stream_voice_message(...)
    V->>V: Build context, trim history
    V->>A: run_stream_events(...)

    loop Stream
        A->>LLM: Request
        LLM-->>A: Stream text (JSON)
        A-->>V: part_delta
        V->>V: Parse audio from JSON
        V-->>S: JSON chunk
        S-->>VP: SSE chunk
        opt Tool needed
            A->>T: Call tool
            T-->>A: Result
            A->>LLM: Continue
        end
    end

    A-->>V: agent_run_result
    V-->>S: Final chunk
    S-->>VP: [DONE]
    V->>Redis: update_message_history
```

## File reference

| Component | File |
|-----------|------|
| App entry | `main.py` |
| Auth | `app/auth/jwt_auth.py`, `app/config.py` (AUTH_ENABLED) |
| Router | `app/routers/openai.py` |
| OpenAI Service | `app/services/openai_service.py` |
| Voice Service | `app/services/voice.py` |
| Session / history | `app/utils.py`, `app/core/cache.py` |
| Voice Agent | `agents/voice.py` |
| LLM config | `agents/models.py` |
| Tools | `agents/tools/__init__.py` + tool modules |

## Data flow (streaming)

- **Entry flow:** Voice Provider sends request → Auth validates JWT (when enabled) → Router receives request (body: messages; headers: X-Tenant-ID, X-User-ID, X-Session-ID, X-Language).
- **Each SSE chunk:** `delta.content` = full JSON: `{"audio": "...", "end_interaction": false, "language": "en"|"hi"|null}`. Voice provider uses latest chunk for TTS.
- **Session:** Stored in Redis as `{session_id}_SVA`; updated after each run.
