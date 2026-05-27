# Eval facade: rule-based metrics (sync, every request) + sampled LLM judge.
# All methods take a `student_id: str` — no Principal class.
import re
from datetime import datetime, timezone
from uuid import uuid4

from app.src.database import Data
from app.src.database.client import get_db
from app.src.database.models.evals import EvalRecord, EvalRules
from app.src.eval.judge import judge as _llm_judge
from app.src.logs import log
from app.src.metrics import EVAL_RULE_FAILURES_TOTAL

# rough intent-to-expected-tool map; emits an "unexpected" rule failure metric
# when the LLM picked a different tool than the heuristic suggested
_INTENT_TO_TOOL = [
    (re.compile(r"\bweak\b|\bstruggl", re.I), "get_my_topics"),
    (re.compile(r"\bstrong\b|\bgood at\b", re.I), "get_my_topics"),
    (re.compile(r"\bperformance\b|\bscore\b|\bmarks\b|\bhow am i doing\b", re.I),
     "get_performance_history"),
    (re.compile(r"\btest\b|\bexam\b|\bschedule\b|\bupcoming\b|\bdeadline\b", re.I),
     "get_upcoming_tests"),
    (re.compile(r"\bmaterial\b|\bnotes\b|\bvideo\b|\bworksheet\b|\brevise\b|\bstudy\b", re.I),
     "recommend_study_material"),
]


def _tool_correctness(message: str, tool_names: list[str]) -> str:
    expected = None
    for pat, tool in _INTENT_TO_TOOL:
        if pat.search(message):
            expected = tool
            break
    if expected is None:
        return "none"
    return "expected" if expected in tool_names else "unexpected"


class Eval:
    """Offline eval facade. Writes go to the `evals` Mongo collection.
    `record()` is sync (cheap rules); `judge_async()` is fire-and-forget at
    the sample rate set in config."""

    def __init__(self, data: Data) -> None:
        self._data = data  # kept for future cross-checks

    @property
    def _evals(self):
        return get_db().evals

    async def record(
        self,
        *,
        request_id: str,
        student_id: str,
        chat_id: str,
        user_message: str,
        response: str,
        tool_trace: list[dict],
        latency_ms_total: int,
        latency_ms_llm: int,
        latency_ms_tools: int,
        latency_ms_pii: int,
        pii_input_categories: list[str],
        error: str | None,
    ) -> None:
        tool_names = [t.get("name") for t in tool_trace if t.get("name")]
        correctness = _tool_correctness(user_message, tool_names)
        rules = EvalRules(
            tool_count=len(tool_names),
            tool_names=tool_names,
            tool_correctness=correctness,
            latency_ms_total=latency_ms_total,
            latency_ms_llm=latency_ms_llm,
            latency_ms_tools=latency_ms_tools,
            latency_ms_pii=latency_ms_pii,
            pii_triggered_input=bool(pii_input_categories),
            pii_categories_input=pii_input_categories,
            # output PII redaction disabled; columns kept for schema compat
            # so old eval rows still load cleanly
            pii_triggered_output=False,
            pii_categories_output=[],
            response_nonempty=bool(response),
            response_token_count=len(response.split()),
            error=error,
            rate_limited=False,
        )

        if correctness == "unexpected":
            EVAL_RULE_FAILURES_TOTAL.labels(check="tool_correctness").inc()
        if not rules.response_nonempty:
            EVAL_RULE_FAILURES_TOTAL.labels(check="response_nonempty").inc()
        if rules.pii_triggered_output:
            EVAL_RULE_FAILURES_TOTAL.labels(check="pii_in_output").inc()

        rec = EvalRecord(
            request_id=request_id,
            chat_id=chat_id,
            student_id=student_id,
            timestamp=datetime.now(timezone.utc),
            rules=rules,
            judge=None,
        )
        doc = rec.model_dump(mode="json")
        doc["_id"] = str(uuid4())
        try:
            await self._evals.insert_one(doc)
        except Exception as e:
            log.warning("eval_record_failed", err=str(e))

    async def judge_async(
        self,
        *,
        request_id: str,
        user_message: str,
        response: str,
        tool_trace: list[dict],
    ) -> None:
        judge_row = await _llm_judge(
            user_message=user_message,
            response=response,
            tool_trace=tool_trace,
        )
        if judge_row is None:
            return
        try:
            await self._evals.update_one(
                {"request_id": request_id},
                {"$set": {"judge": judge_row.model_dump(mode="json")}},
            )
        except Exception as e:
            log.warning("eval_judge_persist_failed", err=str(e))

    async def list_for_student(
        self, student_id: str, *, limit: int = 100
    ) -> list[EvalRecord]:
        # RLS hard-wired: only this student's rows ever returned
        cursor = (
            self._evals.find({"student_id": student_id})
            .sort("timestamp", -1)
            .limit(limit)
        )
        out: list[EvalRecord] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(EvalRecord(**doc))
        return out
