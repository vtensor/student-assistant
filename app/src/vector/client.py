# Weaviate async client + StudyMaterials collection schema (manual vectors).
from urllib.parse import urlparse

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.init import Auth

from app.config import get_settings
from app.src.logs import log

_client: weaviate.WeaviateAsyncClient | None = None


def _parse_url(url: str) -> tuple[str, int, bool]:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    secure = parsed.scheme == "https"
    port = parsed.port or (443 if secure else 8080)
    return host, port, secure


def _build_client() -> weaviate.WeaviateAsyncClient:
    s = get_settings()
    host, http_port, secure = _parse_url(s.weaviate_url)
    auth = Auth.api_key(s.weaviate_api_key) if s.weaviate_api_key else None
    return weaviate.use_async_with_custom(
        http_host=host,
        http_port=http_port,
        http_secure=secure,
        grpc_host=host,
        grpc_port=s.weaviate_grpc_port,
        grpc_secure=secure,
        auth_credentials=auth,
    )


def _is_alive(client: weaviate.WeaviateAsyncClient) -> bool:
    """Cheap check that the inner httpx client survived. The v4 client's
    `__send` does `assert self._client is not None`; if Weaviate was down
    at startup the client object exists but its underlying http connection
    never came up — we'd hit AssertionError on every request."""
    try:
        return getattr(client._connection, "_client", None) is not None
    except Exception:
        return False


async def get_weaviate() -> weaviate.WeaviateAsyncClient:
    """Return a connected Weaviate async client. Reconnects if the cached
    client lost its underlying transport (e.g. Weaviate was unreachable at
    startup but is up now). Only caches the client after `connect()` succeeds,
    so a failed startup never leaves a half-initialized object behind."""
    global _client
    if _client is not None and _is_alive(_client):
        return _client

    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
        _client = None
        log.info("weaviate_client_reconnect")

    client = _build_client()
    await client.connect()
    _client = client
    return _client


async def close_weaviate() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def ensure_collection() -> None:
    s = get_settings()
    client = await get_weaviate()
    name = s.weaviate_collection_study_materials
    if await client.collections.exists(name):
        return
    await client.collections.create(
        name=name,
        properties=[
            Property(name="material_id", data_type=DataType.TEXT),
            Property(name="topic", data_type=DataType.TEXT),
            Property(name="title", data_type=DataType.TEXT),
            Property(name="content", data_type=DataType.TEXT),
            Property(name="board", data_type=DataType.TEXT),
            Property(name="grade", data_type=DataType.INT),
            Property(name="mongo_doc_id", data_type=DataType.TEXT),
            Property(name="embedded_text", data_type=DataType.TEXT),
        ],
        vectorizer_config=Configure.Vectorizer.none(),
        inverted_index_config=Configure.inverted_index(bm25_b=0.75, bm25_k1=1.2),
    )
    log.info("weaviate_collection_created", name=name)


async def drop_collection() -> None:
    s = get_settings()
    client = await get_weaviate()
    name = s.weaviate_collection_study_materials
    if await client.collections.exists(name):
        await client.collections.delete(name)
