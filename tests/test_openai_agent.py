#!/usr/bin/env python3
"""Offline OpenAI agent tests with fake HTTP and MCP registry implementations."""
from __future__ import annotations

import copy
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import openai_agent  # noqa: E402


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def discover(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "calendar__get_slots",
                    "description": "Get available slots",
                    "parameters": {
                        "type": "object",
                        "properties": {"date": {"type": "string"}},
                    },
                },
            }
        ]

    def call(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        return "10:00 and 11:30 are available"

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = body
        self.raise_count = 0

    def raise_for_status(self) -> None:
        self.raise_count += 1

    def json(self) -> dict:
        return self.body


class FakeOpenAIHTTPClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = [FakeResponse(body) for body in responses]
        self.requests: list[dict] = []
        self.closed = False

    def post(self, url, *, headers, json):
        self.requests.append(
            {"url": url, "headers": dict(headers), "json": copy.deepcopy(json)}
        )
        if not self.responses:
            raise AssertionError("unexpected OpenAI request")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class OpenAIConfigurationTests(unittest.TestCase):
    def test_api_key_and_model_are_both_required(self) -> None:
        for env in (
            {},
            {"OPENAI_API_KEY": "offline-key"},
            {"OPENAI_MODEL": "test/model"},
        ):
            with (
                self.subTest(env=env),
                mock.patch.dict(os.environ, env, clear=True),
                self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY.*OPENAI_MODEL"),
            ):
                openai_agent.OpenAIAgent()

    def test_api_key_cannot_be_sent_over_plain_http(self) -> None:
        env = {
            "OPENAI_API_KEY": "secret",
            "OPENAI_MODEL": "test/model",
            "OPENAI_BASE_URL": "http://api.example.test/v1/chat/completions",
        }
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(
            RuntimeError, "must use HTTPS"
        ):
            openai_agent.OpenAIAgent()

    def test_default_endpoint_is_direct_openai_chat_completions(self) -> None:
        registry = FakeRegistry()
        http_client = FakeOpenAIHTTPClient([])
        env = {"OPENAI_API_KEY": "offline-key", "OPENAI_MODEL": "test/model"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(openai_agent, "RemoteMCPRegistry", return_value=registry),
            mock.patch.object(openai_agent.httpx, "Client", return_value=http_client),
        ):
            agent = openai_agent.OpenAIAgent()
            self.assertEqual(
                agent.endpoint, "https://api.openai.com/v1/chat/completions"
            )
            agent.close()


class OpenAIToolRoundTests(unittest.TestCase):
    def test_luna_disables_reasoning_when_chat_completions_uses_tools(self) -> None:
        registry = FakeRegistry()
        http_client = FakeOpenAIHTTPClient(
            [{"choices": [{"message": {"role": "assistant", "content": "Готово"}}]}]
        )
        env = {
            "OPENAI_API_KEY": "offline-key",
            "OPENAI_MODEL": "gpt-5.6-luna",
            "OPENAI_REASONING_EFFORT": "low",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(openai_agent, "RemoteMCPRegistry", return_value=registry),
            mock.patch.object(openai_agent.httpx, "Client", return_value=http_client),
        ):
            agent = openai_agent.OpenAIAgent()
            self.assertEqual(agent.reply("Проверка"), ("Готово", False))
            agent.close()

        self.assertEqual(
            http_client.requests[0]["json"]["reasoning_effort"], "none"
        )

    def test_one_mcp_tool_round_then_returns_spoken_answer(self) -> None:
        registry = FakeRegistry()
        http_client = FakeOpenAIHTTPClient(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "offline-call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "calendar__get_slots",
                                            "arguments": '{"date":"2030-01-02"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "Есть свободное время в десять и в одиннадцать тридцать. "
                                    "<<END_CALL>>"
                                ),
                            }
                        }
                    ]
                },
            ]
        )
        env = {
            "OPENAI_API_KEY": "offline-key",
            "OPENAI_MODEL": "test/model",
            "OPENAI_BASE_URL": "https://api.openai.invalid/v1/chat/completions",
            "OPENAI_MAX_TOOL_ROUNDS": "2",
            "OPENAI_TEMPERATURE": "0.1",
            "OPENAI_REASONING_EFFORT": "low",
        }

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(openai_agent, "RemoteMCPRegistry", return_value=registry),
            mock.patch.object(openai_agent.httpx, "Client", return_value=http_client),
        ):
            agent = openai_agent.OpenAIAgent(objective="Найти ближайшее окно")
            text, end_call = agent.reply("Когда есть свободное время?")
            agent.close()

        self.assertEqual(
            text, "Есть свободное время в десять и в одиннадцать тридцать."
        )
        self.assertTrue(end_call)
        self.assertEqual(
            registry.calls,
            [("calendar__get_slots", {"date": "2030-01-02"})],
        )
        self.assertTrue(registry.closed)
        self.assertTrue(http_client.closed)
        self.assertEqual(len(http_client.requests), 2)

        first, second = http_client.requests
        self.assertEqual(first["url"], "https://api.openai.invalid/v1/chat/completions")
        self.assertEqual(first["headers"]["Authorization"], "Bearer offline-key")
        self.assertEqual(first["headers"]["Content-Type"], "application/json")
        self.assertEqual(set(first["headers"]), {"Authorization", "Content-Type"})
        self.assertNotIn("store", first["json"])
        self.assertEqual(first["json"]["model"], "test/model")
        self.assertEqual(first["json"]["temperature"], 0.1)
        self.assertEqual(first["json"]["reasoning_effort"], "low")
        self.assertEqual(first["json"]["max_completion_tokens"], 600)
        self.assertNotIn("max_tokens", first["json"])
        self.assertEqual(first["json"]["tool_choice"], "auto")
        self.assertEqual(len(first["json"]["messages"]), 3)
        self.assertEqual(first["json"]["messages"][0]["role"], "developer")
        self.assertNotIn("Найти ближайшее окно", first["json"]["messages"][0]["content"])
        self.assertIn("Найти ближайшее окно", first["json"]["messages"][1]["content"])

        second_roles = [item["role"] for item in second["json"]["messages"]]
        self.assertEqual(second_roles, ["developer", "user", "user", "assistant", "tool"])
        tool_message = second["json"]["messages"][-1]
        self.assertEqual(tool_message["tool_call_id"], "offline-call-1")
        self.assertEqual(tool_message["content"], "10:00 and 11:30 are available")

    def test_total_tool_call_cap_rejects_oversized_model_batch(self) -> None:
        registry = FakeRegistry()
        calls = [
            {
                "id": f"call-{index}",
                "type": "function",
                "function": {
                    "name": "calendar__get_slots",
                    "arguments": '{"date":"2030-01-02"}',
                },
            }
            for index in range(2)
        ]
        http_client = FakeOpenAIHTTPClient(
            [{"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}]
        )
        env = {
            "OPENAI_API_KEY": "offline-key",
            "OPENAI_MODEL": "test/model",
            "OPENAI_MAX_TOOL_CALLS_PER_TURN": "1",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(openai_agent, "RemoteMCPRegistry", return_value=registry),
            mock.patch.object(openai_agent.httpx, "Client", return_value=http_client),
        ):
            agent = openai_agent.OpenAIAgent()
            with self.assertRaisesRegex(RuntimeError, "tool call limit"):
                agent.reply("Проверь все окна")
            agent.close()
        self.assertEqual(registry.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
