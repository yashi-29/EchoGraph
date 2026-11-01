import asyncio
from typing import cast

import voyageai

from app.core.config import get_settings

settings = get_settings()

_client: voyageai.Client | None = None
# Voyage caps both texts and tokens per request; a big PDF or bookmark export easily
# exceeds one request, so documents are embedded in slices of this many texts.
_BATCH_SIZE = 128


def _get_client() -> voyageai.Client:
    global _client
    if _client is None:
        if not settings.voyage_api_key:
            raise RuntimeError("VOYAGE_API_KEY is not configured")
        _client = voyageai.Client(api_key=settings.voyage_api_key)
    return _client


async def _embed(texts: list[str], input_type: str) -> list[list[float]]:
    if not texts:
        return []
    client = _get_client()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        result = await asyncio.to_thread(
            client.embed,
            texts[start : start + _BATCH_SIZE],
            model=settings.voyage_embedding_model,
            input_type=input_type,
            output_dimension=settings.voyage_embedding_dimension,
        )
        # We never pass output_dtype, so Voyage always returns float vectors here.
        vectors.extend(cast(list[list[float]], result.embeddings))
    return vectors


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of documents for storage (not a search query)."""
    return await _embed(texts, input_type="document")


async def embed_query(text: str) -> list[float]:
    """Embed a single search query — Voyage's models are trained asymmetrically,
    so a query must use input_type="query" rather than "document" for good retrieval.
    """
    vectors = await _embed([text], input_type="query")
    return vectors[0]
