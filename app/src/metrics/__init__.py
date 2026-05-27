# Public API of the metrics module: shared counters/histograms + render().
from app.src.metrics.metrics import (
    CHAT_LATENCY_MS,
    CHAT_REQUESTS_TOTAL,
    EVAL_RULE_FAILURES_TOTAL,
    PII_DETECTIONS_TOTAL,
    RATE_LIMITED_TOTAL,
    TOOL_CALLS_TOTAL,
    render,
)

__all__ = [
    "CHAT_REQUESTS_TOTAL",
    "CHAT_LATENCY_MS",
    "TOOL_CALLS_TOTAL",
    "PII_DETECTIONS_TOTAL",
    "EVAL_RULE_FAILURES_TOTAL",
    "RATE_LIMITED_TOTAL",
    "render",
]
