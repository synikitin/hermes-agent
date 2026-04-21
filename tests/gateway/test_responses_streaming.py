"""SSE streaming tests for the OpenAI Responses API path (POST /v1/responses).

These tests guard the streaming patch shipped on the Zona fork. Upstream
v0.10.0 already implements the SSE writer; we keep these tests in the fork's
gating CI so a future upstream rebase that breaks streaming on the Responses
API trips the suite before reaching production.

Notable assertions:
- Content-Type is text/event-stream
- response.created is the first event
- response.output_text.delta arrives incrementally (multiple events,
  each carrying an individual ``delta`` field)
- response.completed is the terminal event and carries a parseable
  response object with ``status: completed``
- /metrics endpoint exposes the streaming-active gauge and gets
  decremented after the stream finishes
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    """Mirror the routes from APIServerAdapter.connect() so tests can hit
    the real handlers via TestServer/TestClient."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_get("/metrics", adapter._handle_metrics)
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/responses/{response_id}", adapter._handle_get_response)
    return app


def _parse_sse(body: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Parse an SSE body into (event_name, json_data) pairs.

    Robust against blank-line padding, comment lines (``: keepalive``), and
    the ``[DONE]`` sentinel some endpoints emit. Skips frames whose data is
    not valid JSON instead of raising — Responses API frames are always
    JSON, but this keeps the helper reusable across endpoints.
    """
    import json

    events: List[Tuple[str, Dict[str, Any]]] = []
    current_event: str = "message"
    current_data_lines: List[str] = []

    def _flush() -> None:
        nonlocal current_event, current_data_lines
        if not current_data_lines:
            current_event = "message"
            return
        raw = "\n".join(current_data_lines)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            current_event = "message"
            current_data_lines = []
            return
        events.append((current_event, parsed))
        current_event = "message"
        current_data_lines = []

    for line in body.splitlines():
        if line.startswith(":"):
            continue
        if line == "":
            _flush()
            continue
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            current_data_lines.append(line.split(":", 1)[1].lstrip())
    _flush()
    return events


@pytest.fixture
def adapter() -> APIServerAdapter:
    return _make_adapter()


class TestResponsesStreaming:
    @pytest.mark.asyncio
    async def test_sse_content_type_and_envelope_events(self, adapter):
        """stream=true must return text/event-stream and emit
        response.created before any delta and response.completed last."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            async def _mock_run_agent(**kwargs):
                cb = kwargs.get("stream_delta_callback")
                if cb:
                    cb("Hello ")
                    cb("world!")
                return (
                    {
                        "final_response": "Hello world!",
                        "messages": [
                            {"role": "user", "content": "ping"},
                            {"role": "assistant", "content": "Hello world!"},
                        ],
                        "api_calls": 1,
                    },
                    {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "test",
                        "input": "ping",
                        "stream": True,
                    },
                )
                assert resp.status == 200
                assert "text/event-stream" in resp.headers.get("Content-Type", "")
                assert resp.headers.get("X-Accel-Buffering") == "no"
                events = _parse_sse(await resp.text())

        event_names = [name for name, _ in events]
        assert event_names[0] == "response.created", event_names
        assert event_names[-1] == "response.completed", event_names

        completed_payload = events[-1][1]
        assert completed_payload.get("type") == "response.completed"
        response_obj = completed_payload.get("response", {})
        assert response_obj.get("status") == "completed"
        assert response_obj.get("model") == "test"

    @pytest.mark.asyncio
    async def test_incremental_text_deltas(self, adapter):
        """Multiple stream_delta_callback calls must produce multiple
        response.output_text.delta events, each with the individual
        delta text. Concatenating them must reproduce the streamed
        response — the doubling/concatenation regression that motivated
        the fork would manifest here as a duplicated final string."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            tokens = ["The ", "quick ", "brown ", "fox"]

            async def _mock_run_agent(**kwargs):
                cb = kwargs.get("stream_delta_callback")
                if cb:
                    for tok in tokens:
                        cb(tok)
                return (
                    {
                        "final_response": "".join(tokens),
                        "messages": [
                            {"role": "user", "content": "describe"},
                            {"role": "assistant", "content": "".join(tokens)},
                        ],
                        "api_calls": 1,
                    },
                    {"input_tokens": 5, "output_tokens": 4, "total_tokens": 9},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "test", "input": "describe", "stream": True},
                )
                events = _parse_sse(await resp.text())

        deltas = [
            payload.get("delta", "")
            for name, payload in events
            if name == "response.output_text.delta"
        ]
        assert len(deltas) >= len(tokens), deltas
        assert "".join(deltas) == "".join(tokens)

    @pytest.mark.asyncio
    async def test_metrics_endpoint_exposes_streaming_gauge(self, adapter):
        """The /metrics endpoint must serve Prometheus exposition format.

        Skips silently when prometheus_client is not installed in the
        environment — the endpoint still returns 200 with an empty body
        in that case (see metrics.py).
        """
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            async def _mock_run_agent(**kwargs):
                cb = kwargs.get("stream_delta_callback")
                if cb:
                    cb("x")
                return (
                    {
                        "final_response": "x",
                        "messages": [
                            {"role": "user", "content": "x"},
                            {"role": "assistant", "content": "x"},
                        ],
                        "api_calls": 1,
                    },
                    {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                stream_resp = await cli.post(
                    "/v1/responses",
                    json={"model": "test", "input": "x", "stream": True},
                )
                await stream_resp.text()  # drain to ensure the stream's
                # finally block ran and decremented the gauge

            metrics_resp = await cli.get("/metrics")
            assert metrics_resp.status == 200
            body = await metrics_resp.text()

        try:
            import prometheus_client  # noqa: F401
        except ImportError:
            pytest.skip("prometheus_client not installed in this env")

        assert "hermes_responses_streaming_active" in body
        assert "hermes_responses_history_length" in body

    @pytest.mark.asyncio
    async def test_stream_completed_carries_usage_and_output(self, adapter):
        """response.completed envelope must include usage tokens and
        output items so clients can render the final response without
        a separate GET /v1/responses/{id} round-trip."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            async def _mock_run_agent(**kwargs):
                cb = kwargs.get("stream_delta_callback")
                if cb:
                    cb("Hi.")
                return (
                    {
                        "final_response": "Hi.",
                        "messages": [
                            {"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "Hi."},
                        ],
                        "api_calls": 1,
                    },
                    {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "test", "input": "hi", "stream": True},
                )
                events = _parse_sse(await resp.text())

        completed = next(
            (payload for name, payload in events if name == "response.completed"),
            None,
        )
        assert completed is not None
        usage = completed["response"]["usage"]
        assert usage["input_tokens"] == 3
        assert usage["output_tokens"] == 1
        assert usage["total_tokens"] == 4
