from groq import APIError, AsyncGroq

from app.core.config import get_settings

settings = get_settings()

# The formats Groq's transcription endpoint accepts.
AUDIO_EXTENSIONS = {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}

_client: AsyncGroq | None = None


class TranscriptionFailed(Exception):
    pass


def _get_client() -> AsyncGroq:
    global _client
    if _client is None:
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        _client = AsyncGroq(api_key=settings.groq_api_key)
    return _client


async def transcribe(filename: str, data: bytes) -> str:
    try:
        transcription = await _get_client().audio.transcriptions.create(
            model=settings.groq_transcription_model, file=(filename, data)
        )
    except APIError as exc:
        raise TranscriptionFailed(str(exc)) from exc
    return transcription.text.strip()
