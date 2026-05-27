# EmbeddingClient: Azure OpenAI embeddings with cache read-through.
import hashlib

from openai import AsyncAzureOpenAI

from app.config import get_settings
from app.src.cache import Cache

_client: AsyncAzureOpenAI | None = None


def _azure() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncAzureOpenAI(
            api_key=s.azure_openai_api_key,
            api_version=s.azure_openai_api_version,
            azure_endpoint=s.azure_openai_endpoint,
        )
    return _client


def _normalize(text: str) -> str:
    # normalize whitespace before hashing so minor edits still hit the cache
    return " ".join(text.split())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class EmbeddingClient:
    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    async def embed_one(self, text: str) -> list[float]:
        norm = _normalize(text)
        sha = _sha(norm)
        cached = await self._cache.get_embedding(sha)
        if cached is not None:
            return cached
        s = get_settings()
        resp = await _azure().embeddings.create(
            model=s.azure_openai_embedding_deployment, input=norm
        )
        vec = resp.data[0].embedding
        await self._cache.set_embedding(sha, vec)
        return vec

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        # bulk path for indexing; bypasses the cache for throughput
        if not texts:
            return []
        s = get_settings()
        resp = await _azure().embeddings.create(
            model=s.azure_openai_embedding_deployment,
            input=[_normalize(t) for t in texts],
        )
        return [d.embedding for d in resp.data]
