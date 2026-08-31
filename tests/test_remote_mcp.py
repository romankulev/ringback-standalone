#!/usr/bin/env python3
"""Offline tests for remote MCP configuration, SSE sessions, and tool policy."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import remote_mcp  # noqa: E402


class ServerConfigTests(unittest.TestCase):
    def test_environment_references_expand_recursively(self) -> None:
        raw = json.dumps(
            [
                {
                    "server_url": "${RINGBACK_TEST_MCP_URL}",
                    "authorization": "Bearer ${RINGBACK_TEST_MCP_TOKEN}",
                    "headers": {"X-Test-Header": "${RINGBACK_TEST_MCP_HEADER}"},
                    "allowed_tools": ["${RINGBACK_TEST_MCP_TOOL}"],
                }
            ]
        )
        env = {
            "RINGBACK_TEST_MCP_URL": "https://mcp.invalid/stream",
            "RINGBACK_TEST_MCP_TOKEN": "offline-token",
            "RINGBACK_TEST_MCP_HEADER": "offline-header",
            "RINGBACK_TEST_MCP_TOOL": "list_slots",
        }

        with mock.patch.dict(os.environ, env, clear=False):
            configs = remote_mcp.load_server_configs(raw)

        self.assertEqual(
            configs,
            [
                {
                    "server_url": "https://mcp.invalid/stream",
                    "authorization": "Bearer offline-token",
                    "headers": {"X-Test-Header": "offline-header"},
                    "allowed_tools": ["list_slots"],
                    "server_label": "mcp_1",
                }
            ],
        )

    def test_empty_configuration_is_valid(self) -> None:
        self.assertEqual(remote_mcp.load_server_configs(""), [])
        self.assertEqual(remote_mcp.load_server_configs("[]"), [])

    def test_malformed_or_incomplete_configuration_is_rejected(self) -> None:
        cases = (
            ("not json", "valid JSON"),
            ('{"server_url":"https://mcp.invalid"}', "JSON array"),
            ('[{}]', "needs server_url"),
        )
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, message):
                remote_mcp.load_server_configs(raw)

    def test_credentials_require_https_except_on_loopback(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            remote_mcp.RemoteMCPServer(
                {"server_label": "bad", "server_url": "http://mcp.example.test/tools"}
            )
        server = remote_mcp.RemoteMCPServer(
            {"server_label": "local", "server_url": "http://127.0.0.1:5678/mcp"}
        )
        server.close()

    def test_remote_server_requires_auth_and_resolved_secret(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires authorization"):
            remote_mcp.load_server_configs(
                '[{"server_url":"https://mcp.example.test/tools"}]'
            )
        with self.assertRaisesRegex(ValueError, "unresolved environment variable"):
            remote_mcp.load_server_configs(
                '[{"server_url":"https://mcp.example.test/tools",'
                '"authorization":"Bearer ${MISSING_TEST_TOKEN}"}]'
            )

def _response(
    text: str = "",
    *,
    status: int = 200,
    content_type: str = "text/event-stream",
    session_id: str | None = None,
) -> httpx.Response:
    headers = {"content-type": content_type}
    if session_id:
        headers["mcp-session-id"] = session_id
    return httpx.Response(
        status,
        headers=headers,
        text=text,
        request=httpx.Request("POST", "https://mcp.invalid/stream"),
    )


def _sse(value: dict) -> str:
    return f"event: message\ndata: {json.dumps(value)}\n\n"


class FakeHTTPClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.closed = False

    def post(self, url, *, headers, json):
        self.requests.append({"url": url, "headers": dict(headers), "json": json})
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class StreamableHttpSessionTests(unittest.TestCase):
    def test_sse_parser_selects_the_matching_json_rpc_id(self) -> None:
        response = _response(
            "data: not-json\n\n"
            + _sse({"jsonrpc": "2.0", "id": 7, "result": {"value": "wanted"}})
            + _sse({"jsonrpc": "2.0", "id": 8, "result": {"value": "other"}})
        )

        self.assertEqual(
            remote_mcp._parse_mcp_body(response, 7)["result"],
            {"value": "wanted"},
        )

    def test_initialize_list_and_call_reuse_the_server_session(self) -> None:
        client = FakeHTTPClient(
            [
                _response(
                    _sse(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {"protocolVersion": "2025-03-26"},
                        }
                    ),
                    session_id="offline-session",
                ),
                _response(status=202, text="", content_type="application/json"),
                _response(
                    _sse({"jsonrpc": "2.0", "id": 999, "result": {"ignored": True}})
                    + _sse(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "tools": [
                                    {
                                        "name": "list_slots",
                                        "description": "List free slots",
                                        "inputSchema": {"type": "object"},
                                    }
                                ]
                            },
                        }
                    )
                ),
                _response(
                    _sse(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "result": {
                                "content": [{"type": "text", "text": "10:00, 11:30"}]
                            },
                        }
                    )
                ),
            ]
        )

        with mock.patch.object(remote_mcp.httpx, "Client", return_value=client):
            server = remote_mcp.RemoteMCPServer(
                {
                    "server_label": "calendar",
                    "server_url": "https://mcp.invalid/stream",
                    "authorization": "Bearer offline-token",
                }
            )

        tools = server.list_tools()
        result = server.call_tool("list_slots", {"date": "2030-01-02"})
        server.close()

        self.assertEqual([tool["name"] for tool in tools], ["list_slots"])
        self.assertEqual(result["content"][0]["text"], "10:00, 11:30")
        self.assertEqual(server.session_id, "offline-session")
        self.assertTrue(client.closed)

        requests = client.requests
        self.assertEqual(
            [item["json"]["method"] for item in requests],
            ["initialize", "notifications/initialized", "tools/list", "tools/call"],
        )
        self.assertEqual(requests[0]["json"]["id"], 1)
        self.assertEqual(
            requests[0]["json"]["params"]["clientInfo"]["name"],
            "ringback-openai",
        )
        self.assertNotIn("id", requests[1]["json"])
        self.assertEqual(requests[2]["json"]["id"], 2)
        self.assertEqual(requests[3]["json"]["id"], 3)
        self.assertNotIn("mcp-session-id", requests[0]["headers"])
        for request in requests[1:]:
            self.assertEqual(request["headers"]["mcp-session-id"], "offline-session")
            self.assertEqual(request["headers"]["MCP-Protocol-Version"], "2025-03-26")

    def test_stateless_server_without_session_header_is_supported(self) -> None:
        client = FakeHTTPClient(
            [
                _response(
                    _sse({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}})
                ),
                _response(status=202, text="", content_type="application/json"),
                _response(
                    _sse({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
                ),
            ]
        )
        with mock.patch.object(remote_mcp.httpx, "Client", return_value=client):
            server = remote_mcp.RemoteMCPServer(
                {
                    "server_label": "stateless",
                    "server_url": "https://mcp.invalid/stream",
                    "authorization": "Bearer offline-token",
                }
            )

        self.assertEqual(server.list_tools(), [])
        self.assertIsNone(server.session_id)
        self.assertTrue(all("mcp-session-id" not in item["headers"] for item in client.requests))


class FakeRemoteServer:
    instances: list["FakeRemoteServer"] = []

    def __init__(self, config: dict) -> None:
        self.label = config["server_label"]
        self.allowed_tools = set(config.get("allowed_tools") or [])
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.instances.append(self)

    def list_tools(self) -> list[dict]:
        return [
            {"name": "list_services", "description": "Read services"},
            {"name": "get_slots", "description": "Read availability"},
            {"name": "create_booking", "description": "Create a booking"},
            {"name": "delete_booking", "description": "Delete a booking"},
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"called {name}"}]}

    def close(self) -> None:
        self.closed = True


class DynamicToolPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeRemoteServer.instances.clear()

    def _registry(self, extra_env: dict[str, str] | None = None):
        env = {"MCP_TOOL_POLICY": "read_only", "MCP_ALLOWED_TOOLS": ""}
        env.update(extra_env or {})
        patcher = mock.patch.dict(os.environ, env, clear=False)
        class_patcher = mock.patch.object(remote_mcp, "RemoteMCPServer", FakeRemoteServer)
        patcher.start()
        class_patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(class_patcher.stop)
        return remote_mcp.RemoteMCPRegistry(
            [{"server_label": "yclients", "server_url": "https://mcp.invalid"}]
        )

    def test_read_only_default_hides_mutating_dynamic_tools(self) -> None:
        registry = self._registry()
        functions = registry.discover()
        names = [item["function"]["name"] for item in functions]

        self.assertEqual(names, ["yclients__list_services", "yclients__get_slots"])
        self.assertNotIn("yclients__create_booking", registry.tools)
        self.assertEqual(
            registry.call("yclients__get_slots", {"date": "2030-01-02"}),
            "called get_slots",
        )

    def test_read_only_filter_rejects_substrings_and_mixed_mutations(self) -> None:
        self.assertFalse(remote_mcp._read_only_name("forget_everything"))
        self.assertFalse(remote_mcp._read_only_name("check_and_delete_slot"))
        self.assertTrue(remote_mcp._read_only_name("nami_get_available_dates"))

    def test_explicit_allowlist_can_select_one_named_read_tool(self) -> None:
        registry = self._registry({"MCP_ALLOWED_TOOLS": "get_slots"})

        functions = registry.discover()

        self.assertEqual(
            [item["function"]["name"] for item in functions],
            ["yclients__get_slots"],
        )

    def test_allowlist_cannot_expose_a_mutating_tool(self) -> None:
        registry = self._registry({"MCP_ALLOWED_TOOLS": "create_booking"})
        self.assertEqual(registry.discover(), [])

    def test_unknown_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "MCP_TOOL_POLICY"):
            self._registry({"MCP_TOOL_POLICY": "read-only"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
