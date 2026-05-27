# /evals: browse this student's own eval rows (RLS-hardwired).
from fastapi import APIRouter, Depends, Query

from app.dependencies import get_student_id
from app.src.cache import Cache
from app.src.database import Data
from app.src.database.models.evals import EvalRecord
from app.src.eval import Eval

router = APIRouter(tags=["eval"])
_eval = Eval(Data(Cache()))


@router.get("/evals", response_model=list[EvalRecord])
async def list_evals(
    limit: int = Query(default=100, ge=1, le=500),
    student_id: str = Depends(get_student_id),
) -> list[EvalRecord]:
    return await _eval.list_for_student(student_id, limit=limit)
