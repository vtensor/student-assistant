# /metrics route: Prometheus text-format exposition.
from fastapi import APIRouter, Response

from app.src.metrics.metrics import render

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics() -> Response:
    body, content_type = render()
    return Response(content=body, media_type=content_type)
