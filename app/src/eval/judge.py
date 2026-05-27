# LLM-as-judge: structured-output scoring of (user, tools, response) triples.
import time
from pathlib import Path

from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field

from app.src.database.models.evals import EvalJudge
from app.src.llm import get_structured_model
from app.src.logs import log

_PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


class _JudgeOutput(BaseModel):
    answer_correctness: float = Field(ge=0.0, le=1.0)
    context_utilization: float = Field(ge=0.0, le=1.0)
    faithfulness: float = Field(ge=0.0, le=1.0)
    rationale: str


def _format_trace(trace: list[dict]) -> str:
    if not trace:
        return "(no tools called)"
    return "\n".join(
        f"- {t.get('name')}: {t.get('result_preview', '')[:200]}" for t in trace
    )


async def judge(
    *, user_message: str, response: str, tool_trace: list[dict]
) -> EvalJudge | None:
    prompt = _PROMPT.format(
        user_message=user_message,
        tool_trace=_format_trace(tool_trace),
        response=response or "(empty)",
    )
    started = time.perf_counter()
    try:
        llm = get_structured_model().with_structured_output(_JudgeOutput)
        out: _JudgeOutput = await llm.ainvoke([SystemMessage(content=prompt)])
    except Exception as e:
        log.warning("eval_judge_failed", err=str(e))
        return None
    elapsed = int((time.perf_counter() - started) * 1000)
    return EvalJudge(
        sampled=True,
        answer_correctness=out.answer_correctness,
        context_utilization=out.context_utilization,
        faithfulness=out.faithfulness,
        rationale=out.rationale,
        judge_latency_ms=elapsed,
    )
