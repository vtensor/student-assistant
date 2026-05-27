# Mongo motor client + ensure_indexes for all collections.
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_settings

_client: AsyncIOMotorClient | None = None


def get_mongo_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncIOMotorClient(s.mongodb_uri, uuidRepresentation="standard")
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_mongo_client()[get_settings().mongodb_database]


async def ensure_indexes() -> None:
    db = get_db()
    await db.students.create_index("student_id", unique=True)
    await db.auth.create_index("email", unique=True)
    await db.auth.create_index("student_id", unique=True)
    await db.performance.create_index("student_id", unique=True)
    await db.upcoming_tests.create_index("student_id", unique=True)
    await db.study_materials.create_index("material_id", unique=True)
    await db.study_materials.create_index([("board", 1), ("grade", 1), ("topic", 1)])
    await db.chats.create_index("chat_id", unique=True)
    await db.chats.create_index([("student_id", 1), ("last_active_at", -1)])
    await db.messages.create_index([("chat_id", 1), ("created_at", 1)])
    await db.messages.create_index([("student_id", 1), ("created_at", -1)])
    await db.messages.create_index("message_id", unique=True)
    await db.long_term_memory.create_index("memory_id", unique=True)
    await db.long_term_memory.create_index([("student_id", 1), ("created_at", -1)])
    await db.long_term_memory.create_index([("student_id", 1), ("valid_until", 1)])
    await db.long_term_memory.create_index([("student_id", 1), ("category", 1)])
    await db.evals.create_index("request_id", unique=True)
    await db.evals.create_index([("student_id", 1), ("timestamp", -1)])
    await db.evals.create_index([("judge.sampled", 1)])
    await db.traces.create_index("request_id", unique=True)
    await db.traces.create_index([("student_id", 1), ("started_at", -1)])
    await db.traces.create_index([("chat_id", 1), ("started_at", -1)])


async def close_mongo() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
