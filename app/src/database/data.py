# Data: single facade for ALL per-student reads/writes (Mongo + Vector + Cache).
# RLS lives HERE, not in routes: every method takes a verified `student_id: str`
# and every Mongo query is hard-wired with that student_id. No "target id" arg
# anywhere on the per-student methods, so cross-student leakage is
# structurally impossible.
from datetime import date, datetime, timezone
from uuid import uuid4

from app.exceptions import NotFound, ProfileNotFound
from app.src.cache import Cache
from app.src.database.client import get_db
from app.src.database.models.chats import ChatRecord
from app.src.database.models.memory import MemoryRecord
from app.src.database.models.messages import MessageRecord
from app.src.database.models.performance import PerformanceRecord
from app.src.database.models.students import StudentProfile, StudentProfileUpdate
from app.src.database.models.study_materials import StudyMaterial
from app.src.database.models.traces import TraceRecord
from app.src.database.models.upcoming_tests import UpcomingTest
from app.src.vector import SearchHit, SearchRequest, VectorService


class Data:
    """Single data facade. Routes, the agent, the tools — they all go through
    this class for any persistent state."""

    def __init__(self, cache: Cache) -> None:
        self._cache = cache
        # VectorService hydrates Mongo material rows by _id; we hand it a
        # bound method to avoid cross-module circular imports.
        self._vector = VectorService(cache, self._hydrate_materials_by_id)

    # --- raw collection handles (private) ------------------------------

    @property
    def _students(self):
        return get_db().students

    @property
    def _performance(self):
        return get_db().performance

    @property
    def _tests(self):
        return get_db().upcoming_tests

    @property
    def _materials(self):
        return get_db().study_materials

    @property
    def _chats(self):
        return get_db().chats

    @property
    def _messages(self):
        return get_db().messages

    @property
    def _memory(self):
        return get_db().long_term_memory

    @property
    def _traces(self):
        return get_db().traces

    # --- profile -------------------------------------------------------

    async def get_profile(self, student_id: str) -> StudentProfile:
        cached = await self._cache.get_profile(student_id)
        if cached is not None:
            return StudentProfile(**cached)
        doc = await self._students.find_one({"student_id": student_id})
        if not doc:
            raise ProfileNotFound("profile not found")
        doc.pop("_id", None)
        profile = StudentProfile(**doc)
        await self._cache.set_profile(student_id, profile.model_dump())
        return profile

    async def update_profile(
        self, student_id: str, patch: StudentProfileUpdate
    ) -> StudentProfile:
        update = {k: v for k, v in patch.model_dump(exclude_none=True).items()}
        if update:
            res = await self._students.update_one(
                {"student_id": student_id}, {"$set": update}
            )
            if res.matched_count == 0:
                raise ProfileNotFound("profile not found")
        await self._cache.invalidate_profile(student_id)
        return await self.get_profile(student_id)

    # --- performance ---------------------------------------------------

    async def get_performance(self, student_id: str) -> PerformanceRecord:
        cached = await self._cache.get_performance(student_id)
        if cached is not None:
            return PerformanceRecord(**cached)
        doc = await self._performance.find_one({"student_id": student_id})
        if not doc:
            return PerformanceRecord(
                student_id=student_id, subject_performance=[]
            )
        doc.pop("_id", None)
        rec = PerformanceRecord(**doc)
        await self._cache.set_performance(student_id, rec.model_dump())
        return rec

    # --- upcoming tests -----------------------------------------------

    async def get_upcoming_tests(
        self,
        student_id: str,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[UpcomingTest]:
        cached = await self._cache.get_tests(student_id)
        if cached is not None:
            tests = [UpcomingTest(**t) for t in cached]
        else:
            doc = await self._tests.find_one({"student_id": student_id})
            tests = (
                [UpcomingTest(**t) for t in doc.get("upcoming_tests", [])]
                if doc else []
            )
            await self._cache.set_tests(
                student_id, [t.model_dump(mode="json") for t in tests]
            )

        if from_date is not None:
            tests = [t for t in tests if t.date >= from_date]
        if to_date is not None:
            tests = [t for t in tests if t.date <= to_date]
        tests.sort(key=lambda t: t.date)
        return tests

    # --- study materials (catalog, not student-scoped) ----------------

    async def get_study_material(self, material_id: str) -> StudyMaterial:
        doc = await self._materials.find_one({"material_id": material_id})
        if not doc:
            raise NotFound(f"material {material_id} not found")
        doc.pop("_id", None)
        return StudyMaterial(**doc)

    async def list_study_materials(
        self, *, board: str | None = None, topic: str | None = None
    ) -> list[StudyMaterial]:
        query: dict = {}
        if board:
            query["board"] = board
        if topic:
            query["topic"] = topic
        cursor = self._materials.find(query)
        out: list[StudyMaterial] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(StudyMaterial(**doc))
        return out

    async def search_study_materials(
        self,
        student_id: str,
        query: str,
        *,
        top_k: int = 5,
        use_reranker: bool = False,
    ) -> list[SearchHit]:
        # board+grade come from the cached profile, NEVER from the LLM
        profile = await self.get_profile(student_id)
        req = SearchRequest(
            query=query,
            board=profile.board,
            grade=profile.grade,
            top_k=top_k,
            use_reranker=use_reranker,
        )
        return await self._vector.hybrid_search(req)

    # --- chats ---------------------------------------------------------

    async def create_chat(
        self, student_id: str, *, title: str | None = None
    ) -> ChatRecord:
        now = datetime.now(timezone.utc)
        chat = ChatRecord(
            chat_id=str(uuid4()),
            student_id=student_id,
            started_at=now,
            last_active_at=now,
            status="active",
            title=title,
        )
        doc = chat.model_dump(mode="json")
        doc["_id"] = str(uuid4())
        await self._chats.insert_one(doc)
        return chat

    async def get_chat(self, student_id: str, chat_id: str) -> ChatRecord:
        # RLS hard-wired: matched only if (chat_id, student_id) pair exists
        doc = await self._chats.find_one(
            {"chat_id": chat_id, "student_id": student_id}
        )
        if not doc:
            raise NotFound(f"chat {chat_id} not found")
        doc.pop("_id", None)
        return ChatRecord(**doc)

    async def list_chats(
        self,
        student_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[ChatRecord]:
        query: dict = {"student_id": student_id}
        if before is not None:
            query["last_active_at"] = {"$lt": before}
        cursor = self._chats.find(query).sort("last_active_at", -1).limit(limit)
        out: list[ChatRecord] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(ChatRecord(**doc))
        return out

    async def touch_chat(self, student_id: str, chat_id: str) -> None:
        await self._chats.update_one(
            {"chat_id": chat_id, "student_id": student_id},
            {"$set": {"last_active_at": datetime.now(timezone.utc)}},
        )

    async def end_chat(self, student_id: str, chat_id: str) -> None:
        res = await self._chats.update_one(
            {"chat_id": chat_id, "student_id": student_id},
            {"$set": {"status": "ended"}},
        )
        if res.matched_count == 0:
            raise NotFound(f"chat {chat_id} not found")

    async def delete_chat(self, student_id: str, chat_id: str) -> dict:
        # RLS: delete the chat row only if it belongs to this student. If no
        # match, raise NotFound BEFORE touching messages — so a wrong chat_id
        # cannot incidentally wipe another student's data.
        chat_res = await self._chats.delete_one(
            {"chat_id": chat_id, "student_id": student_id}
        )
        if chat_res.deleted_count == 0:
            raise NotFound(f"chat {chat_id} not found")
        msg_res = await self._messages.delete_many(
            {"chat_id": chat_id, "student_id": student_id}
        )
        await self._cache.delete_chat_session(student_id, chat_id)
        return {
            "chats_deleted": chat_res.deleted_count,
            "messages_deleted": msg_res.deleted_count,
        }

    async def set_chat_title_if_empty(
        self, student_id: str, chat_id: str, title: str
    ) -> None:
        # called once when the first user message arrives; truncates to 80 chars
        title = (title or "")[:80].strip() or "New chat"
        await self._chats.update_one(
            {"chat_id": chat_id, "student_id": student_id, "title": None},
            {"$set": {"title": title}},
        )

    # --- messages ------------------------------------------------------

    async def insert_message(
        self, student_id: str, message: MessageRecord
    ) -> MessageRecord:
        # the message MUST belong to the verified student; if the LLM tried
        # to put a different student_id on the row, we reject here.
        if message.student_id != student_id:
            raise NotFound("chat not found")  # opaque error, no info leak
        doc = message.model_dump(mode="json")
        doc["_id"] = str(uuid4())
        await self._messages.insert_one(doc)
        return message

    async def list_messages(
        self,
        student_id: str,
        chat_id: str,
        *,
        limit: int = 10,
        before: datetime | None = None,
    ) -> list[MessageRecord]:
        # RLS: confirm the chat belongs to this student first (raises 404 if not)
        await self.get_chat(student_id, chat_id)
        # Defense in depth: also filter the message query by student_id, so
        # if any stale row was ever inserted with a mismatched student_id
        # (e.g. from an older bug), it cannot surface here.
        query: dict = {"chat_id": chat_id, "student_id": student_id}
        if before is not None:
            query["created_at"] = {"$lt": before}
        # newest-first for "load older" pagination; frontend will reverse for display
        cursor = self._messages.find(query).sort("created_at", -1).limit(limit)
        out: list[MessageRecord] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(MessageRecord(**doc))
        out.reverse()  # return oldest-first within the batch
        return out

    # --- long-term memory ---------------------------------------------

    async def insert_memory(
        self, student_id: str, record: MemoryRecord
    ) -> MemoryRecord:
        if record.student_id != student_id:
            raise NotFound("memory not found")
        doc = record.model_dump(mode="json")
        doc["_id"] = str(uuid4())
        await self._memory.insert_one(doc)
        await self._cache.invalidate_memory(student_id)
        return record

    async def list_memory(
        self,
        student_id: str,
        *,
        category: str | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        query: dict = {"student_id": student_id}
        if category:
            query["category"] = category
        if active_only:
            today = date.today().isoformat()
            query["$or"] = [{"valid_until": None}, {"valid_until": {"$gte": today}}]
        cursor = self._memory.find(query).sort("created_at", -1).limit(limit)
        out: list[MemoryRecord] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(MemoryRecord(**doc))
        return out

    async def delete_memory(self, student_id: str, memory_id: str) -> None:
        res = await self._memory.delete_one(
            {"memory_id": memory_id, "student_id": student_id}
        )
        if res.deleted_count == 0:
            raise NotFound(f"memory {memory_id} not found")
        await self._cache.invalidate_memory(student_id)

    # --- traces (developer-facing; one doc per request_id) ------------

    async def insert_trace(self, trace: TraceRecord) -> None:
        # never let trace persistence break the chat response
        from app.src.logs import log
        try:
            doc = trace.model_dump(mode="json")
            doc["_id"] = trace.request_id
            await self._traces.insert_one(doc)
        except Exception as e:
            log.warning(
                "trace_insert_failed",
                rid=trace.request_id,
                err=f"{type(e).__name__}: {e}",
            )

    async def get_trace(self, request_id: str) -> TraceRecord | None:
        doc = await self._traces.find_one({"request_id": request_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return TraceRecord(**doc)

    async def list_traces_for_chat(
        self, chat_id: str, *, limit: int = 50
    ) -> list[TraceRecord]:
        cursor = (
            self._traces.find({"chat_id": chat_id})
            .sort("started_at", -1)
            .limit(limit)
        )
        out: list[TraceRecord] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(TraceRecord(**doc))
        return out

    # --- indexing (called by lifespan, not via HTTP) -------------------

    async def reindex_all_missing(self) -> tuple[str, int]:
        items = await self._list_materials_with_ids()
        return await self._vector.reindex_all(items, only_missing=True)

    # --- internal helpers ---------------------------------------------

    async def _hydrate_materials_by_id(
        self, mongo_ids: list[str]
    ) -> dict[str, StudyMaterial]:
        # Material _ids may be Mongo ObjectIds (created by mongoimport) or
        # UUID strings (created by app code). The indexer stores str(_id)
        # in Weaviate; here we convert back to ObjectId where possible so
        # the $in match works against the native field type.
        from bson import ObjectId
        from bson.errors import InvalidId

        if not mongo_ids:
            return {}
        candidates: list = []
        for mid in mongo_ids:
            try:
                candidates.append(ObjectId(mid))
            except (InvalidId, TypeError):
                candidates.append(mid)
        cursor = self._materials.find({"_id": {"$in": candidates}})
        out: dict[str, StudyMaterial] = {}
        async for doc in cursor:
            mid = str(doc.pop("_id"))
            out[mid] = StudyMaterial(**doc)
        return out

    async def _list_materials_with_ids(
        self,
    ) -> list[tuple[str, StudyMaterial]]:
        cursor = self._materials.find({})
        out: list[tuple[str, StudyMaterial]] = []
        async for doc in cursor:
            mid = str(doc.pop("_id"))
            out.append((mid, StudyMaterial(**doc)))
        return out
