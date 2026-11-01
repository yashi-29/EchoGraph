from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    database_url: str = "postgresql+psycopg://digitaltwin:changeme@localhost:5432/digitaltwin"
    redis_url: str = "redis://localhost:6379/0"

    secret_key: str = "changeme-generate-a-real-secret"
    access_token_expire_minutes: int = 60 * 24
    session_cookie_name: str = "dt_session"
    cookie_secure: bool = False

    backend_cors_origins: list[str] = ["http://localhost:8080"]
    backend_base_url: str = "http://localhost:8000"
    frontend_base_url: str = "http://localhost:8080"

    anthropic_api_key: str | None = None

    groq_api_key: str | None = None
    groq_chat_model: str = "openai/gpt-oss-120b"
    # Voice-note uploads are transcribed with Groq's hosted Whisper (same GROQ_API_KEY).
    groq_transcription_model: str = "whisper-large-v3-turbo"

    # Manual uploads are parsed and indexed inside the request, so they're capped. Groq's
    # transcription endpoint accepts at most 25 MB on the free tier.
    upload_max_bytes: int = 20 * 1024 * 1024
    upload_audio_max_bytes: int = 25 * 1024 * 1024
    # Tesseract language packs for image uploads, "+"-joined (e.g. "eng+hin"); each one
    # must be installed in the backend image (tesseract-ocr-<lang>).
    ocr_languages: str = "eng"

    voyage_api_key: str | None = None
    # "voyage-4" is the flagship general-purpose model; matches the personal-project free tier
    # (200M tokens). The dimension must match memory_item.py's EMBEDDING_DIM/pgvector column —
    # changing either one without the other requires a migration.
    voyage_embedding_model: str = "voyage-4"
    voyage_embedding_dimension: int = 1024

    google_client_id: str | None = None
    google_client_secret: str | None = None
    # Gmail's first sync backfills this much history, one page per poll (every 15 min) so
    # neither Gmail's nor Voyage's rate limits are hit in one burst; later polls only fetch
    # what's new via Gmail's history API.
    gmail_backfill_days: int = 180
    gmail_backfill_max_messages: int = 1000
    gmail_sync_page_size: int = 100
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    notion_client_id: str | None = None
    notion_client_secret: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
