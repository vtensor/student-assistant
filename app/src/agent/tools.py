# Four tools the LLM can call.
#
# Implementation notes (NOT shown to the LLM):
# - The tool_node wrapper verifies the JWT on every call and injects
#   `student_id` and `data`; tool impls never read identity from `args`.
# - Tool arg schemas use ConfigDict(extra="forbid") so the LLM cannot
#   smuggle identity fields (student_id, jwt, etc.) into the call.
# - recommend_study_material uses Weaviate hybrid (BM25 + dense, RRF),
#   board+grade filter from the cached profile, optional Jina rerank.
# - get_performance_history caps at 10 subjects internally; the LLM does
#   not need to know.

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.src.database import Data


# --- tool 1: get_my_topics(strength) ---------------------------------

class GetMyTopicsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strength: Literal["strong", "weak"] = Field(
        description=(
            "Which list to retrieve. Pass 'weak' for topics the student "
            "currently needs to improve on. Pass 'strong' for topics the "
            "student is already confident in."
        ),
    )


async def get_my_topics(
    a: GetMyTopicsArgs, student_id: str, data: Data
) -> dict:
    profile = await data.get_profile(student_id)
    topics = profile.strong_topics if a.strength == "strong" else profile.weak_topics
    return {"strength": a.strength, "topics": topics}


# --- tool 2: get_upcoming_tests() ------------------------------------

class GetUpcomingTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def get_upcoming_tests(
    a: GetUpcomingTestsArgs, student_id: str, data: Data
) -> dict:
    today = date.today()
    tests = await data.get_upcoming_tests(student_id, from_date=today)
    capped = tests[:10]
    return {
        "as_of_date": today.isoformat(),
        "tests": [t.model_dump(mode="json") for t in capped],
    }


# --- tool 3: recommend_study_material(query) -------------------------

class RecommendStudyMaterialArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        min_length=3,
        max_length=500,
        description=(
            "A short search phrase about the topic or concept the student "
            "needs help with. Use exact topic names from the conversation "
            "when available. "
            "Examples: 'Algebra revision', 'Quadratic Equations practice', "
            "'Light - Reflection and Refraction', 'Trigonometry basics'."
        ),
    )


async def recommend_study_material(
    a: RecommendStudyMaterialArgs, student_id: str, data: Data
) -> dict:
    # USE_RERANKER env flag is the single switch; when on we fetch a larger
    # candidate pool from Weaviate and let the Jina reranker pick the top 3.
    from app.config import get_settings
    hits = await data.search_study_materials(
        student_id, a.query, top_k=3,
        use_reranker=get_settings().use_reranker,
    )
    results = [
        {"title": h.title, "topic": h.topic, "content": h.content}
        for h in hits
    ]
    return {"results": results}


# --- tool 4: get_performance_history(subject) ------------------------

class GetPerformanceHistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(
        description=(
            "Either 'all' to retrieve scores for every subject the student "
            "studies, OR one exact subject name from the student's subject "
            "list shown in the system prompt (e.g. 'Mathematics', "
            "'Physics', 'English'). Matching is case-insensitive. If the "
            "subject is not in the student's list, the results array will "
            "be empty."
        ),
    )


async def get_performance_history(
    a: GetPerformanceHistoryArgs, student_id: str, data: Data
) -> dict:
    perf = await data.get_performance(student_id)
    rows = [s.model_dump() for s in perf.subject_performance]
    if a.subject == "all":
        return {"subject": "all", "results": rows[:10]}
    needle = a.subject.lower()
    matches = [r for r in rows if r["subject"].lower() == needle]
    return {"subject": a.subject, "results": matches}


# --- registry --------------------------------------------------------

TOOLS = {
    "get_my_topics": (
        GetMyTopicsArgs,
        get_my_topics,
        "Retrieves the student's list of topics they are currently weak "
        "in OR strong in, from their stored profile. Pass strength='weak' "
        "to get topics the student needs to improve, or strength='strong' "
        "to get topics the student is already confident in. Returns a JSON "
        "object: {'strength': 'weak'|'strong', 'topics': [topic_name, ...]}. "
        "Use this whenever the student asks about their strengths, "
        "weaknesses, what to improve, or what they are already good at. "
        "This tool does not return performance scores or test history.",
    ),
    "get_upcoming_tests": (
        GetUpcomingTestsArgs,
        get_upcoming_tests,
        "Retrieves the student's upcoming tests, sorted by date in "
        "ascending order, returning only tests scheduled for today or any "
        "future date. Takes no arguments. Returns a JSON object: "
        "{'as_of_date': 'YYYY-MM-DD', 'tests': [{test_id, subject, "
        "test_name, date (YYYY-MM-DD), topics: [str, ...]}]}. Use this "
        "whenever the student asks about test schedules, exam dates, "
        "deadlines, what is coming up, or what they need to prepare for "
        "next. This tool does not return past tests or scores.",
    ),
    "recommend_study_material": (
        RecommendStudyMaterialArgs,
        recommend_study_material,
        "Searches the study materials catalog for resources matching a "
        "topic or concept the student wants help with. Results are "
        "automatically scoped to the student's board and grade. Returns "
        "a JSON object: {'results': [{'title': str, 'topic': str, "
        "'content_excerpt': str (up to ~400 chars)}]} - ordered by "
        "relevance, up to 5 items. Use this whenever the student asks for "
        "study resources, revision notes, videos, worksheets, practice "
        "problems, or help understanding any topic. Always cite the "
        "returned titles verbatim; never invent titles.",
    ),
    "get_performance_history": (
        GetPerformanceHistoryArgs,
        get_performance_history,
        "Retrieves the student's recent performance scores broken down by "
        "subject. Pass subject='all' to get every subject the student "
        "studies, or pass one exact subject name to get only that "
        "subject's score. Returns a JSON object: {'subject': str, "
        "'results': [{'subject': str, 'overall_score_percentage': float "
        "between 0 and 100}]}. Use this whenever the student asks about "
        "marks, scores, percentages, grades, or how they are doing. The "
        "list of valid subject names appears in your system prompt.",
    ),
}
