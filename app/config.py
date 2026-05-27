# Pydantic Settings: single env-driven config for the whole monolith.
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"

    jwt_secret: str = Field(default="change-me", alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_expiration_minutes: int = Field(default=60, alias="JWT_EXPIRATION_MINUTES")

    mongodb_uri: str = Field(default="mongodb://localhost:27017", alias="MONGODB_URI")
    mongodb_database: str = Field(default="student_agent", alias="MONGODB_DATABASE")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    weaviate_url: str = Field(default="http://localhost:8080", alias="WEAVIATE_URL")
    weaviate_grpc_port: int = Field(default=50051, alias="WEAVIATE_GRPC_PORT")
    weaviate_api_key: str = Field(default="", alias="WEAVIATE_API_KEY")
    weaviate_collection_study_materials: str = Field(
        default="StudyMaterials", alias="WEAVIATE_COLLECTION_STUDY_MATERIALS"
    )

    ttl_profile: int = Field(default=900, alias="CACHE_TTL_PROFILE_SECONDS")
    ttl_performance: int = Field(default=600, alias="CACHE_TTL_PERFORMANCE_SECONDS")
    ttl_tests: int = Field(default=300, alias="CACHE_TTL_TESTS_SECONDS")
    ttl_session: int = Field(default=3600, alias="CACHE_TTL_SESSION_SECONDS")
    ttl_memory: int = Field(default=1800, alias="CACHE_TTL_MEMORY_SECONDS")
    ttl_embedding: int = Field(default=86400, alias="CACHE_TTL_EMBEDDING_SECONDS")

    azure_openai_api_key: str = Field(default="", alias="AZURE_OPENAI_API_KEY")
    azure_openai_endpoint: str = Field(default="", alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_version: str = Field(
        default="2024-12-01-preview", alias="AZURE_OPENAI_API_VERSION"
    )
    azure_openai_chat_deployment: str = Field(
        default="o4-mini", alias="AZURE_OPENAI_CHAT_DEPLOYMENT"
    )
    azure_openai_chat_model: str = Field(
        default="o4-mini", alias="AZURE_OPENAI_CHAT_MODEL"
    )
    azure_openai_embedding_deployment: str = Field(
        default="text-embedding-3-small", alias="AZURE_OPENAI_EMBEDDING_DEPLOYMENT"
    )

    llm_max_tokens: int = Field(default=2048, alias="LLM_MAX_TOKENS")
    llm_timeout_seconds: int = Field(default=60, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")
    llm_temperature: float = Field(default=1.0, alias="LLM_TEMPERATURE")
    llm_top_p: float = Field(default=1.0, alias="LLM_TOP_P")

    use_reranker: bool = Field(default=False, alias="USE_RERANKER")
    reranker_model: str = Field(default="jina-reranker-v3", alias="RERANKER_MODEL")
    reranker_url: str = Field(
        default="https://api.jina.ai/v1/rerank", alias="RERANKER_URL"
    )
    reranker_api_key: str = Field(default="", alias="RERANKER_API_KEY")

    max_turns_in_context: int = Field(default=10, alias="MAX_TURNS_IN_CONTEXT")
    memory_confidence_threshold: float = Field(
        default=0.7, alias="MEMORY_CONFIDENCE_THRESHOLD"
    )

    eval_judge_sample_rate: float = Field(
        default=0.05, alias="EVAL_JUDGE_SAMPLE_RATE"
    )

    rate_limit_rps: int = Field(default=10, alias="RATE_LIMIT_RPS")
    rate_limit_burst: int = Field(default=10, alias="RATE_LIMIT_BURST")


@lru_cache
def get_settings() -> Settings:
    return Settings()
