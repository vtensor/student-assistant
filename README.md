# Student Learning Assistant

A personal tutoring agent for Class 10 students (CBSE / ICSE). The student chats with a LangGraph ReAct agent that calls four tools against the student's own profile, performance, tests, and a hybrid-search study-material catalog. Designed as a modular monolith: one FastAPI process for the backend, one Streamlit process for the chat UI, and three stateful services (Mongo, Redis, Weaviate).

The agent is built around strict per-student isolation, end-to-end PII redaction on user input, structured tracing of every tool call, and offline evaluation (rule-based on every request, LLM-judge on a 5% sample).

This README walks through the full system: stack, architecture, agent flow, tools, data model, sample data, setup, and a roadmap.

---

## Table of contents

1. [Tech stack](#tech-stack)
2. [Architecture](#architecture)
3. [Tenant and session isolation](#tenant-and-session-isolation)
4. [Redis structure and use cases](#redis-structure-and-use-cases)
5. [Agent architecture](#agent-architecture)
6. [Tools](#tools)
7. [PII redaction](#pii-redaction)
8. [Memory](#memory)
9. [Tracing](#tracing)
10. [Evaluation](#evaluation)
11. [Rate limiting](#rate-limiting)
12. [Metrics](#metrics)
13. [Database models](#database-models)
14. [Collections and indexes](#collections-and-indexes)
15. [Sample data and what changed](#sample-data-and-what-changed)
16. [Hybrid search pipeline](#hybrid-search-pipeline)
17. [API endpoints](#api-endpoints)
18. [Prerequisites](#prerequisites)
19. [Setup and usage](#setup-and-usage)
20. [Way forward](#way-forward)

---

## Tech stack

### Models used

| Role | Model | Provider |
|---|---|---|
| LLM (chat + tool routing) | `gpt-4.1-mini` | Azure OpenAI |
| Embeddings (study material vectors + query) | `text-embedding-3-small` (1536-dim) | Azure OpenAI |
| Reranker (optional, hybrid hits) | `jina-reranker-v3` | Jina |
| PII detector | Regex heuristics (no ML model) | In-process |

The LLM is also used for two secondary tasks: extracting long-term memory candidates from a chat (lower output cap) and acting as the offline judge for sampled responses. Both reuse the same gpt-4.1-mini deployment via a separate `get_structured_model()` factory with `temperature=0.0` and a tighter token cap.

### Runtime stack

| Layer | Choice | Why |
|---|---|---|
| HTTP | FastAPI + Uvicorn | Async, OpenAPI for free, easy router composition |
| Agent framework | LangGraph + LangChain | First-class state graph, native tool binding |
| Schemas | Pydantic v2 + pydantic-settings | One env-driven config object, models double as API schemas |
| Structured store | MongoDB 7 (via Motor async driver) | Document model fits student profile, tests, messages, memory natively; flexible schema for evolving features |
| Cache and session turns | Redis 7 | Per-student namespaced keys, sub-millisecond reads, atomic token-bucket rate limiting via Lua |
| Hybrid vector search | Weaviate 1.27 | First-class BM25 + dense fusion (RRF), filter-by-property at query time, idempotent upsert by stable UUID |
| Auth | Custom JWT (HS256) + Argon2id passwords | Single decode point, no third-party identity coupling, structural row-level security |
| Frontend | Streamlit 1.40 | Fast iteration on a chat UI without a SPA build pipeline |
| Metrics | Prometheus client | Single registry, exposed at `/metrics` |
| Logs | python-json-logger | Structured JSON to stdout, request_id + endpoint bound per request via ContextVars |

### Why these choices

- **Mongo over Postgres**: every domain object (profile, tests, messages, memory) is a nested document with optional fields. Schema evolution is constant during agent development; Mongo absorbs new fields without migrations.
- **Redis as cache + session store + rate limiter**: one dependency does three jobs. Profile and performance reads cache for 15-30 min; recent chat turns live in a sliding Redis LIST per chat; the token-bucket rate limiter uses an atomic Lua script.
- **Weaviate over pgvector**: native hybrid (BM25 + dense) with Reciprocal Rank Fusion (`HybridFusion.RANKED`), property filters (board, grade), and idempotent upsert by deterministic UUID. Avoids running our own BM25.
- **Custom JWT auth (no Auth0 / Cognito)**: the project's "single decode point" property is load-bearing for tenant isolation. A third-party SDK would obscure where verification happens. Argon2id (via `argon2-cffi`) is the current OWASP recommendation for password hashing.
- **gpt-4.1-mini, not o-series**: the agent needs reliable tool calling and configurable temperature. o-series reasoning models force `temperature=1.0` and `top_p=1.0`, which is too random for tool routing. `gpt-4.1-mini` allows temperature tuning while being cost-efficient. The LLM client at `app/src/llm/client.py` auto-detects o-series prefixes and pins the forced parameters if you ever switch back.

---

## Architecture

```
                                  Browser
                                     |
                                     | HTTPS + JWT
                                     v
+--------------------------------------------------------------+
|                       sa_app  (one container)                |
|                                                              |
|   :8501  Streamlit UI  -- httpx --> http://localhost:8000    |
|                                                              |
|   :8000  FastAPI backend                                     |
|   middleware:   request_id + endpoint binding                |
|                                                              |
|   /auth/*   --> Identity  --> Mongo.auth + students          |
|   /profile  --> Data      --> Mongo + Redis (cache)          |
|   /chat     --> Agent     --> LangGraph + Tools (blocking)   |
|   /achat    --> Agent     --> LangGraph + Tools (SSE stream) |
|   /metrics  --> Prometheus exposition                        |
|                                                              |
|   shared:   Cache (Redis facade)                             |
|             RateLimiter (Lua token bucket)                   |
|             PII (regex redactor)                             |
|             Memory (short-term + long-term)                  |
|             VectorService (Weaviate + embed)                 |
|             Eval (rules + sampled judge)                     |
+-----+----------------+--------------------+------------------+
      |                |                    |
      v                v                    v
+----------+   +-------------+   +-------------------+
| MongoDB  |   | Redis       |   | Weaviate          |
| :27017   |   | :6379       |   | :8080 + :50051    |
+----------+   +-------------+   +-------------------+
```

All four services live in a single `docker-compose.yml`: three stateful backing services (Mongo, Redis, Weaviate) plus one combined application image that runs the FastAPI backend on port 8000 and the Streamlit frontend on port 8501 inside the same container. Streamlit talks to the backend over `localhost:8000`.

---

## Tenant and session isolation

This is the most load-bearing design choice in the codebase. Every read and write is scoped to a specific student, and there is no global "current user" context.

### Tenant (per-student) isolation

- **Single decode point**. `Identity.verify(jwt_token: str) -> student_id: str` in `app/src/auth/identity.py` is the only place JWTs are decoded. Routes call it via `Depends(get_student_id)`.
- **No Principal class**. Identity flows as a plain `student_id: str`. There is no object with extra capabilities that could be passed around accidentally.
- **Every Data method takes student_id**. Methods like `Data.get_profile(student_id)`, `Data.list_chats(student_id)`, `Data.get_chat(student_id, chat_id)` always filter by `student_id`. There is no "target student" parameter. Cross-tenant queries are structurally impossible.
- **Tool argument schemas forbid extras**. The LangChain tool schemas use `model_config = ConfigDict(extra="forbid")` so the LLM cannot smuggle `student_id` or `chat_id` in via crafted arguments. Identity is injected by the tool dispatcher, not by the model.
- **Defense in depth on tools**. The `/chat` route uses `Depends(get_jwt)` to thread the raw token into the agent. The tool node calls `Identity.verify(jwt)` again before executing tools, so a token that expires mid-turn fails every tool in the batch uniformly rather than running with stale identity.
- **Redis namespacing**. Every per-student Redis key is prefixed by `student_id` (see Redis section). Cross-student key collisions are impossible by construction.

### Session (per-chat) isolation

- `chat_id` is paired with `student_id` in every query: `db.chats.find_one({"chat_id": chat_id, "student_id": student_id})`. Messaging routes filter both, never one alone.
- The Redis session key encodes both: `{student_id}:chat_session:{chat_id}`. One student's two chats are stored in two independent Redis LISTs and cannot bleed into each other.
- When a chat is deleted, three things are wiped: the `chats` row, all `messages` for that `chat_id`, and the Redis session key. The cleanup function is `Data.delete_chat(student_id, chat_id)` in `app/src/database/data.py`.
- The `messages` collection always co-filters `{chat_id, student_id}` even though `chat_id` alone is unique. This is defense in depth in case an old row was ever inserted with a mismatched `student_id`.

### What the LLM never sees

The system prompt, tool schemas, and tool results passed back to the LLM never include `student_id`, `chat_id`, `jwt`, or `request_id`. The LLM cannot reason about other students because it has no identifiers for them.

---

## Redis structure and use cases

Redis is a single Redis 7 instance acting as cache, session store, rate limiter, and embedding cache. All keys live in one logical database (db 0).

### Key namespaces

Helper: `Cache._k(student_id, ns, ident="")` produces `"{student_id}:{ns}"` or `"{student_id}:{ns}:{ident}"`. Global keys use `_global_k(ns, ident)` producing `"global:{ns}:{ident}"`.

| Namespace | Key pattern | TTL config var | Default TTL | Type | Purpose |
|---|---|---|---|---|---|
| `profile` | `{sid}:profile` | `ttl_profile` | 900s | JSON string | Cached StudentProfile to avoid Mongo hit on every chat turn |
| `performance` | `{sid}:performance` | `ttl_performance` | 600s | JSON string | Cached PerformanceRecord for `get_performance_history` tool |
| `tests` | `{sid}:tests` | `ttl_tests` | 300s | JSON string | Cached UpcomingTestsRecord for `get_upcoming_tests` tool |
| `lt_memory` | `{sid}:lt_memory` | `ttl_memory` | 1800s | JSON string | Sorted list of long-term memory entries; fed into the system prompt |
| `chat_session` | `{sid}:chat_session:{chat_id}` | `ttl_session` | 3600s | LIST | Recent (user, assistant) turns for the active chat, newest-first |
| `embedding` | `global:embedding:{sha256(normalized_text)}` | `ttl_embedding` | 86400s | JSON list (float vector) | Reuse embeddings across identical queries from any student |
| `ratelimit` | `ratelimit:{user_uuid}` | bucket auto-expires | scales with capacity / refill | HASH (tokens, ts) | Token-bucket counters for `/chat` and `/achat` |

### Lifecycle and invalidation

- **Reads are cache-first**. `Data.get_profile(sid)` checks Redis, falls back to Mongo, repopulates Redis on miss.
- **Writes invalidate**. `Data.update_profile()` calls `cache.invalidate_profile(sid)`. Same pattern for performance, tests, memory.
- **Chat session is a sliding window**. `cache.push_chat_session()` uses an atomic pipeline of `LPUSH`, `LTRIM` to `max_turns_in_context` (default 10), and `EXPIRE`. Old turns drop off automatically. Cold reads (TTL expired) fall back to Mongo via `Memory.session_recent()`.
- **Embedding cache is global**. Same text from two different students produces the same vector, so the cache key has no `student_id`. Text is whitespace-normalized before hashing so trivial edits hit the cache.
- **Rate-limit bucket auto-expires**. PEXPIRE is set to `(capacity * 1000 / refill_per_sec) + 1000 ms` so cold accounts do not leave stale keys.

### Live samples (current Redis state)

From `redis-cli --scan` on the running container right now:

| Real key | Redis type | Sample contents | TTL remaining |
|---|---|---|---|
| `SDA728DEF6C:chat_session:1060be0d-...-1db75bf151a2` | LIST | element 0: `{"content": "Hello Arjun!...", "role": "assistant"}` <br> element 1: `{"content": "hi", "role": "user"}` | ~2802s of 3600s sliding window |
| `global:embedding:578caa84...b3ba034` | STRING | `[0.01702880859375, -0.047088623046875, 0.0191802978515625, ...]` (1536-element JSON float array) | ~75441s of 86400s |
| (none) | HASH | A rate-limit bucket would look like `{"tokens": "9.4", "ts": "1748312345678"}` at key `ratelimit:{student_id}` | scales with capacity / refill |
| (none cached now) | STRING | `profile`, `performance`, `tests`, `lt_memory` keys hold a single JSON string each; absent here because they expired or were invalidated | as in table above |

Each LIST entry is a JSON-encoded `{"role": "...", "content": "..."}` object. Newest is at index 0 because `push_chat_session` uses `LPUSH`. `load_context` reverses the list to replay oldest-first into the LLM.

The 9 chat_session keys currently live are all for student `SDA728DEF6C`. Each corresponds to one chat; a chat with no traffic for an hour drops its session key (Mongo `messages` collection remains the source of truth for cold replays).

---

## Agent architecture

The agent is a LangGraph state graph. State carries the conversation messages, identity, tool trace, and per-phase timings. The graph compiles once at import time.

### Node flow

```
START
  |
  v
pii_in         (redact PII from raw user message)
  |
  v
load_context   (build system prompt + replay recent turns from Redis)
  |
  v
agent  <----+  (LLM call; streams tokens to the response queue as they arrive;
  |         |   may emit tool_calls instead of content)
  | has tool_calls?
  |    yes  |
  v         |
tools ------+  (execute every tool call serially, re-verify JWT)
  |
  | no tool_calls
  v
persist        (write user + final assistant rows to Mongo; push to Redis session)
  |
  v
END
```

Edges:
- `START -> pii_in -> load_context -> agent`
- `agent -> tools` if the last message has `tool_calls`, else `agent -> persist`
- `tools -> agent` (loop back so the model can read tool results and decide what to do next)
- `persist -> END`

The agent loop is bounded by the LLM's own decision to stop emitting tool calls. There is no hard cap on tool-call iterations; the model decides when it has enough context to answer.

There is no `pii_out` node in the current flow. Output-side masking was dropped because the response is streamed to the client as tokens arrive from the LLM. By the time the graph reaches the end, the user has already seen every token, so post-hoc redaction would be useless. PII handling now applies only on input; output-side PII detection still happens for metrics but does not mutate text (see the PII section).

### What each node does

- **`pii_in`**: scans the raw user message with the four PII regexes, replaces each hit with `<PII:CATEGORY>`, records `pii_input_categories` in state, increments `pii_detections_total{surface="input"}` per category.
- **`load_context`**: looks up `Identity.verify(jwt)` to get `student_id`, fetches the cached StudentProfile, loads recent turns from Redis (Mongo fallback), and renders the system prompt template with `{student_name, grade, board, target_exam, current_date, current_weekday, subjects_list, memories_preview}`. Pushes a SystemMessage plus replayed HumanMessage / AIMessage pairs plus the redacted current HumanMessage into state.
- **`agent`**: calls `bound_model.astream(state["messages"])`. The bound model has the 4 tools registered via `bind_tools`. As content chunks arrive, the node pushes `("token", {"text": chunk})` items onto `state["stream_queue"]` so the SSE route can emit them to the browser in real time. The accumulated AIMessage is appended to `state["messages"]` and `latency_ms_llm` is recorded. If the model emits tool calls instead of content, those route to the `tools` node.
- **`tools`**: re-verifies the JWT once at entry. For each tool call in the last AIMessage: looks up the tool in the `TOOLS` registry, validates args against the Pydantic schema, calls the async impl with `(parsed_args, student_id, data_service)`, serializes the result, appends a ToolMessage, appends a trace entry `{name, args, result_preview}` (preview truncated to 240 chars). Increments `tool_calls_total{tool=name}` per call.
- **`persist`**: writes one user MessageRecord and one assistant MessageRecord to Mongo `messages`, calls `data.touch_chat()` to bump `last_active_at`, and calls `data.set_chat_title_if_empty()` to set the chat title from the first user message. Pushes both turns to the Redis chat session via `memory.session_push()`. Intermediate AI and tool messages stay in-graph only; only the user message and the final assistant reply are persisted.

### Streaming response (default)

The response is streamed as Server-Sent Events via `POST /achat`. The route calls `Agent.run_stream()`, which runs the graph in a background task and drains an `asyncio.Queue` populated by `agent_node`. The HTTP layer wraps each item as an SSE frame:

- `event: token` with `data: {"text": "..."}` per content chunk produced by the LLM
- `event: done` with `data: {request_id, chat_id, latency_ms, full_text}` as the terminal frame
- `event: error` with `data: {message: "..."}` if the graph throws, followed by `done`

A blocking variant (`POST /chat`) is also available; it returns a single JSON ChatResponse after the whole graph completes. Useful for non-browser clients and replayable tests. Both routes call the same compiled graph.

---

## Tools

The `TOOLS` dict in `app/src/agent/tools.py` maps each tool name to `(args_schema, async_impl, description)`. The LangGraph `agent` node binds these via `bind_tools()` so the LLM sees them as callable functions with JSON schemas.

Every tool impl signature is `async def impl(args: ToolArgs, student_id: str, data: Data) -> dict`. The student_id is never in the args; it is injected by the tool node after JWT re-verification.

### 1. `get_my_topics(strength)`

- **Purpose**: list the student's strong topics or weak topics from their profile.
- **Input**: `{"strength": "strong" | "weak"}`
- **Output**: `{"strength": "strong" | "weak", "topics": ["Algebra", "Quadratic Equations", ...]}`

Used when the student asks things like "what am I struggling with" or "where am I strong".

### 2. `get_performance_history(subject)`

- **Purpose**: return recent score percentages, either for one subject or for all enrolled subjects.
- **Input**: `{"subject": "Mathematics"}` or `{"subject": "all"}`
- **Output**: `{"subject": "Mathematics", "results": [{"subject": "Mathematics", "overall_score_percentage": 52.0}]}` (filtered) or all subjects if `"all"`. Capped at 10 subjects.

Used when the student asks "how am I doing in Science" or "what are my recent scores".

### 3. `get_upcoming_tests()`

- **Purpose**: list the student's upcoming tests sorted by date.
- **Input**: `{}` (no args)
- **Output**: `{"as_of_date": "2026-05-27", "tests": [{"test_id": "T201", "subject": "Mathematics", "test_name": "Math Weekly Test", "date": "2026-05-28", "topics": ["Algebra", "Quadratic Equations"]}, ...]}`. Capped at 10 tests.

Used for "do I have any tests coming up" or "what's my next test".

### 4. `recommend_study_material(query)`

- **Purpose**: hybrid search over the StudyMaterials catalog (BM25 + dense, RRF) filtered to the student's board and grade.
- **Input**: `{"query": "discriminant of quadratic"}` (3 to 500 chars)
- **Output**: `{"results": [{"title": "Quadratic Equations Concept Video", "topic": "Quadratic Equations", "content": "..."}, ...]}`. Top-3 by default.

This is the only tool that touches Weaviate. See [Hybrid search pipeline](#hybrid-search-pipeline) for the full path.

---

## PII redaction

### Categories

Regex-only detection at `app/src/pii/patterns.py`:

| Category | Regex | Marker |
|---|---|---|
| `EMAIL` | `\b[\w.+-]+@[\w-]+\.[\w.-]+\b` | `<PII:EMAIL>` |
| `PHONE` | `(?:\+?\d{1,3}[\s-]?)?(?:\d[\s-]?){9,12}\d` | `<PII:PHONE>` |
| `AADHAAR` | `\b\d{4}\s?\d{4}\s?\d{4}\b` | `<PII:AADHAAR>` |
| `PAN` | `\b[A-Z]{5}\d{4}[A-Z]\b` | `<PII:PAN>` |

Names (PERSON) are explicitly NOT redacted because the assistant addresses the student by first name. There is no ML model for PII; everything is regex.

### Redaction algorithm

`PII.redact(text)` collects all hits across categories, sorts them right-to-left by start offset, and applies replacements in that order so earlier offsets remain valid. Returns `(redacted_text, [PIIHit])` where each hit has `(category, start, end, original)`.

### Where redaction runs

- **Input** (`pii_in` node): the user's raw message is redacted before being added to the LLM context. The original (un-redacted) text is preserved separately for persistence so the student's own message appears verbatim in their history.
- **Output**: the response is streamed to the client as tokens arrive from the LLM (see Streaming response), so there is no post-hoc masking opportunity. Any output-side PII risk has to be controlled at the model level (system prompt instructions, training-data hygiene).

### Metric

`pii_detections_total{category, surface}` increments per category per surface. Only `surface="input"` is populated in the current flow.

---

## Memory

Memory is split into short-term (recent turns of the active chat) and long-term (multi-chat preferences, struggles, goals).

### Short-term: session turns

- **Storage**: Redis LIST at `{student_id}:chat_session:{chat_id}`, newest-first via `LPUSH`.
- **Sliding window**: `LTRIM 0, max_turns_in_context - 1` (default 10 turns kept).
- **TTL**: `ttl_session` (3600s by default), reset on every push.
- **Fallback**: `Memory.session_recent()` returns the cache contents if present, otherwise loads the last N messages from Mongo and rehydrates the cache.
- **Replay**: `load_context` reverses the cache (oldest-first) before adding to the LLM message list.

### Long-term: extracted memory

After every turn, `Memory.extract_and_persist()` calls the LLM (via `get_structured_model()`, temperature 0) with the prompt at `app/src/memory/prompt.md` and the current user/assistant turn. The LLM returns zero or more candidates of shape:

```json
{
  "category": "preference | struggle | goal | schedule | milestone",
  "content": "...",
  "confidence": 0.0,
  "valid_until": "YYYY-MM-DD" | null
}
```

Only candidates with `confidence >= memory_confidence_threshold` (default 0.7) are written to `long_term_memory`. On any write, the Redis `lt_memory` cache for that student is invalidated.

When the next chat starts, `load_context` calls `Memory.lt_load(student_id)`, which returns memories sorted by confidence descending. The top 5 by confidence (truncated to 240 characters total) are rendered into the system prompt under `{memories_preview}`.

---

## Tracing

Every chat turn produces a trace in three places, each with a different level of detail.

### 1. In-graph (`state["tool_calls_trace"]`)

While the graph runs, the `tools` node appends one entry per tool invocation:

```json
[
  {
    "name": "get_upcoming_tests",
    "args": {},
    "result_preview": "{\"as_of_date\": \"2026-05-27\", \"tests\": [...]}"
  },
  {
    "name": "recommend_study_material",
    "args": {"query": "Algebra"},
    "result_preview": "{\"results\": [{\"title\": \"Algebra Basics Revision Notes\", ..."
  }
]
```

`result_preview` is truncated to `MAX_PREVIEW_CHARS = 240` characters. This trace lives in graph state for the duration of the turn and is then persisted as described below.

### 2. Persisted in `messages.tool_calls`

When `persist` writes the assistant MessageRecord, the trace is attached to its `tool_calls` field. Useful for showing "what tools did the agent call when it produced this reply" without joining another collection. See the assistant-message example in [Database models](#database-models).

### 3. Persisted in `traces` collection (deepest)

For every request, a separate row is written to the `traces` collection keyed by `request_id`. This is the full structured trace:

- `user_input`, `final_output`, `started_at`, `finished_at`, `total_latency_ms`
- `llm_calls`: list of every LLM hop with `model`, `input_messages` (full conversation as sent to the API), `output_content`, `output_tool_calls`, `token_usage {input, output, reasoning, total}`, `latency_ms`, `error`
- `tool_calls`: structured tool execution records
- `total_token_usage`: cumulative across all LLM hops (input + output + reasoning + total)
- `error`: top-level failure string if the graph crashed

Use `traces` for cost attribution (token usage per request), debugging (full input messages to reconstruct what the LLM actually saw), and replay (you can re-run a prompt by feeding the stored `input_messages` back to the LLM).

### Latency breakdown

Per-phase timings are tracked in state and emitted as `chat_latency_ms{phase}` histogram observations:

- `phase="llm"`: cumulative LLM wall-clock time across all `agent` node invocations
- `phase="tools"`: cumulative tool execution time
- `phase="pii"`: time spent in the input-side `pii_in` node
- `phase="total"`: total wall-clock for the whole graph

These same fields are stored in `EvalRecord.rules.latency_ms_*` for offline analysis.

### Request correlation

The middleware in `app/main.py` extracts or generates an `x-request-id` per HTTP request and binds it to the structured logger via ContextVars. Every log line from that request includes the request_id, and the response carries `x-request-id` back out. The `evals` row uses the same `request_id` as its primary key.

---

## Evaluation

Two layers of eval, both writing to the `evals` collection.

### Rule-based (every request)

`Eval.record()` is called synchronously from the chat route after every successful or failed request. It writes an EvalRecord with these fields under `rules`:

| Field | Type | Source |
|---|---|---|
| `tool_count` | int | Number of tools called this turn |
| `tool_names` | list[str] | Names of tools invoked, in order |
| `tool_correctness` | "expected" / "unexpected" / "none" | Heuristic regex-to-tool intent map. e.g. "test" or "exam" in the user message expects `get_upcoming_tests`; "weak" or "struggl" expects `get_my_topics`; etc. |
| `latency_ms_total` | int | End-to-end |
| `latency_ms_llm` | int | LLM wall-clock |
| `latency_ms_tools` | int | Tool execution wall-clock |
| `latency_ms_pii` | int | PII pass wall-clock |
| `pii_triggered_input` | bool | Any PII hit in user message |
| `pii_categories_input` | list[str] | Unique categories from input |
| `pii_triggered_output` | bool | Any PII hit in response (failure if true) |
| `pii_categories_output` | list[str] | Unique categories from output |
| `response_nonempty` | bool | Assistant produced text |
| `response_token_count` | int | Whitespace token count of the response |
| `error` | str / null | Backend error if the request failed |
| `rate_limited` | bool | True if the request was rejected by the limiter |

Failure metrics emitted by `Eval.record()`:

- `eval_rule_failures_total{check="tool_correctness"}` when correctness is "unexpected"
- `eval_rule_failures_total{check="response_nonempty"}` when the response is empty
- `eval_rule_failures_total{check="pii_in_output"}` when PII categories fire on the output surface

### LLM judge (sampled)

`Eval.judge_async()` is called fire-and-forget on a `eval_judge_sample_rate` fraction of successful requests (default 5%). It calls the LLM with a structured prompt that includes the user message, the assistant response, and the tool trace. The judge returns:

```python
{
  "answer_correctness": 0.0..1.0,
  "context_utilization": 0.0..1.0,
  "faithfulness": 0.0..1.0,
  "rationale": "...",
  "sampled": True,
  "judge_latency_ms": 1234
}
```

The judge row is merged into the existing EvalRecord by `request_id`. If the judge call fails, nothing is written and the rules row stands alone.

### EvalRecord shape

```python
class EvalRecord:
    request_id: str
    chat_id: str
    student_id: str
    timestamp: datetime
    rules: EvalRules
    judge: EvalJudge | None
```

Stored in `evals` collection. Queryable per student via `GET /evals`.

---

## Rate limiting

Per-student token bucket implemented in Redis via a Lua script.

- **Capacity**: `rate_limit_burst` tokens (default 10)
- **Refill**: `rate_limit_rps` tokens per second (default 10)
- **Key**: `ratelimit:{user_uuid}` (HASH with fields `tokens`, `ts`)
- **Routes guarded**: `POST /chat` and `POST /achat` via `Depends(rate_limited)` in the chat router.

The Lua script atomically reads current tokens, computes refill since last access, decrements one token if available, and writes back. Returns `[allowed, retry_after_ms]`. If Lua is unavailable (very rare), the limiter falls back to a cheap `INCR + EXPIRE` on a 1-second window.

When the limit is exceeded:
- Counter `rate_limited_total{route="/chat"}` increments
- The route raises a domain exception that maps to HTTP 429

The bucket key has `PEXPIRE` of `ceil(capacity * 1000 / refill) + 1000` ms so dormant students do not leave stale keys.

---

## Metrics

A single `prometheus_client.CollectorRegistry` lives in `app/src/metrics/metrics.py`. The `/metrics` route exposes it in Prometheus text format.

| Metric | Type | Labels | Increment site |
|---|---|---|---|
| `chat_requests_total` | Counter | `outcome` (success / error / rate_limited) | Chat route on every request, by outcome |
| `chat_latency_ms` | Histogram | `phase` (llm / tools / pii / total) | `agent_node` and `tool_node` per phase |
| `tool_calls_total` | Counter | `tool` (one of 4 tool names) | `tool_node` after each tool execution |
| `pii_detections_total` | Counter | `category` (EMAIL / PHONE / AADHAAR / PAN), `surface` (`input` only in current flow) | `pii_in` node |
| `eval_rule_failures_total` | Counter | `check` (tool_correctness / response_nonempty / pii_in_output) | `Eval.record()` |
| `rate_limited_total` | Counter | `route` (/chat or /achat) | Rate limiter dependency on reject |

Histogram buckets for `chat_latency_ms`: 10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000 ms.

Logs are structured JSON via `python-json-logger`. Per-request, the middleware binds `request_id` and `endpoint` into log context. Sensitive field values (email, name, password, jwt, authorization, token) are stripped to `<redacted>` even if accidentally passed as log fields.

---

## Database models

Eleven Mongo collections live in the `student_agent` database. The shapes below are taken from `db.<collection>.findOne()` against the live database, not from Pydantic source. Counts are from the current Mongo state.

### `students` (6 docs)

```json
{
  "_id": "d4760238-5bbb-4e18-8ebd-defd717ec93d",
  "student_id": "SDA728DEF6C",
  "name": "Arjun Sharma",
  "grade": 10,
  "board": "CBSE",
  "target_exam": "CBSE Board Exams 2027",
  "daily_study_time_minutes": 90,
  "strong_topics": ["Chemical Reactions and Equations", "Real Numbers", "Acids, Bases and Salts", "Pair of Linear Equations"],
  "weak_topics": ["Quadratic Equations", "Light - Reflection and Refraction", "Trigonometry", "Coordinate Geometry", "Arithmetic Progressions", "Algebra"],
  "subjects": ["English", "Hindi", "Mathematics", "Science", "Social Science"]
}
```

### `auth` (6 docs)

```json
{
  "_id": "d4760238-5bbb-4e18-8ebd-defd717ec93d",
  "student_id": "SDA728DEF6C",
  "email": "arjun@example.com",
  "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$...",
  "created_at": "2026-05-26T14:35:39.859296Z",
  "updated_at": "ISODate(2026-05-27T05:07:54.401Z)",
  "last_login_at": "ISODate(2026-05-27T05:07:54.401Z)"
}
```

`_id` matches the `students._id`. `password_hash` is Argon2id (via argon2-cffi). Per-student row, never returned through any API.

### `performance` (2 docs)

```json
{
  "_id": "f7613b44-6d29-4ece-88e3-9a9f6a1ccc8d",
  "student_id": "SDA728DEF6C",
  "subject_performance": [
    {"subject": "Mathematics", "overall_score_percentage": 52},
    {"subject": "Science", "overall_score_percentage": 63},
    {"subject": "English", "overall_score_percentage": 71},
    {"subject": "Social Science", "overall_score_percentage": 58},
    {"subject": "Hindi", "overall_score_percentage": 68}
  ]
}
```

One row per student. Read by `get_performance_history(subject)`.

### `upcoming_tests` (2 docs)

```json
{
  "_id": "954962a7-d750-4c6f-bd94-ddd28c9156cb",
  "student_id": "SDA728DEF6C",
  "upcoming_tests": [
    {"test_id": "T201", "subject": "Mathematics", "test_name": "Math Weekly Test", "date": "2026-05-28", "topics": ["Algebra", "Quadratic Equations"]},
    {"test_id": "T202", "subject": "Science", "test_name": "Science Unit Test - Physics", "date": "2026-06-03", "topics": ["Light - Reflection and Refraction", "Electricity"]}
  ]
}
```

One row per student. Read by `get_upcoming_tests()`.

### `study_materials` (25 docs)

```json
{
  "_id": "ObjectId('6a13d6e5f0fe991520eb213d')",
  "material_id": "M102",
  "topic": "Pair of Linear Equations",
  "title": "Linear Equations in Two Variables - Solved Examples",
  "board": "CBSE",
  "grade": 10,
  "content": "Linear Equations in Two Variables - solved-example walkthrough aligned to NCERT Class 10 Mathematics, Chapter 3..."
}
```

Read by `recommend_study_material(query)` via Weaviate hybrid search, hydrated back from Mongo by `_id`. Each Mongo doc has a mirror in Weaviate with a deterministic UUID derived from `material_id`.

### `chats` (19 docs)

```json
{
  "_id": "cfd2126d-2d27-43b5-83e7-4d038bddd928",
  "chat_id": "3e91151a-e9f3-4467-80f0-0276b048a6c3",
  "student_id": "S57A09C51F2",
  "started_at": "2026-05-26T23:07:34.471700Z",
  "last_active_at": "ISODate(2026-05-26T23:07:42.374Z)",
  "status": "active",
  "title": "help me revise compound interest with examples"
}
```

One row per chat. `title` is auto-set from the first user message (truncated to 80 chars). `status` is `"active"` or `"ended"`. `last_active_at` is bumped on every chat turn.

### `messages` (42 docs)

User message:
```json
{
  "_id": "026c56e4-a9c6-4c52-9532-3f323f5e10a4",
  "message_id": "8a4c5734-02b6-473c-b3f4-ac3d26181535",
  "chat_id": "3e91151a-e9f3-4467-80f0-0276b048a6c3",
  "student_id": "S57A09C51F2",
  "role": "user",
  "content": "help me revise compound interest with examples",
  "tool_calls": null,
  "tool_call_id": null,
  "token_usage": {"input": 0, "output": 0},
  "latency_ms": null,
  "created_at": "2026-05-26T23:07:42.372243Z"
}
```

Assistant message with persisted tool trace:
```json
{
  "_id": "a950cd32-45cf-4efa-87e5-37e89ab02ad5",
  "message_id": "5180311b-69a2-4a2d-bc4b-c2bd72235b71",
  "chat_id": "3e91151a-e9f3-4467-80f0-0276b048a6c3",
  "student_id": "S57A09C51F2",
  "role": "assistant",
  "content": "Here's a quick rundown of compound interest...",
  "tool_calls": [
    {
      "name": "recommend_study_material",
      "args": {"query": "Compound Interest revision"},
      "result_preview": "{\"results\": [{\"title\": \"Compound Interest Yearly and Half-Yearly\", \"topic\": \"Compound Interest\", \"content_excerpt\": \"...\"}"
    }
  ],
  "tool_call_id": null,
  "token_usage": {"input": 0, "output": 0},
  "latency_ms": null,
  "created_at": "2026-05-26T23:07:42.372243Z"
}
```

Only `role: "user"` and the final `role: "assistant"` rows are persisted per turn. Intermediate tool messages stay in graph state. `tool_calls.result_preview` is truncated to 240 chars per entry.

### `long_term_memory` (69 docs)

```json
{
  "_id": "9cd5db5d-0b29-4371-95a8-56e04ea72299",
  "memory_id": "93931616-3eb1-4176-899b-7ffc20c0fe4e",
  "student_id": "SDA728DEF6C",
  "category": "struggle",
  "content": "Struggles with Algebra.",
  "confidence": 0.9,
  "created_at": "2026-05-26T21:17:39.727959Z",
  "valid_until": null,
  "source_message_id": null
}
```

`category` is one of `preference`, `struggle`, `goal`, `schedule`, `milestone`. Only candidates with `confidence >= 0.7` (the configured threshold) are written. `valid_until` is null for indefinite memories or an ISO date for memories that auto-expire (e.g. test-prep schedules).

### `evals` (73 docs)

Rule-only (every request):
```json
{
  "_id": "e495fb13-f5b3-4a53-93d1-919aff86c9cf",
  "request_id": "1f64dc2b828f4034ae1cb34c9a150bf4",
  "chat_id": "b3848587-9bf4-4050-b761-34695e101c8f",
  "student_id": "SDA728DEF6C",
  "timestamp": "2026-05-26T21:11:45.748749Z",
  "rules": {
    "tool_count": 0,
    "tool_names": [],
    "tool_correctness": "none",
    "latency_ms_total": 2207,
    "latency_ms_llm": 0,
    "latency_ms_tools": 0,
    "latency_ms_pii": 0,
    "pii_triggered_input": false,
    "pii_categories_input": [],
    "pii_triggered_output": false,
    "pii_categories_output": [],
    "response_nonempty": false,
    "response_token_count": 0,
    "error": null,
    "rate_limited": false
  },
  "judge": null
}
```

With LLM judge populated (5% sample):
```json
{
  "judge": {
    "sampled": true,
    "answer_correctness": 0.0,
    "context_utilization": 0.0,
    "faithfulness": 0.0,
    "rationale": "The assistant incorrectly guessed the user's name without any prior context or tool data...",
    "judge_latency_ms": 3034
  }
}
```

`request_id` is the unique key; the judge row is patched in by `Eval.judge_async` after the rules row is written.

### `traces` (39 docs)

The deepest record of one chat turn. One row per `request_id`.

```json
{
  "_id": "120f5c4eb54e495c8090f05e48c44aa5",
  "request_id": "120f5c4eb54e495c8090f05e48c44aa5",
  "student_id": "SDA728DEF6C",
  "chat_id": "7537d599-9c9b-4ae7-916f-73c6e7e1c830",
  "user_message_id": "f321f759-c90f-4f96-b967-360264dd921f",
  "assistant_message_id": "73e7c042-b62f-46e9-b916-b698d85eed9e",
  "started_at": "2026-05-27T01:05:23.819482Z",
  "finished_at": "2026-05-27T01:05:26.041207Z",
  "total_latency_ms": 2221,
  "user_input": "hi",
  "final_output": "Hello Arjun! How can I help you with your studies today?",
  "llm_calls": [
    {
      "index": 0,
      "started_at": "2026-05-27T01:05:23.823496Z",
      "latency_ms": 2210,
      "model": "gpt-4.1-mini",
      "input_messages": [{"role": "system", "content": "You are a personal learning assistant..."}, {"role": "human", "content": "hi"}],
      "output_content": "Hello Arjun! How can I help you with your studies today?",
      "output_tool_calls": [],
      "token_usage": {"input": 1289, "output": 33, "reasoning": 0, "total": 1322},
      "error": null
    }
  ],
  "tool_calls": [],
  "total_token_usage": {"input": 1289, "output": 33, "reasoning": 0, "total": 1322},
  "error": null
}
```

`traces` is richer than `messages.tool_calls` (which is just the trimmed action trace). It captures every LLM call's full input messages, output content, tool calls emitted, token usage broken down (input / output / reasoning / total), and per-call latency. Used for offline debugging and cost attribution.

### `sessions` (0 docs)

Currently unused. Reserved for future per-session metadata that does not belong in `chats` or Redis.

---

## Collections and indexes

Created idempotently at app startup by `ensure_indexes()` in `app/src/database/client.py`.

| Collection | Indexes |
|---|---|
| `students` | `student_id` unique |
| `auth` | `email` unique, `student_id` unique |
| `performance` | `student_id` unique |
| `upcoming_tests` | `student_id` unique |
| `study_materials` | `material_id` unique, compound `(board, grade, topic)` |
| `chats` | `chat_id` unique, compound `(student_id, last_active_at desc)` |
| `messages` | compound `(chat_id, created_at asc)`, compound `(student_id, created_at desc)`, `message_id` unique |
| `long_term_memory` | `memory_id` unique, three compound indexes on `student_id` with `created_at desc`, `valid_until`, and `category` |
| `evals` | `request_id` unique, compound `(student_id, timestamp desc)`, single on `judge.sampled` |

---

## Sample data and what changed

The assignment shipped four sample files. The current Mongo state adds a few fields to two of them. The other two are unchanged in shape.

### Fields added per collection

| Collection | Original assignment fields | Fields added in current Mongo |
|---|---|---|
| `students` | `student_id, name, grade, board, target_exam, daily_study_time_minutes, strong_topics, weak_topics` | `subjects: list[str]` |
| `performance` | `student_id, subject_performance: [{subject, overall_score_percentage}]` | none |
| `upcoming_tests` | `student_id, upcoming_tests: [{test_id, subject, test_name, date, topics}]` | none |
| `study_materials` | `material_id, topic, title` | `board: "CBSE"|"ICSE"`, `grade: int`, `content: str` |

### Why each addition

- **`students.subjects`**: the agent's `get_performance_history(subject)` tool needs to know which subjects a student is enrolled in. Live Mongo example for `SDA728DEF6C`: `["English", "Hindi", "Mathematics", "Science", "Social Science"]`.
- **`study_materials.board` + `study_materials.grade`**: hybrid search filters by both at query time so a CBSE grade-10 student never sees ICSE results.
- **`study_materials.content`**: the original sample had only title and topic. The agent now reads the full chapter outline from `content` (NCERT chapter for CBSE materials, CISCE syllabus for ICSE materials) so it can quote real subtopic numbers, formulas, and examples instead of just citing titles.

### Live Mongo example: `study_materials`

Showing one document as it exists in Mongo right now (`material_id: M103`, content truncated for display):

```json
{
  "_id": "ObjectId('6a13d6e5f0fe991520eb213d')",
  "material_id": "M103",
  "topic": "Quadratic Equations",
  "title": "Quadratic Equations Concept Video",
  "board": "CBSE",
  "grade": 10,
  "content": "Concept-video walkthrough aligned to NCERT Class 10 Mathematics, Chapter 4 (Quadratic Equations), Reprint 2026-27. Covers every subtopic of the chapter end-to-end. 4.1 Introduction... [~700 words]"
}
```

### Live Mongo example: `students`

```json
{
  "_id": "d4760238-5bbb-4e18-8ebd-defd717ec93d",
  "student_id": "SDA728DEF6C",
  "name": "Arjun Sharma",
  "grade": 10,
  "board": "CBSE",
  "target_exam": "CBSE Board Exams 2027",
  "daily_study_time_minutes": 90,
  "strong_topics": ["Chemical Reactions and Equations", "Real Numbers", "Acids, Bases and Salts", "Pair of Linear Equations"],
  "weak_topics": ["Quadratic Equations", "Light - Reflection and Refraction", "Trigonometry", "Coordinate Geometry", "Arithmetic Progressions", "Algebra"],
  "subjects": ["English", "Hindi", "Mathematics", "Science", "Social Science"]
}
```

`subjects` is the new field; everything else matches the original assignment schema.

---

## Hybrid search pipeline

The `recommend_study_material` tool delegates to `VectorService.hybrid_search()`. Pipeline:

```
user query (string)
  |
  v
EmbeddingClient.embed_one(query)
  |  (Azure OpenAI text-embedding-3-small; cache hit on sha256 of normalized text)
  v
1536-dim query vector
  |
  v
Weaviate.collections.StudyMaterials.query.hybrid(
    query=text,
    vector=vector,
    fusion_type=HybridFusion.RANKED,    # Reciprocal Rank Fusion
    filters=board==X AND grade==Y,
    limit=fetch_limit                    # top_k or RERANK_POOL=20 if reranking
)
  |
  v
list of WeaviateObject (each has material_id, mongo_doc_id, score, content)
  |
  v
[optional] RerankerClient.rerank(query, [title + content], top_n=top_k)
  |  (Jina jina-reranker-v3 over HTTP; identity ordering if API key missing)
  v
list of reordered indices
  |
  v
Data._hydrate_materials_by_id(mongo_doc_ids)
  |  (Mongo lookup by _id; returns full StudyMaterial including content)
  v
list[SearchHit]    (StudyMaterial + score)
```

Key properties:

- **Hybrid fusion**: Weaviate combines BM25 and dense vector rankings via RRF, no manual alpha tuning needed. `HYBRID_ALPHA` is reserved in config but not currently passed (RANKED fusion does not take it).
- **Per-student filters**: `board` and `grade` come from the student's profile, never from the LLM. This means a CBSE grade-10 student cannot retrieve ICSE materials.
- **Stable upsert by UUID**: every material gets a deterministic UUID via `sha1(material_id)[:32]`. Re-indexing the same material replaces the Weaviate object rather than creating duplicates.
- **Embedding cache**: query text is whitespace-normalized and sha256-hashed; embeddings are reused across queries from any student. Cache TTL is 24 hours.
- **Reranker toggle**: `USE_RERANKER` is the single switch. `.env.example` ships it as `false` so a first-time clone runs without needing a Jina API key. When set to `true` (with `RERANKER_API_KEY` populated), the tool fetches a larger pool of 20 hits from Weaviate, passes them through Jina, and keeps the requested top-k. If the API call fails for any reason, the code falls back to the original Weaviate order rather than erroring the request.

---

## API endpoints

All routes are JWT-protected except auth, health, and metrics. The chat routes are additionally rate-limited.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness + Redis ping |
| GET | `/metrics` | none | Prometheus text exposition |
| POST | `/auth/signup` | none | Create student, return JWT |
| POST | `/auth/login` | none | Email + password to JWT |
| POST | `/auth/refresh` | JWT | Refresh an existing JWT |
| GET | `/profile` | JWT | Current student's profile |
| PUT | `/profile` | JWT | Update own profile |
| POST | `/chat` | JWT + rate-limited | Blocking chat turn, returns ChatResponse JSON |
| POST | `/achat` | JWT + rate-limited | Streaming chat via SSE |
| POST | `/chat/{chat_id}/end` | JWT | Mark chat as ended |
| DELETE | `/chat/{chat_id}` | JWT | Delete chat row + all messages + Redis session |
| GET | `/chat/history` | JWT | List current student's chats |
| GET | `/chat/{chat_id}/messages` | JWT | Paginated messages for one chat |
| GET | `/study-materials/{material_id}` | JWT | Fetch one StudyMaterial by id |
| GET | `/evals` | JWT | List the current student's eval rows |

---

## Prerequisites

- Docker Desktop (or Docker Engine 24+) with Compose v2
- Python 3.11 if you want to run the backend or Streamlit outside Docker
- An Azure OpenAI deployment with:
  - A chat model deployment named `gpt-4.1-mini` (or another non-reasoning model)
  - An embedding deployment named `text-embedding-3-small`
- Optional: a Jina API key for `jina-reranker-v3`
- At least 4 GB free RAM for the four backing services + the app

---

## Setup and usage

### 1. Clone the repo

```bash
git clone https://github.com/<your-org>/student-agent.git
cd student-agent
```

### 2. Configure secrets

```bash
cp .env.example .env
```

Open `.env` and set:

```
# Azure OpenAI
AZURE_OPENAI_API_KEY=<your key>
AZURE_OPENAI_ENDPOINT=https://<your-resource>.services.ai.azure.com
AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4.1-mini
AZURE_OPENAI_CHAT_MODEL=gpt-4.1-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
AZURE_OPENAI_EMBEDDING_MODEL=text-embedding-3-small

# JWT secret (change in production)
JWT_SECRET=<a-long-random-string>

# Reranker. Set USE_RERANKER=true once RERANKER_API_KEY is populated to
# enable Jina reranking on every recommend_study_material call. Defaults
# to false in .env.example so a first-time clone runs without a Jina key.
USE_RERANKER=true
RERANKER_API_KEY=<your jina api key>
```

Tunable LLM settings (recommended for tool-calling reliability):

```
LLM_TEMPERATURE=0.2
LLM_TOP_P=1.0
```

### 3. Bring up the stack

A single docker-compose brings up Mongo, Redis, Weaviate, the FastAPI backend, and the Streamlit frontend together:

```bash
docker compose up -d --build
```

Verify all four services are healthy:

```bash
docker compose ps
curl http://localhost:8000/health
```

Container ports:

```
sa_mongo     :27017
sa_redis     :6379
sa_weaviate  :8080 (HTTP) + :50051 (gRPC)
sa_app       :8000 (FastAPI backend) + :8501 (Streamlit frontend)
```

### 4. Create a student and (optionally) seed data

The lifespan hook calls `ensure_indexes()` automatically when the backend boots. The repo does not ship seed data; the easiest path for a first run is to sign up via the API (covered in step 7) and let the agent populate `chats`, `messages`, `long_term_memory`, `evals`, and `traces` as you use it.

If you have local seed files (`students.json`, `performance_history.json`, `upcoming_tests.json`, `study_materials.json`) that match the shapes in [Database models](#database-models), you can bulk-load them with `mongoimport`. Example for one collection:

```bash
docker cp study_materials.json sa_mongo:/tmp/study_materials.json
docker exec sa_mongo bash -c "mongoimport --db student_agent --collection study_materials --jsonArray --file /tmp/study_materials.json"
```

After loading `study_materials`, trigger the Weaviate reindexer (next step) so the new rows are searchable.

### 5. Index the catalog into Weaviate

The lifespan hook also queues a one-shot "missing only" reindex job at startup. Watch the logs:

```bash
docker compose logs -f sa_app | grep startup_index
```

When you see `startup_index_queued ... queued=<N>` followed by a job completion line, the catalog is searchable.

### 6. Use the app

Open the Streamlit UI:

```
http://localhost:8501
```

Sign up with email + password + name + grade + board. Then chat. Try:

- "I have a Maths test coming up. Help me prepare."
- "What am I weak in?"
- "How am I doing in Science?"
- "Show me notes on the discriminant of a quadratic"

### 7. Direct API access (without UI)

```bash
# Sign up
curl -X POST http://localhost:8000/auth/signup \
  -H 'content-type: application/json' \
  -d '{"email":"arjun@test.com","password":"longpassword","name":"Arjun","grade":10,"board":"CBSE","target_exam":"CBSE Board Exams 2027","daily_study_time_minutes":90}'

# Save the access_token from the response, then:
TOKEN=<paste>

# Chat
curl -X POST http://localhost:8000/chat \
  -H "authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"message": "I have a Maths test coming up. Help me prepare."}'
```

### Troubleshooting

- **`sa_app` fails to start with "port 8000 or 8501 already in use"**: another process is bound to that port on the host. Find it with `lsof -nP -iTCP:<port> -sTCP:LISTEN`, stop it, or change the published port in `docker-compose.yml`.
- **Weaviate search returns empty for known queries**: the startup auto-indexer only indexes "missing" materials. If you change material content, you must reindex explicitly. The Cache invalidation does not extend to Weaviate.
- **`/chat` returns 429**: the rate limiter is configured to 10 requests per second per student by default. Wait a moment or raise `RATE_LIMIT_RPS` and `RATE_LIMIT_BURST` in `.env`.

---

## Way forward

Concrete next steps planned for this codebase.

### Long-term memory sub-agent

Today, long-term memory is extracted by a single LLM call that runs after every turn and emits zero or more candidates above the confidence threshold. This is rigid: there is no logic to update existing memories, expire them, or merge near-duplicates.

The plan is to introduce a dedicated **memory sub-agent** that the main agent can call as a tool. The sub-agent will have its own narrower toolset:

- `add_memory(category, content, confidence, valid_until)`
- `update_memory(memory_id, fields)`
- `expire_memory(memory_id)`
- `find_memory(query)` to search before adding (dedup)

The main agent will explicitly invoke the memory sub-agent when it notices new student-state changes (e.g. the student says "I am finding Quadratic Equations easier now" should trigger an update of the existing struggle entry, not an add). This moves memory management from an after-the-fact LLM extraction to an in-loop deliberate action.

### More tools

The 4 current tools cover profile-anchored questions (topics, performance, tests, materials). Can add more tool such as below:

- **`get_chapter_summary(board, grade, subject, chapter)`**: structured chapter outline lookup against a separate `chapters` collection (seeded from NCERT / CISCE indexes). Lighter than full material content; useful for the agent to plan a study session.
- **`schedule_study_block(date, duration_minutes, topic)`**: write a planned study slot to a `study_schedule` collection. Lets the agent build a personalised plan rather than only suggesting one.
- **`record_self_assessment(topic, confidence)`**: the student reports how confident they feel; the agent persists it to inform future weak/strong adjustments.

### Search tool for syllabus explanations

The current `recommend_study_material` returns curated chapter content from our 25-material catalog. For deeper queries (a worked example, an alternate method, a related real-world application), the agent has no way to fetch supplementary content.

A **search tool** is planned that calls a web search API restricted to a curated domain allowlist (NCERT, CISCE, Khan Academy, Vedantu). The tool will return short snippets with source attribution and the agent will summarize them with explicit citation. This avoids hallucination by grounding every external claim in a fetched snippet.

### Output PII masking under streaming

The current streaming design means there is no post-hoc opportunity to mask PII in the response: tokens are already on the wire. If output masking becomes a hard requirement, the options are (a) buffer the full response server-side before forwarding (loses the streaming experience), or (b) run a token-level streaming PII detector that holds back suspicious chunks until the surrounding context confirms or rejects a hit. Either is a meaningful redesign, not a flag flip.
---
