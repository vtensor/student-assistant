# VectorService: hybrid search + indexing facade over Weaviate.
# Uses HybridFusion.RANKED (Reciprocal Rank Fusion) for BM25 + dense merge,
# matching the Milvus-style RRF the user requested.
import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

import weaviate.classes.query as wq
from weaviate.classes.query import HybridFusion

from app.src.logs import log

from app.config import get_settings
from app.exceptions import VectorSearchFailed
from app.src.cache import Cache
from app.src.database.models.study_materials import StudyMaterial
from app.src.vector.client import ensure_collection, get_weaviate
from app.src.vector.embeddings import EmbeddingClient
from app.src.vector.models import SearchHit, SearchRequest
from app.src.vector.reranker import RerankerClient

RERANK_POOL = 20
EMBED_BATCH_SIZE = 32


def _deterministic_uuid(material_id: str) -> str:
    # stable UUID per material so re-indexing replaces (idempotent upsert)
    h = hashlib.sha1(material_id.encode()).hexdigest()[:32]
    return str(uuid.UUID(h))


def _embed_text(m: StudyMaterial) -> str:
    return f"{m.title}. Topic: {m.topic}. {m.content}"


@dataclass
class IndexJob:
    job_id: str
    status: str = "running"
    total: int = 0
    processed: int = 0
    errors: list[str] = field(default_factory=list)


class VectorService:
    """Public facade for vector search and indexing. The agent and routes
    talk only to this class; embeddings + reranker + Weaviate are internals."""

    def __init__(self, cache: Cache, hydrate_by_mongo_ids) -> None:
        # hydrate_by_mongo_ids: async callable(list[str]) -> dict[str, StudyMaterial]
        # injected from Data to avoid a circular module dep
        self._cache = cache
        self._hydrate = hydrate_by_mongo_ids
        self._embed = EmbeddingClient(cache)
        self._reranker = RerankerClient()
        self._jobs: dict[str, IndexJob] = {}

    # --- search ---------------------------------------------------------

    async def hybrid_search(self, req: SearchRequest) -> list[SearchHit]:
        await ensure_collection()
        s = get_settings()
        client = await get_weaviate()
        coll = client.collections.get(s.weaviate_collection_study_materials)

        try:
            qvec = await self._embed.embed_one(req.query)
        except Exception as e:
            raise VectorSearchFailed(f"embedding failed: {e}") from e

        fetch_limit = RERANK_POOL if req.use_reranker else req.top_k
        try:
            res = await coll.query.hybrid(
                query=req.query,
                vector=qvec,
                fusion_type=HybridFusion.RANKED,   # Reciprocal Rank Fusion (RRF)
                limit=fetch_limit,
                filters=(
                    wq.Filter.by_property("board").equal(req.board)
                    & wq.Filter.by_property("grade").equal(req.grade)
                ),
                return_metadata=wq.MetadataQuery(score=True),
                return_properties=[
                    "material_id", "topic", "title", "content",
                    "board", "grade", "mongo_doc_id",
                ],
            )
        except Exception as e:
            raise VectorSearchFailed(f"hybrid search failed: {e}") from e

        hits = await self._to_hits(res.objects)
        if req.use_reranker and len(hits) > req.top_k:
            order = await self._reranker.rerank(
                query=req.query,
                docs=[f"{h.title}. {h.content}" for h in hits],
                top_n=req.top_k,
            )
            hits = [hits[i] for i in order if i < len(hits)][: req.top_k]
        return hits[: req.top_k]

    async def _to_hits(self, objects: Any) -> list[SearchHit]:
        if not objects:
            return []
        doc_ids = [
            o.properties.get("mongo_doc_id") for o in objects
            if o.properties.get("mongo_doc_id")
        ]
        materials = await self._hydrate(doc_ids)
        out: list[SearchHit] = []
        for o in objects:
            mid = o.properties.get("mongo_doc_id")
            m = materials.get(mid) if mid else None
            if m is None:
                continue
            score = (
                float(o.metadata.score)
                if o.metadata and o.metadata.score is not None
                else 0.0
            )
            out.append(SearchHit(**m.model_dump(), score=score))
        return out

    # --- indexing -------------------------------------------------------

    async def reindex_all(
        self,
        items: list[tuple[str, StudyMaterial]],
        *,
        only_missing: bool = False,
    ) -> tuple[str, int]:
        await ensure_collection()
        if only_missing:
            items = await self._filter_missing(items)
        job = IndexJob(job_id=str(uuid.uuid4()), total=len(items))
        self._jobs[job.job_id] = job
        asyncio.create_task(self._run_job(job, items))
        return job.job_id, len(items)

    async def reindex_one(self, mongo_doc_id: str, m: StudyMaterial) -> str:
        await ensure_collection()
        job = IndexJob(job_id=str(uuid.uuid4()), total=1)
        self._jobs[job.job_id] = job
        asyncio.create_task(self._run_job(job, [(mongo_doc_id, m)]))
        return job.job_id

    def get_job(self, job_id: str) -> IndexJob | None:
        return self._jobs.get(job_id)

    async def _filter_missing(
        self, items: list[tuple[str, StudyMaterial]]
    ) -> list[tuple[str, StudyMaterial]]:
        if not items:
            return items
        s = get_settings()
        client = await get_weaviate()
        coll = client.collections.get(s.weaviate_collection_study_materials)
        missing: list[tuple[str, StudyMaterial]] = []
        for mid, m in items:
            uuid_ = _deterministic_uuid(m.material_id)
            try:
                exists = await coll.data.exists(uuid_)
            except Exception:
                exists = False
            if not exists:
                missing.append((mid, m))
        return missing

    async def _run_job(
        self, job: IndexJob, items: list[tuple[str, StudyMaterial]]
    ) -> None:
        s = get_settings()
        try:
            client = await get_weaviate()
            coll = client.collections.get(s.weaviate_collection_study_materials)
            for start in range(0, len(items), EMBED_BATCH_SIZE):
                batch = items[start : start + EMBED_BATCH_SIZE]
                texts = [_embed_text(m) for _, m in batch]
                try:
                    vectors = await self._embed.embed_many(texts)
                except Exception as e:
                    job.errors.append(f"embed batch failed: {e}")
                    continue
                for (mongo_id, m), text, vec in zip(batch, texts, vectors):
                    uuid_ = _deterministic_uuid(m.material_id)
                    props = {
                        "material_id": m.material_id,
                        "topic": m.topic,
                        "title": m.title,
                        "content": m.content,
                        "board": m.board,
                        "grade": m.grade,
                        "mongo_doc_id": mongo_id,
                        "embedded_text": text,
                    }
                    try:
                        if await coll.data.exists(uuid_):
                            await coll.data.replace(
                                uuid=uuid_, properties=props, vector=vec
                            )
                        else:
                            await coll.data.insert(
                                uuid=uuid_, properties=props, vector=vec
                            )
                        job.processed += 1
                    except Exception as e:
                        job.errors.append(f"upsert {m.material_id} failed: {e}")
            job.status = "failed" if job.errors and job.processed == 0 else "done"
        except Exception as e:
            log.exception("index_job_crashed")
            job.errors.append(str(e))
            job.status = "failed"
