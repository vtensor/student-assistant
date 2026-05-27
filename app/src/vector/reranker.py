# RerankerClient: Jina hosted reranker; falls back to identity ordering on
# error or when no API key is set. URL + model + key all come from settings.
import httpx

from app.config import get_settings
from app.src.logs import log


class RerankerClient:
    async def rerank(
        self, *, query: str, docs: list[str], top_n: int
    ) -> list[int]:
        s = get_settings()
        if not s.reranker_api_key:
            return list(range(min(top_n, len(docs))))
        payload = {
            "model": s.reranker_model,
            "query": query,
            "documents": docs,
            "top_n": min(top_n, len(docs)),
        }
        headers = {
            "authorization": f"Bearer {s.reranker_api_key}",
            "content-type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(s.reranker_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("reranker_failed", err=str(e))
            return list(range(min(top_n, len(docs))))
        return [item["index"] for item in data.get("results", [])]
