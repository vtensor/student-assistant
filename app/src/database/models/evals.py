# Eval row: deterministic rules (every request) + optional sampled LLM judge.
from datetime import datetime

from pydantic import BaseModel, Field


class EvalRules(BaseModel):
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    tool_correctness: str = "none"  # expected | unexpected | none
    latency_ms_total: int = 0
    latency_ms_llm: int = 0
    latency_ms_tools: int = 0
    latency_ms_pii: int = 0
    pii_triggered_input: bool = False
    pii_categories_input: list[str] = Field(default_factory=list)
    pii_triggered_output: bool = False
    pii_categories_output: list[str] = Field(default_factory=list)
    response_nonempty: bool = True
    response_token_count: int = 0
    error: str | None = None
    rate_limited: bool = False


class EvalJudge(BaseModel):
    sampled: bool = True
    answer_correctness: float = Field(ge=0.0, le=1.0)
    context_utilization: float = Field(ge=0.0, le=1.0)
    faithfulness: float = Field(ge=0.0, le=1.0)
    rationale: str
    judge_latency_ms: int


class EvalRecord(BaseModel):
    request_id: str
    chat_id: str
    student_id: str
    timestamp: datetime
    rules: EvalRules
    judge: EvalJudge | None = None
