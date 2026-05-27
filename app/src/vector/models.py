# Pydantic schemas for the vector search public surface.
from typing import Literal

from pydantic import BaseModel, Field

from app.src.database.models.study_materials import StudyMaterial


class SearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    board: Literal["CBSE", "ICSE"]
    grade: int = Field(ge=1, le=12)
    top_k: int = Field(default=5, ge=1, le=20)
    use_reranker: bool = False


class SearchHit(StudyMaterial):
    score: float
