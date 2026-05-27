# Chat record (renamed from sessions). One row per conversation thread.
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ChatRecord(BaseModel):
    chat_id: str
    student_id: str
    started_at: datetime
    last_active_at: datetime
    status: Literal["active", "ended"] = "active"
    title: str | None = None
