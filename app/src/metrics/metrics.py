# Prometheus counters + histograms shared by the whole monolith.
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

# A single registry so all metric definitions land in one place. Routes,
# nodes, and the rate limiter import these by name.
REGISTRY = CollectorRegistry()

CHAT_REQUESTS_TOTAL = Counter(
    "chat_requests_total",
    "Total /agent/chat calls by outcome.",
    ["outcome"],
    registry=REGISTRY,
)

CHAT_LATENCY_MS = Histogram(
    "chat_latency_ms",
    "End-to-end and per-phase latency for /agent/chat (ms).",
    ["phase"],
    buckets=(10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000),
    registry=REGISTRY,
)

TOOL_CALLS_TOTAL = Counter(
    "tool_calls_total",
    "LLM tool invocations by tool name.",
    ["tool"],
    registry=REGISTRY,
)

PII_DETECTIONS_TOTAL = Counter(
    "pii_detections_total",
    "PII detections by category and surface (input|output).",
    ["category", "surface"],
    registry=REGISTRY,
)

EVAL_RULE_FAILURES_TOTAL = Counter(
    "eval_rule_failures_total",
    "Rule-based eval check failures.",
    ["check"],
    registry=REGISTRY,
)

RATE_LIMITED_TOTAL = Counter(
    "rate_limited_total",
    "Requests rejected by the rate limiter.",
    ["route"],
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
