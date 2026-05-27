# Per-student subject performance schema.
from pydantic import BaseModel, Field


class SubjectScore(BaseModel):
    subject: str
    overall_score_percentage: float = Field(ge=0, le=100)


class PerformanceRecord(BaseModel):
    student_id: str
    subject_performance: list[SubjectScore]
