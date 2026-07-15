import os
from pathlib import Path
from typing import List, Optional
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

class Settings(BaseSettings):
    # Core Application Settings
    app_name: str = "Amul Voice AI API"
    environment: str = os.getenv("ENVIRONMENT", "production")
    debug: bool = False
    base_dir: Path = Path(__file__).resolve().parent.parent
    secret_key: str = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
    timezone: str = "Asia/Kolkata"

    # Server Configuration
    host: str = "0.0.0.0"
    port: int = 8003
    api_prefix: str = "/api"
    rate_limit_requests_per_minute: int = 1000

    # Security Settings
    allowed_origins: List[str] = os.getenv("ALLOWED_ORIGINS", "*").split(",")
    allowed_credentials: bool = True
    allowed_methods: List[str] = ["*"]
    allowed_headers: List[str] = ["*"]

    # JWT Configuration
    # Keys can be provided as raw PEM values (JWT_PUBLIC_KEY / JWT_PRIVATE_KEY) or as file paths (JWT_PUBLIC_KEY_PATH / JWT_PRIVATE_KEY_PATH). Values take precedence over paths.
    jwt_algorithm: str = "RS256"
    jwt_public_key: Optional[str] = os.getenv("JWT_PUBLIC_KEY")
    jwt_public_key_path: str = os.getenv("JWT_PUBLIC_KEY_PATH", "jwt_public_key.pem")
    jwt_private_key: Optional[str] = os.getenv("JWT_PRIVATE_KEY")
    jwt_private_key_path: Optional[str] = os.getenv("JWT_PRIVATE_KEY_PATH")

    # Worker Settings
    uvicorn_workers: int = os.cpu_count() or 1

    # Redis Settings
    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))
    redis_db: int = 0
    redis_password: Optional[str] = os.getenv("REDIS_PASSWORD")
    redis_key_prefix: str = "sva-cache-"
    redis_socket_connect_timeout: int = 10
    redis_socket_timeout: int = 10
    redis_max_connections: int = 100
    redis_retry_on_timeout: bool = True

    # Cache Configuration
    default_cache_ttl: int = 60 * 60 * 24  # 24 hours
    session_owner_ttl_seconds: int = int(os.getenv("SESSION_OWNER_TTL_SECONDS", "120"))
    session_owner_refresh_interval_seconds: int = int(os.getenv("SESSION_OWNER_REFRESH_INTERVAL_SECONDS", "15"))
    # Farmer cache policy: beyond this age a cached record is too stale to serve —
    # the read blocks on a bounded API call instead of serving it (falls back to
    # the stale record only if the API also fails). Backstop above the 12h/2h
    # soft-refresh; the 7d hard Redis TTL still deletes records entirely.
    farmer_max_serve_stale_seconds: int = int(os.getenv("FARMER_MAX_SERVE_STALE_SECONDS", str(60 * 60 * 24)))
    # Farmer/animal API tracing records a PII-SAFE structure summary by default
    # (status, record count, which keys are present/null) — enough to prove
    # inconsistent returns without shipping farmer PII to Langfuse. Raw response
    # bodies are only captured when FARMER_API_TRACE_BODY is explicitly enabled
    # (temporary deep-debug), capped at FARMER_API_TRACE_BODY_CHARS.
    farmer_api_trace_body: bool = _get_bool_env("FARMER_API_TRACE_BODY", default=False)
    farmer_api_trace_body_chars: int = int(os.getenv("FARMER_API_TRACE_BODY_CHARS", "8000"))

    # Logging Configuration
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # External Service URLs
    telemetry_api_url: str = "https://vistaar.kenpath.ai/observability-service/action/data/v3/telemetry"
    nudge_api_url: str = os.getenv("NUDGE_API_URL", "https://vistaar.getraya.app/api/nudge-user")
    nudge_timeout_seconds: float = float(os.getenv("NUDGE_TIMEOUT_SECONDS", "3.0"))
    enable_voice_nudges: bool = _get_bool_env("ENABLE_VOICE_NUDGES", default=True)
    stt_signal_retry_ceiling: int = int(os.getenv("STT_SIGNAL_RETRY_CEILING", "3"))
    openai_pretranslation_timeout_seconds: float = float(os.getenv("OPENAI_PRETRANSLATION_TIMEOUT_SECONDS", "10.0"))
    voice_non_meaningful_timeout_seconds: float = float(os.getenv("VOICE_NON_MEANINGFUL_TIMEOUT_SECONDS", "0.60"))
    voice_non_meaningful_gate_timeout_seconds: float = float(os.getenv("VOICE_NON_MEANINGFUL_GATE_TIMEOUT_SECONDS", "0.50"))
    enable_voice_tracing: bool = _get_bool_env("ENABLE_VOICE_TRACING", default=True)
    voice_trace_text_mode: str = os.getenv("VOICE_TRACE_TEXT_MODE", "preview_hash")
    voice_trace_preview_chars: int = int(os.getenv("VOICE_TRACE_PREVIEW_CHARS", "120"))
    voice_trace_log_summary: bool = _get_bool_env("VOICE_TRACE_LOG_SUMMARY", default=True)
    # Shared voice profile prompt fields (rendered once at startup).
    voice_profile_creation_date_words: str = os.getenv(
        "VOICE_PROFILE_CREATION_DATE_WORDS",
        "eleventh February two thousand twenty six",
    )
    voice_profile_service_channels: str = os.getenv(
        "VOICE_PROFILE_SERVICE_CHANNELS",
        "chat, voice call, and WhatsApp",
    )
    voice_profile_helpline_number_words: str = os.getenv(
        "VOICE_PROFILE_HELPLINE_NUMBER_WORDS",
        "zero eight zero three five four five three five four five",
    )

    # Sticky %-split between OSS (vLLM gemma + pretranslation) and legacy pipelines.
    # A session is bucketed deterministically by session_id; OSS_PIPELINE_PCT
    # controls what fraction lands on OSS. With OSS_PIPELINE_PCT=0 (default) or
    # OSS_INFERENCE_ENDPOINT_URL unset, every session resolves to 'legacy' and
    # behaviour is byte-identical to today.
    oss_pipeline_pct: int = int(os.getenv("OSS_PIPELINE_PCT", "0"))
    oss_inference_endpoint_url: Optional[str] = os.getenv("OSS_INFERENCE_ENDPOINT_URL")
    oss_llm_model_name: Optional[str] = os.getenv("OSS_LLM_MODEL_NAME")
    oss_variant_ttl: int = int(os.getenv("OSS_VARIANT_TTL", str(60 * 60 * 24 * 7)))  # 7d sticky

    # Standard OSS -> managed fallback (see docs/oss-fallback-design.md).
    # Kill-switch defaults OFF: when false, pipelines keep today's behaviour.
    fallback_enabled: bool = os.getenv("FALLBACK_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }
    # Per-pipeline OSS time-to-respond budgets before falling back to managed.
    fallback_chat_oss_timeout_ms: int = int(os.getenv("FALLBACK_CHAT_OSS_TIMEOUT_MS", "8000"))
    fallback_moderation_oss_timeout_ms: int = int(os.getenv("FALLBACK_MODERATION_OSS_TIMEOUT_MS", "5000"))
    fallback_pretranslation_oss_timeout_ms: int = int(os.getenv("FALLBACK_PRETRANSLATION_OSS_TIMEOUT_MS", "10000"))
    fallback_suggestions_oss_timeout_ms: int = int(os.getenv("FALLBACK_SUGGESTIONS_OSS_TIMEOUT_MS", "6000"))
    # Deadline for the managed (fallback) tier.
    fallback_managed_timeout_ms: int = int(os.getenv("FALLBACK_MANAGED_TIMEOUT_MS", "20000"))

    # Voice pipeline behavioral flags
    # RETRIEVAL_AUDIT_LOG: log intent/retrieval_called/query per turn for replay analysis
    retrieval_audit_log: bool = _get_bool_env("RETRIEVAL_AUDIT_LOG", default=False)
    # AMBIGUITY_MATCH_THRESHOLD: fuzzy-match cutoff for ambiguity_terms.json (0.0–1.0)
    ambiguity_match_threshold: float = float(os.getenv("AMBIGUITY_MATCH_THRESHOLD", "0.80"))
    ollama_endpoint_url: Optional[str] = None
    marqo_endpoint_url: Optional[str] = None
    inference_endpoint_url: Optional[str] = None

    # External Service API Keys
    openai_api_key: Optional[str] = None
    sarvam_api_key: Optional[str] = None
    meity_api_key_value: Optional[str] = None
    langfuse_public_key: Optional[str] = None
    langfuse_secret_key: Optional[str] = None
    langfuse_base_url: Optional[str] = None
    # Langfuse labels: release = app/service name for grouping; environment = deployment env
    langfuse_release: Optional[str] = None  # e.g. "voice-oan-api" – shown in Langfuse for filtering
    langfuse_environment: Optional[str] = None  # e.g. "production" – defaults to ENVIRONMENT
    inference_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    mapbox_api_token: Optional[str] = None

    # AWS Configuration
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region: Optional[str] = None
    aws_s3_bucket: Optional[str] = None

    # LLM Configuration
    llm_provider: Optional[str] = None
    llm_model_name: Optional[str] = None
    marqo_index_name: Optional[str] = None

    class Config:
        env_file = ".env"
        extra = 'ignore'  # Ignore extra fields from .env

settings = Settings() 
