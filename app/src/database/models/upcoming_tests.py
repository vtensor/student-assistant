# Upcoming tests: single test + per-student container.
from datetime import date

from pydantic import BaseModel, Field


class UpcomingTest(BaseModel):
    test_id: str
    subject: str
    test_name: str
    date: date
    topics: list[str] = Field(default_factory=list)


class UpcomingTestsRecord(BaseModel):
    student_id: str
    upcoming_tests: list[UpcomingTest]
