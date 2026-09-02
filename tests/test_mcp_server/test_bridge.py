from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agentos.mcp_server.bridge import AgentOSMCPBridge, _clamp_above_zero


class FakeGatewayClient:
    def __init__(self) -> None:
        self.connected_url: str | None = None
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self, url: str) -> None:
        self.connected_url = url

    async def close(self) -> None:
        self.closed = True

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        self.calls.append(("sessions.list", {"limit": limit}))
        return {"sessions": [{"key": "agent:main:main", "entry_count": 2}], "count": 1}

    async def resolve_session(self, key: str) -> dict[str, Any]:
        self.calls.append(("sessions.resolve", {"key": key}))
        return {"key": key, "session_id": "sid-1", "agent_id": "main"}

    async def session_history(self, session_key: str, limit: int = 1000) -> dict[str, Any]:
        self.calls.append(("chat.history", {"sessionKey": session_key, "limit": limit}))
        return {
            "messages": [
                {
                    "id": "m1",
                    "role": "user",
                    "text": "hello",
                    "timestamp": 1,
                },
                {
                    "id": "m2",
                    "role": "assistant",
                    "text": "looked it up",
                    "timestamp": 2,
                    "tool_calls": [
                        {
                            "type": "tool_use",
                            "tool_use_id": "tool-1",
                            "name": "lookup",
                            "input": {"q": "hello"},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "name": "lookup",
                            "result": "world",
                            "is_error": False,
                            "execution_status": {
                                "version": 1,
                                "status": "success",
                                "exit_code": 0,
                                "timed_out": False,
                                "truncated": False,
                                "reason": None,
                                "source": "adapter",
                                "preservation_class": "normal",
                            },
                        },
                    ],
                },
            ]
        }

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        if method == "sessions.messages.subscribe":
            return {
                "subscribed": True,
                "key": params["key"],
                "current_stream_seq": 7,
                "replay_complete": True,
            }
        if method == "sessions.send":
            return {"status": "accepted", "key": params["key"], "task_id": "task-1"}
        raise AssertionError(f"unexpected method: {method}")

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.events.get()
        return await asyncio.wait_for(self.events.get(), timeout=timeout)


@pytest.mark.asyncio
async def test_bridge_reuses_gateway_read_rpcs() -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(
        gateway_url="ws://127.0.0.1:18791/ws",
        gateway_client_factory=lambda: client,
    )

    sessions = await bridge.conversations_list(limit=10)
    resolved = await bridge.session_resolve("agent:main:main")
    messages = await bridge.messages_read("agent:main:main", limit=5)

    assert client.connected_url == "ws://127.0.0.1:18791/ws"
    assert sessions["sessions"][0]["key"] == "agent:main:main"
    assert resolved["session_id"] == "sid-1"
    assert messages["messages"][0]["text"] == "hello"
    assert client.calls[:3] == [
        ("sessions.list", {"limit": 10}),
        ("sessions.resolve", {"key": "agent:main:main"}),
        ("chat.history", {"sessionKey": "agent:main:main", "limit": 5}),
    ]


@pytest.mark.asyncio
async def test_transcript_jsonl_preserves_tool_result_execution_status() -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    transcript = await bridge.transcript_jsonl("agent:main:main", limit=5)

    rows = [json.loads(line) for line in transcript.splitlines()]
    tool_result_message = rows[2]["message"]
    assert tool_result_message["isError"] is False
    assert tool_result_message["executionStatus"]["status"] == "success"


@pytest.mark.asyncio
async def test_messages_send_subscribes_before_accepting_turn() -> None:
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.messages_send("agent:main:main", "continue", intent="continue")

    assert result == {
        "status": "accepted",
        "key": "agent:main:main",
        "task_id": "task-1",
        "current_stream_seq": 7,
        "replay_complete": True,
        "replay_gap_reason": None,
    }
    assert client.closed is True
    assert client.calls == [
        ("sessions.messages.subscribe", {"key": "agent:main:main", "since_stream_seq": None}),
        (
            "sessions.send",
            {
                "key": "agent:main:main",
                "message": "continue",
                "attachments": [],
                "intent": "continue",
                "_source": {
                    "caller_kind": "cli",
                    "channel_kind": "cli",
                    "channel_id": "mcp:bridge",
                    "source_kind": "mcp",
                    "source_name": "mcp_server",
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_events_wait_returns_session_events_until_terminal() -> None:
    client = FakeGatewayClient()
    await client.events.put(
        {
            "event": "session.event.text_delta",
            "payload": {"session_key": "agent:main:main", "stream_seq": 8, "text": "hi"},
        }
    )
    await client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 9, "reason": "stop"},
        }
    )
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait("agent:main:main", since_stream_seq=7, timeout_ms=1000)

    assert result["current_stream_seq"] == 9
    assert [event["event"] for event in result["events"]] == [
        "session.event.text_delta",
        "session.event.done",
    ]


@pytest.mark.asyncio
async def test_transcript_jsonl_exports_standard_tool_evidence() -> None:
    bridge = AgentOSMCPBridge(gateway_client_factory=FakeGatewayClient)

    text = await bridge.transcript_jsonl("agent:main:main")
    rows = [json.loads(line) for line in text.splitlines()]

    assert rows[0]["message"]["role"] == "user"
    assert rows[1]["message"]["role"] == "assistant"
    assert rows[1]["message"]["content"][0]["type"] == "toolCall"
    assert rows[1]["message"]["content"][0]["name"] == "lookup"
    assert rows[2]["message"]["role"] == "toolResult"
    assert rows[2]["message"]["toolCallId"] == "tool-1"


def test_mcp_server_package_does_not_import_cli_layer() -> None:
    package_root = Path("src/agentos/mcp_server")
    imported_modules: set[str] = set()
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert not any(module.startswith("agentos.cli") for module in imported_modules)
    assert not any(
        module == "agentos.gateway" or module.startswith("agentos.gateway.")
        for module in imported_modules
    )


@pytest.mark.asyncio
async def test_events_wait_uses_dedicated_connection_and_closes_it() -> None:
    clients: list[FakeGatewayClient] = []
    event_client = FakeGatewayClient()

    def factory() -> FakeGatewayClient:
        if len(clients) == 1:
            clients.append(event_client)
            return event_client
        client = FakeGatewayClient()
        clients.append(client)
        return client

    bridge = AgentOSMCPBridge(gateway_client_factory=factory)
    await bridge.conversations_list()
    await clients[0].events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 3},
        }
    )
    await event_client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 8},
        }
    )

    result = await bridge.events_wait("agent:main:main", timeout_ms=1000)

    assert len(clients) == 2
    assert result["current_stream_seq"] == 8
    assert clients[0].closed is False
    assert event_client.closed is True


# ---------------------------------------------------------------------------
# issue #685 — upper-clamp inbound bridge parameters
# ---------------------------------------------------------------------------
#
# Without these clamps a misbehaving or compromised MCP client can request
# ``max_events=10**7``, ``timeout_ms=60_000``, or ``limit=10**9`` and pin a
# gateway connection while unbounded buffers grow, OOMing the host
# (100K events collected in 0.42s by the issue author). The bridge must
# clamp each of those into a finite, documented ceiling before forwarding.


@pytest.mark.asyncio
async def test_conversations_list_clamps_huge_limit_to_ceiling() -> None:
    """``conversations_list(limit=10**9)`` must be forwarded to the gateway
    at the documented ceiling, not the raw hostile value."""
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.conversations_list(limit=10**9)

    assert client.calls == [("sessions.list", {"limit": 5000})]


@pytest.mark.asyncio
async def test_conversations_list_passes_through_within_bounds() -> None:
    """Sanity check: a sensible ``limit`` is forwarded unchanged so the
    clamp does not regress the happy path."""
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.conversations_list(limit=50)

    assert client.calls == [("sessions.list", {"limit": 50})]


@pytest.mark.asyncio
async def test_messages_read_clamps_huge_limit_to_ceiling() -> None:
    """``messages_read(key, limit=10**9)`` must clamp before forwarding."""
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 5},
        }
    )
    await bridge.events_wait("agent:main:main", timeout_ms=200)
    # Subscribe happens before the history read on the first call, so reset.
    client.calls.clear()

    await bridge.messages_read("agent:main:main", limit=10**9)

    assert client.calls == [
        ("chat.history", {"sessionKey": "agent:main:main", "limit": 5000})
    ]


@pytest.mark.asyncio
async def test_transcript_jsonl_clamps_huge_limit_to_ceiling() -> None:
    """``transcript_jsonl`` delegates to ``messages_read`` and must inherit
    the same ceiling."""
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.transcript_jsonl("agent:main:main", limit=10**9)

    assert client.calls == [
        ("chat.history", {"sessionKey": "agent:main:main", "limit": 5000})
    ]


@pytest.mark.asyncio
async def test_bridge_rejects_zero_or_negative_limits() -> None:
    """A hostile ``limit=0`` or ``limit=-5`` must coerce to 1 so the
    gateway call does not break the lower-bound invariant. This is the
    existing behavior; the upper-clamp change must not regress it."""
    client = FakeGatewayClient()
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    await bridge.conversations_list(limit=0)
    await bridge.messages_read("agent:main:main", limit=-5)

    assert client.calls == [
        ("sessions.list", {"limit": 1}),
        ("chat.history", {"sessionKey": "agent:main:main", "limit": 1}),
    ]


@pytest.mark.asyncio
async def test_events_wait_clamps_huge_max_events_to_ceiling() -> None:
    """``events_wait(..., max_events=10**7)`` must terminate promptly with
    the events list bounded by the ceiling. Without the clamp the loop
    would run indefinitely until ``timeout_ms`` expires."""
    client = FakeGatewayClient()
    # Drain a single terminal event so the loop exits on its own.
    await client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 1},
        }
    )
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait(
        "agent:main:main", timeout_ms=200, max_events=10**7
    )

    assert len(result["events"]) == 1


@pytest.mark.asyncio
async def test_events_wait_passes_through_within_bounds() -> None:
    """Sanity check: small ``max_events`` and ``timeout_ms`` are honored
    exactly so the clamp does not regress the happy path."""
    client = FakeGatewayClient()
    await client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 9},
        }
    )
    bridge = AgentOSMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait(
        "agent:main:main", timeout_ms=500, max_events=5
    )

    assert result["current_stream_seq"] == 9
    assert len(result["events"]) == 1


# ---------------------------------------------------------------------------
# _clamp_above_zero — unit tests for the helper used by events_wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "ceiling", "expected"),
    [
        # Existing lower-bound behavior preserved.
        (0, 5000, 1),
        (-1, 5000, 1),
        (-1000, 5000, 1),
        # Within bounds: pass through.
        (1, 5000, 1),
        (50, 5000, 50),
        (5000, 5000, 5000),
        # Hostile upper bound clamps to ceiling.
        (10**9, 5000, 5000),
        (10**9, 10000, 10000),
        (10**9, 300000, 300000),
        # Non-integer (stringified number, type-coerced) is accepted.
        ("5000", 5000, 5000),
        # Non-numeric input falls back to 1.
        ("not-a-number", 5000, 1),
        (None, 5000, 1),
    ],
)
def test_clamp_above_zero_bounds(raw: Any, ceiling: int, expected: int) -> None:
    assert _clamp_above_zero(raw, ceiling) == expected
