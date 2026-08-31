"""Small Streamable-HTTP MCP client for remote n8n tool servers.

It intentionally supports the subset Ringback needs: initialize, tools/list,
tools/call, JSON responses, and n8n's SSE-wrapped JSON responses.  Server URLs
and required authorization headers come from ``MCP_SERVERS_JSON``; secrets may
reference environment variables as ``${NAME}`` and are never logged.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


def _validated_endpoint(raw: str, label: str = "MCP server") -> str:
    url = raw.strip()
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{label} URL must not contain userinfo or a fragment")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError(f"{label} URL must use HTTPS (HTTP is allowed only on loopback)")
    if not parsed.hostname:
        raise ValueError(f"{label} URL needs a host")
    return url


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def _contains_unresolved_env(value: Any) -> bool:
    if isinstance(value, str):
        return bool(re.search(r"\$(?:\{[^}]+\}|[A-Za-z_][A-Za-z0-9_]*)", value))
    if isinstance(value, list):
        return any(_contains_unresolved_env(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_unresolved_env(item) for item in value.values())
    return False


def _validate_server_config(config: dict[str, Any]) -> dict[str, Any]:
    if _contains_unresolved_env(config):
        raise ValueError("MCP server configuration contains an unresolved environment variable")
    url = _validated_endpoint(str(config.get("server_url", "")))
    parsed = urlsplit(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    authorization = str(config.get("authorization", "")).strip()
    custom_headers = config.get("headers") or {}
    if not isinstance(custom_headers, dict):
        raise ValueError("MCP server headers must be an object")
    has_custom_header = any(str(key).strip() and str(value).strip() for key, value in custom_headers.items())
    if authorization.lower() == "bearer" or authorization.lower().startswith("bearer  "):
        raise ValueError("MCP bearer authorization token is empty")
    if not loopback and not authorization and not has_custom_header:
        raise ValueError("Remote MCP server requires authorization or a protected custom header")
    return config


def load_server_configs(raw: str | None = None) -> list[dict[str, Any]]:
    source = (raw if raw is not None else os.environ.get("MCP_SERVERS_JSON", "[]")).strip()
    if not source:
        return []
    try:
        configs = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError("MCP_SERVERS_JSON must be valid JSON") from exc
    if not isinstance(configs, list):
        raise ValueError("MCP_SERVERS_JSON must be a JSON array")
    result: list[dict[str, Any]] = []
    for index, config in enumerate(configs):
        if not isinstance(config, dict) or not config.get("server_url"):
            raise ValueError(f"MCP server #{index + 1} needs server_url")
        item = _expand(config)
        item.setdefault("server_label", f"mcp_{index + 1}")
        result.append(_validate_server_config(item))
    return result


def _parse_mcp_body(response: httpx.Response, request_id: int | None) -> dict[str, Any]:
    if not response.content:
        return {}
    text = response.text
    candidates: list[dict[str, Any]] = []
    if "text/event-stream" in response.headers.get("content-type", "") or "data:" in text:
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                value = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                candidates.append(value)
    else:
        value = response.json()
        if isinstance(value, dict):
            candidates.append(value)
    if request_id is not None:
        for value in reversed(candidates):
            if value.get("id") == request_id:
                return value
    return candidates[-1] if candidates else {}


class MCPProtocolError(RuntimeError):
    pass


class RemoteMCPServer:
    def __init__(self, config: dict[str, Any]) -> None:
        config = _validate_server_config(_expand(config))
        self.label = str(config["server_label"])
        self.url = _validated_endpoint(str(config["server_url"]))
        self.allowed_tools = {
            str(name) for name in (config.get("allowed_tools") or []) if str(name)
        }
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        authorization = str(config.get("authorization", "")).strip()
        if authorization:
            self.headers["Authorization"] = authorization
        for key, value in (config.get("headers") or {}).items():
            self.headers[str(key)] = str(value)
        # Do not follow redirects while carrying MCP credentials.  The exact n8n
        # production Trigger URL must be configured explicitly.
        timeout = max(3.0, float(os.environ.get("MCP_REQUEST_TIMEOUT", "15")))
        self.client = httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)
        self.session_id: str | None = None
        self.protocol_version = str(
            config.get("protocol_version")
            or os.environ.get("MCP_PROTOCOL_VERSION", "2025-03-26")
        )
        self.negotiated_protocol_version: str | None = None
        self._request_id = 0
        self._lock = threading.RLock()
        self._initialized = False

    def _post(self, method: str, params: dict[str, Any], *, notification: bool = False):
        with self._lock:
            request_id = None if notification else self._request_id + 1
            if request_id is not None:
                self._request_id = request_id
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
            if request_id is not None:
                payload["id"] = request_id
            headers = dict(self.headers)
            if self.session_id:
                headers["mcp-session-id"] = self.session_id
            if self.negotiated_protocol_version and method != "initialize":
                headers["MCP-Protocol-Version"] = self.negotiated_protocol_version
            response = self.client.post(self.url, headers=headers, json=payload)
            response.raise_for_status()
            self.session_id = response.headers.get("mcp-session-id") or self.session_id
            body = _parse_mcp_body(response, request_id)
            if body.get("error"):
                error = body["error"]
                message = error.get("message", "MCP error") if isinstance(error, dict) else str(error)
                raise MCPProtocolError(f"{self.label}: {message}")
            return body.get("result", {}) if isinstance(body, dict) else {}

    def initialize(self) -> None:
        if self._initialized:
            return
        result = self._post(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "ringback-openai", "version": "1.0"},
            },
        )
        if isinstance(result, dict) and result.get("protocolVersion"):
            self.negotiated_protocol_version = str(result["protocolVersion"])
        else:
            self.negotiated_protocol_version = self.protocol_version
        # Streamable HTTP servers may be stateless. If a server assigns a
        # session id we echo it on later requests; absence is valid too.
        self._post("notifications/initialized", {}, notification=True)
        self._initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._post("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if self.allowed_tools:
            tools = [tool for tool in tools if tool.get("name") in self.allowed_tools]
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        if self.allowed_tools and name not in self.allowed_tools:
            raise PermissionError(f"Tool {name!r} is not allowed on {self.label}")
        try:
            result = self._post("tools/call", {"name": name, "arguments": arguments})
        except httpx.HTTPStatusError as exc:
            # Stateful Streamable HTTP sessions may expire. The MCP transport
            # signals that with 404; establish one fresh session and retry once.
            if exc.response.status_code != 404 or not self.session_id:
                raise
            self.session_id = None
            self.negotiated_protocol_version = None
            self._initialized = False
            self.initialize()
            result = self._post("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict) and result.get("isError"):
            raise MCPProtocolError(f"{self.label}.{name} returned an error")
        return result if isinstance(result, dict) else {"content": result}

    def close(self) -> None:
        self.client.close()


_SAFE_READ_WORDS = (
    "get", "list", "check", "read", "find", "search", "current", "available",
    "lookup", "fetch", "status", "info", "describe", "query",
)

_MUTATING_WORDS = {
    "add", "book", "cancel", "create", "delete", "execute", "insert",
    "invite", "move", "patch", "publish", "remove", "replace", "reschedule",
    "send", "set", "update", "upload", "write",
}


def _read_only_name(name: str) -> bool:
    # n8n names commonly look like ``nami_get_available_dates``. Match whole
    # tokens instead of substrings (``forget`` must not count as ``get``), and
    # reject mixed names such as ``check_and_delete``.
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    tokens = {
        token for token in re.split(r"[^A-Za-z0-9]+", expanded.lower()) if token
    }
    if tokens & _MUTATING_WORDS:
        return False
    return bool(tokens & set(_SAFE_READ_WORDS))


def _function_name(label: str, tool_name: str) -> str:
    raw = f"{label}__{tool_name}"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return clean[:64]


def tool_result_text(result: dict[str, Any], limit: int = 12000) -> str:
    content = result.get("content") if isinstance(result, dict) else result
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                pieces.append(str(item.get("text", "")))
            elif isinstance(item, dict) and "text" in item:
                pieces.append(str(item["text"]))
        if pieces:
            return "\n".join(pieces)[:limit]
    return json.dumps(result, ensure_ascii=False, default=str)[:limit]


@dataclass
class MCPTool:
    function_name: str
    server: RemoteMCPServer
    original_name: str
    schema: dict[str, Any]


class RemoteMCPRegistry:
    """Discover tools across all configured n8n MCP endpoints."""

    def __init__(self, configs: list[dict[str, Any]] | None = None) -> None:
        self.servers = [RemoteMCPServer(item) for item in (configs or load_server_configs())]
        self.tools: dict[str, MCPTool] = {}
        self.tool_policy = os.environ.get("MCP_TOOL_POLICY", "read_only").strip().lower()
        if self.tool_policy != "read_only":
            raise ValueError("MCP_TOOL_POLICY must be read_only")
        explicit = os.environ.get("MCP_ALLOWED_TOOLS", "")
        self.explicit_allowed = {
            item for item in explicit.replace(",", " ").replace(";", " ").split() if item
        }

    def discover(self) -> list[dict[str, Any]]:
        functions: list[dict[str, Any]] = []
        self.tools.clear()
        for server in self.servers:
            for raw in server.list_tools():
                original = str(raw["name"])
                if self.explicit_allowed and original not in self.explicit_allowed:
                    continue
                # The standalone booking assistant is read-only by design.
                # An allowlist narrows exposure but never turns a mutating n8n
                # node into a safe tool; writes need a separate confirmation
                # and idempotency gate that this release intentionally lacks.
                if not _read_only_name(original):
                    continue
                function_name = _function_name(server.label, original)
                if function_name in self.tools:
                    raise ValueError(
                        f"MCP tool name collision after normalization: {function_name}"
                    )
                schema = raw.get("inputSchema") or {"type": "object", "properties": {}}
                tool = MCPTool(function_name, server, original, schema)
                self.tools[function_name] = tool
                functions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": function_name,
                            "description": (
                                f"[{server.label}] " + str(raw.get("description") or original)
                            )[:1000],
                            "parameters": schema,
                        },
                    }
                )
        return functions

    def call(self, function_name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(function_name)
        if tool is None:
            raise KeyError(f"Unknown MCP tool: {function_name}")
        return tool_result_text(tool.server.call_tool(tool.original_name, arguments))

    def close(self) -> None:
        for server in self.servers:
            server.close()
