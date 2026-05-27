# Flat student-facing routes: /profile, /chat/history, /chat/{id}/messages,
# /study-materials/*. Admin endpoints are dropped (per user direction);
# catalog data is loaded via mongoimport for the demo.
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_student_id
from app.src.cache import Cache
from app.src.database import Data
from app.src.database.models.chats import ChatRecord
from app.src.database.models.messages import MessageRecord
from app.src.database.models.students import StudentProfile, StudentProfileUpdate
from app.src.database.models.study_materials import StudyMaterial

router = APIRouter(tags=["data"])

_cache = Cache()
_data = Data(_cache)


# --- profile ----------------------------------------------------------

@router.get("/profile", response_model=StudentProfile)
async def get_profile(student_id: str = Depends(get_student_id)) -> StudentProfile:
    return await _data.get_profile(student_id)


@router.put("/profile", response_model=StudentProfile)
async def update_profile(
    patch: StudentProfileUpdate, student_id: str = Depends(get_student_id)
) -> StudentProfile:
    return await _data.update_profile(student_id, patch)


# --- chat history + messages (the data-side of the chat API) ---------

@router.get("/chat/history", response_model=list[ChatRecord])
async def chat_history(
    limit: int = Query(default=50, ge=1, le=100),
    before: datetime | None = Query(default=None),
    student_id: str = Depends(get_student_id),
) -> list[ChatRecord]:
    return await _data.list_chats(student_id, limit=limit, before=before)


@router.get("/chat/{chat_id}/messages", response_model=list[MessageRecord])
async def chat_messages(
    chat_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    before: datetime | None = Query(default=None),
    student_id: str = Depends(get_student_id),
) -> list[MessageRecord]:
    return await _data.list_messages(
        student_id, chat_id, limit=limit, before=before
    )


# --- study materials catalog (browse / fetch one) ---------------------

@router.get("/study-materials/{material_id}", response_model=StudyMaterial)
async def get_material(
    material_id: str, _: str = Depends(get_student_id)
) -> StudyMaterial:
    return await _data.get_study_material(material_id)
