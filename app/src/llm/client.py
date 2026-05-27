# AzureChatOpenAI factory; auto-handles o-series reasoning models.
# Shared by agent (chat), memory (extraction), eval (judge).
#
# IMPORTANT: o-series (o1/o3/o4) only accept temperature=1.0 and top_p=1.0.
# Skipping these fields is NOT enough because LangChain's AzureChatOpenAI has
# its own Pydantic default of temperature=0.7 — that default gets sent to the
# API and the request is rejected with "temperature 0.7 not supported".
# We must EXPLICITLY pass 1.0 to override LangChain's default.
from functools import lru_cache

from langchain_openai import AzureChatOpenAI

from app.config import get_settings

_REASONING_PREFIXES = ("o1", "o3", "o4")


def _is_reasoning(deployment: str) -> bool:
    name = deployment.lower()
    return any(name.startswith(p) for p in _REASONING_PREFIXES)


@lru_cache
def get_chat_model() -> AzureChatOpenAI:
    s = get_settings()
    kw: dict = {
        "api_key": s.azure_openai_api_key,
        "api_version": s.azure_openai_api_version,
        "azure_endpoint": s.azure_openai_endpoint,
        "azure_deployment": s.azure_openai_chat_deployment,
        "model": s.azure_openai_chat_model,
        "timeout": s.llm_timeout_seconds,
        "max_retries": s.llm_max_retries,
    }
    if _is_reasoning(s.azure_openai_chat_deployment):
        # o-series: must explicitly pin to the only accepted values
        kw["temperature"] = 1.0
        kw["top_p"] = 1.0
        kw["max_completion_tokens"] = s.llm_max_tokens
    else:
        kw["temperature"] = s.llm_temperature
        kw["top_p"] = s.llm_top_p
        kw["max_tokens"] = s.llm_max_tokens
    return AzureChatOpenAI(**kw)


@lru_cache
def get_structured_model() -> AzureChatOpenAI:
    """Lower-output cap variant used by memory writer and eval judge."""
    s = get_settings()
    kw: dict = {
        "api_key": s.azure_openai_api_key,
        "api_version": s.azure_openai_api_version,
        "azure_endpoint": s.azure_openai_endpoint,
        "azure_deployment": s.azure_openai_chat_deployment,
        "model": s.azure_openai_chat_model,
        "timeout": s.llm_timeout_seconds,
        "max_retries": s.llm_max_retries,
    }
    if _is_reasoning(s.azure_openai_chat_deployment):
        kw["temperature"] = 1.0
        kw["top_p"] = 1.0
        # o-series eats hundreds of reasoning tokens before output
        kw["max_completion_tokens"] = 2048
    else:
        kw["temperature"] = 0.0
        kw["max_tokens"] = 512
    return AzureChatOpenAI(**kw)
