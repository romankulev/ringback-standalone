"""Autonomous OpenAI brain with remote n8n MCP tool calling."""
from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from remote_mcp import RemoteMCPRegistry


def _validated_openai_endpoint(raw: str) -> str:
    endpoint = raw.strip()
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("OPENAI_BASE_URL must not contain userinfo or a fragment")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise RuntimeError(
            "OPENAI_BASE_URL must use HTTPS (HTTP is allowed only on loopback)"
        )
    if not parsed.hostname:
        raise RuntimeError("OPENAI_BASE_URL needs a host")
    return endpoint


DEFAULT_SYSTEM_PROMPT = """\
Ты — голосовой ассистент в телефонном разговоре. Отвечай по-русски, кратко,
естественно и без Markdown. Для актуальных данных всегда используй доступные MCP-инструменты
и не выдумывай свободные окна, услуги, цены или мастеров. Для YCLIENTS иди по цепочке: услуга,
мастер, даты, время, финальная проверка слота. Предлагай два-три ближайших варианта. Не совершай
изменяющих данные действий без явного подтверждения пользователя. Когда разговор естественно
завершён, добавь в самом конце служебную метку <<END_CALL>>.
"""


class OpenAIAgent:
    def __init__(self, *, objective: str = "") -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("OPENAI_MODEL", "").strip()
        if not self.api_key or not self.model:
            raise RuntimeError("OPENAI_API_KEY and OPENAI_MODEL are required")
        self.endpoint = _validated_openai_endpoint(
            os.environ.get(
                "OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions"
            )
        )
        self.registry = RemoteMCPRegistry()
        self.tools = self.registry.discover()
        system = os.environ.get("AUTONOMOUS_SYSTEM_PROMPT", "").strip() or DEFAULT_SYSTEM_PROMPT
        # Newer OpenAI reasoning models use the developer role for application
        # instructions. It is also accepted by current non-reasoning models.
        self.messages: list[dict[str, Any]] = [{"role": "developer", "content": system}]
        if objective:
            # The objective comes from a Telegram command and is untrusted user
            # input, never a system instruction.
            self.messages.append(
                {"role": "user", "content": f"Контекст запроса: {objective.strip()}"}
            )
        self.client = httpx.Client(timeout=httpx.Timeout(45.0))
        self.max_tool_rounds = max(
            0, int(os.environ.get("OPENAI_MAX_TOOL_ROUNDS", "6"))
        )
        self.max_tool_calls = max(
            1, int(os.environ.get("OPENAI_MAX_TOOL_CALLS_PER_TURN", "8"))
        )
        self.turn_timeout = max(
            5.0, float(os.environ.get("OPENAI_TURN_TIMEOUT", "60"))
        )

    def _request(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            # max_tokens is deprecated and rejected by newer reasoning models.
            "max_completion_tokens": max(
                64, int(os.environ.get("OPENAI_MAX_TOKENS", "600"))
            ),
        }
        temperature = os.environ.get("OPENAI_TEMPERATURE", "").strip()
        if temperature:
            payload["temperature"] = float(temperature)
        reasoning_effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip()
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if self.tools:
            payload["tools"] = self.tools
            payload["tool_choice"] = "auto"
        try:
            response = self.client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"OpenAI returned HTTP {exc.response.status_code}"
            ) from None
        except httpx.RequestError:
            raise RuntimeError("OpenAI is unreachable") from None
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError("OpenAI returned invalid JSON") from None
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise RuntimeError("OpenAI returned no assistant message")
        return choices[0]["message"]

    def reply(self, user_text: str) -> tuple[str, bool]:
        self.messages.append({"role": "user", "content": user_text})
        deadline = time.monotonic() + self.turn_timeout
        total_tool_calls = 0
        for round_index in range(self.max_tool_rounds + 1):
            if time.monotonic() >= deadline:
                raise RuntimeError("OpenAI turn deadline exceeded")
            message = self._request()
            self.messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                text = str(message.get("content") or "").strip()
                end_call = "<<END_CALL>>" in text
                return text.replace("<<END_CALL>>", "").strip(), end_call
            if round_index >= self.max_tool_rounds:
                raise RuntimeError("OpenAI tool round limit exceeded")
            total_tool_calls += len(tool_calls)
            if total_tool_calls > self.max_tool_calls:
                raise RuntimeError("OpenAI tool call limit exceeded")
            for call in tool_calls:
                if time.monotonic() >= deadline:
                    raise RuntimeError("OpenAI turn deadline exceeded")
                function = call.get("function") or {}
                name = str(function.get("name", ""))
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    result = self.registry.call(name, arguments)
                except Exception as exc:
                    # Exception strings may contain credential-bearing endpoint URLs.
                    status = (
                        exc.response.status_code
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None
                    )
                    suffix = f" (HTTP {status})" if status is not None else ""
                    result = f"Tool error: {type(exc).__name__}{suffix}"
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": result,
                    }
                )
        raise RuntimeError("OpenAI exceeded the MCP tool-call round limit")

    def close(self) -> None:
        self.registry.close()
        self.client.close()
