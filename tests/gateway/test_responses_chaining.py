"""Regression test for the conversation-history doubling bug on the
Responses API path.

The historical bug: ``_handle_responses`` (and its streaming sibling
``_write_sse_responses``) persisted ``conversation_history + result['messages']``
even though ``result['messages']`` already starts with a copy of
``conversation_history`` (see the ``messages = list(conversation_history)``
initialization inside ``run_conversation``). This concatenated the prior
history into itself on every turn, producing exponential growth instead of
the linear ``2*n + 1`` shape (one user + one assistant per turn) that the
agent loop actually emits.

These tests pin both behaviors so a future upstream rebase that reintroduces
the bug fails CI before reaching production. The fork's runtime-side canary
is ``hermes_responses_history_length`` in metrics.py.
"""

from __future__ import annotations

from typing import Any, Dict, List
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
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/responses/{response_id}", adapter._handle_get_response)
    return app


def _stub_run_agent_factory(turn_seed: List[Dict[str, Any]]):
    """Build a _run_agent stub that mimics the real run_conversation
    contract: ``messages = list(conversation_history); messages.append(user);
    messages.append(assistant)``. The stub returns the *complete* messages
    list as ``result['messages']`` — exactly the shape the storage block in
    api_server.py reads from."""

    counter = {"n": 0}

    async def _run_agent(**kwargs):
        counter["n"] += 1
        n = counter["n"]
        history = list(kwargs.get("conversation_history") or [])
        user_msg = kwargs.get("user_message") or ""
        assistant_text = f"reply-{n}"
        messages = list(history)
        messages.append({"role": "user", "content": user_msg})
        messages.append({"role": "assistant", "content": assistant_text})
        turn_seed.append({"n": n, "stored_len": len(messages)})
        return (
            {
                "final_response": assistant_text,
                "messages": messages,
                "api_calls": 1,
            },
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    return _run_agent


@pytest.fixture
def adapter() -> APIServerAdapter:
    return _make_adapter()


class TestResponsesChainingHistoryGrowth:
    @pytest.mark.asyncio
    async def test_history_grows_linearly_via_previous_response_id(self, adapter):
        """Five-turn chain via ``previous_response_id`` must produce stored
        history with 2N entries (user+assistant per turn). Doubling would
        produce 2^N which exceeds the linear bound after just a few turns."""
        seed: List[Dict[str, Any]] = []
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", side_effect=_stub_run_agent_factory(seed)):
                prev_id = None
                for turn in range(1, 6):
                    payload = {
                        "model": "test",
                        "input": f"turn {turn}",
                    }
                    if prev_id is not None:
                        payload["previous_response_id"] = prev_id
                    resp = await cli.post("/v1/responses", json=payload)
                    assert resp.status == 200, await resp.text()
                    data = await resp.json()
                    prev_id = data["id"]

                    stored = adapter._response_store.get(prev_id)
                    assert stored is not None
                    history = stored.get("conversation_history", [])
                    assert len(history) == 2 * turn, (
                        f"turn {turn}: expected {2 * turn} stored messages, "
                        f"got {len(history)}. Doubling regression?"
                    )

        # Sanity: the stub recorded N completed turns.
        assert len(seed) == 5

    @pytest.mark.asyncio
    async def test_history_grows_linearly_via_named_conversation(self, adapter):
        """Same property using the ``conversation`` field (Hermes' alternative
        to ``previous_response_id``). Confirms both paths share the fix."""
        seed: List[Dict[str, Any]] = []
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", side_effect=_stub_run_agent_factory(seed)):
                conv = "regression-canary"
                last_id = None
                for turn in range(1, 6):
                    resp = await cli.post(
                        "/v1/responses",
                        json={
                            "model": "test",
                            "input": f"turn {turn}",
                            "conversation": conv,
                        },
                    )
                    assert resp.status == 200
                    last_id = (await resp.json())["id"]

                    stored = adapter._response_store.get(last_id)
                    assert stored is not None
                    history = stored.get("conversation_history", [])
                    assert len(history) == 2 * turn, (
                        f"turn {turn}: stored {len(history)} != expected {2 * turn}; "
                        "doubling regression in conversation chaining?"
                    )

    @pytest.mark.asyncio
    async def test_streaming_path_history_growth_matches_batch_path(self, adapter):
        """Same regression coverage on the SSE streaming path. Both
        _handle_responses and _write_sse_responses must share the fix —
        they have separate storage blocks, so a partial rebase could
        re-break only one."""
        seed: List[Dict[str, Any]] = []
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", side_effect=_stub_run_agent_factory(seed)):
                conv = "regression-canary-stream"
                for turn in range(1, 6):
                    resp = await cli.post(
                        "/v1/responses",
                        json={
                            "model": "test",
                            "input": f"turn {turn}",
                            "conversation": conv,
                            "stream": True,
                        },
                    )
                    assert resp.status == 200
                    await resp.text()  # drain so the storage block runs

                    last_id = adapter._response_store.get_conversation(conv)
                    assert last_id is not None
                    stored = adapter._response_store.get(last_id)
                    assert stored is not None
                    history = stored.get("conversation_history", [])
                    assert len(history) == 2 * turn, (
                        f"streaming turn {turn}: stored {len(history)} != "
                        f"expected {2 * turn}; doubling regression on SSE path?"
                    )
