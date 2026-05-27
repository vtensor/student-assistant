# Study material schema (catalog row) + create alias.
from typing import Literal

from pydantic import BaseModel, Field


class StudyMaterial(BaseModel):
    material_id: str
    topic: str
    title: str
    board: Literal["CBSE", "ICSE"]
    grade: int = Field(ge=1, le=12)
    content: str


class StudyMaterialCreate(StudyMaterial):
    pass
