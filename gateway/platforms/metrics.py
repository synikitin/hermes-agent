"""Prometheus metrics for the OpenAI-compatible API server platform.

Exposed at :8642/metrics by api_server.py when prometheus_client is installed.
Metrics are intentionally narrowly scoped to the Responses API path because
that is the only endpoint with a known production regression history (the
conversation-history doubling bug — see ``responses_history_length`` below).

Prometheus_client is an optional dependency. When unavailable, all helpers
degrade to no-ops so the gateway still starts.
"""

from __future__ import annotations

from typing import Optional

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _Noop:
        def labels(self, *_, **__):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def dec(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

    Counter = Gauge = Histogram = lambda *a, **kw: _Noop()  # type: ignore[assignment]

    def generate_latest() -> bytes:  # type: ignore[no-redef]
        return b""


# ---- Streaming lifecycle ---------------------------------------------------

responses_streaming_active = Gauge(
    "hermes_responses_streaming_active",
    "Currently open Responses-API SSE streams",
)

responses_first_token_seconds = Histogram(
    "hermes_responses_first_token_seconds",
    "Time from request receipt to first token emitted on Responses-API SSE",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

responses_total_duration_seconds = Histogram(
    "hermes_responses_total_duration_seconds",
    "End-to-end Responses-API request duration",
    ["status"],  # completed | failed | aborted
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

responses_tokens_emitted_total = Counter(
    "hermes_responses_tokens_emitted_total",
    "SSE token deltas emitted by Responses-API streams",
    ["model"],
)

responses_tool_calls_total = Counter(
    "hermes_responses_tool_calls_total",
    "Tool calls executed during Responses-API turns",
    ["tool_name"],
)

# ---- Doubling-regression canary -------------------------------------------
#
# Stores the length of conversation_history that gets persisted at the end of
# every store=True turn. Under healthy operation this grows linearly with turn
# count (~2*turns + 1). The historical doubling bug doubled it on every turn,
# so a Histogram with exponentially-spaced buckets exposes the regression as
# a fast climb into the upper buckets after only a handful of turns.

responses_history_length = Histogram(
    "hermes_responses_history_length",
    "Stored conversation history length per turn (doubling-regression canary)",
    ["status"],  # store | skip
    buckets=[2, 5, 10, 25, 50, 100, 250, 500, 1000, 5000],
)


def render() -> bytes:
    """Render the current Prometheus metrics snapshot for /metrics."""
    return generate_latest()


def content_type() -> str:
    """Content-Type header value for /metrics responses."""
    return CONTENT_TYPE_LATEST


__all__ = [
    "PROMETHEUS_AVAILABLE",
    "content_type",
    "render",
    "responses_first_token_seconds",
    "responses_history_length",
    "responses_streaming_active",
    "responses_tokens_emitted_total",
    "responses_tool_calls_total",
    "responses_total_duration_seconds",
]
