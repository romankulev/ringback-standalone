#!/usr/bin/env python3
"""Offline tests for the standalone Telegram/OpenAI application host."""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import standalone_app  # noqa: E402
import configure_telegram  # noqa: E402
from webrtc_transport import WebRTCTransport  # noqa: E402


ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:offline-secret-token",
    "TELEGRAM_CHAT_ID": "42",
    "TELEGRAM_ALLOWED_USER_IDS": "42",
    "VOICE_TELEGRAM_USER_ID": "42",
    "WEBRTC_PUBLIC_URL": "https://voice.example.invalid",
    "OPENAI_API_KEY": "offline-openai-key",
    "OPENAI_MODEL": "test/model",
    "MCP_SERVERS_JSON": "[]",
    "STANDALONE_SILENCE_RETRIES": "0",
}


class FakeTransport:
    def __init__(self) -> None:
        self.handler = None
        self.failed_requests: list[int] = []

    def set_call_request_handler(self, handler) -> None:
        self.handler = handler

    def notify_call_request_failed(self, user_id: int) -> None:
        self.failed_requests.append(user_id)


class FakeSession:
    def __init__(self, *, heard: str = "Найди окно на завтра") -> None:
        self.transport = FakeTransport()
        self._heard = heard
        self.disconnected = False
        self.connected = False
        self.calls: list[tuple] = []

    def start_lib(self) -> None:
        self.calls.append(("start_lib",))

    def place_call(self, *, answer_timeout, callee) -> bool:
        self.calls.append(("place_call", answer_timeout, callee))
        self.connected = True
        return True

    def speak(self, text) -> str:
        self.calls.append(("speak", text))
        return "spoke"

    def listen(self) -> str:
        self.calls.append(("listen",))
        value, self._heard = self._heard, ""
        return value

    def speak_interruptible(self, text, listen_after=True) -> dict:
        self.calls.append(("speak_interruptible", text, listen_after))
        return {"ended": False, "user": ""}

    def hangup(self) -> None:
        self.calls.append(("hangup",))
        self.connected = False
        self.disconnected = True

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))

    def status(self) -> str:
        return "webrtc=idle"


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.messages: list[tuple] = []
        self.closed = False

    def call(self, method, payload=None, *, timeout=35.0):
        self.calls.append((method, payload, timeout))
        return [] if method == "getUpdates" else True

    def send_message(self, chat_id, text, *, web_app_url="", web_app_label="Открыть Ringback"):
        self.messages.append((chat_id, text, web_app_url, web_app_label))
        return True

    def close(self) -> None:
        self.closed = True


class FakeAgent:
    def __init__(self, objective: str) -> None:
        self.objective = objective
        self.requests: list[str] = []
        self.closed = False

    def reply(self, text: str) -> tuple[str, bool]:
        self.requests.append(text)
        return "Есть свободное время в десять.", True

    def close(self) -> None:
        self.closed = True


def update(command: str, *, user_id: int = 42) -> dict:
    return {
        "update_id": 1,
        "message": {
            "text": command,
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
        },
    }


class DotenvTests(unittest.TestCase):
    def test_existing_environment_wins_and_home_is_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'KEEP="from-file"\nMODEL="$HOME/model.bin"\n# ignored\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"KEEP": "from-process", "HOME": "/safe/home"}, clear=True):
                standalone_app.load_env(path)
                self.assertEqual(os.environ["KEEP"], "from-process")
                self.assertEqual(os.environ["MODEL"], "/safe/home/model.bin")

    def test_json_value_is_shell_safe_when_written(self) -> None:
        value = '[{"authorization":"Bearer ${N8N_MCP_TOKEN}"}]'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            configure_telegram.replace_env_values(
                {"MCP_SERVERS_JSON": value}, path=path
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                f"MCP_SERVERS_JSON='{value}'\n",
            )


class ConfigurationCheckTests(unittest.TestCase):
    def test_empty_mcp_array_is_reported_missing(self) -> None:
        with (
            mock.patch.dict(os.environ, ENV, clear=True),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = standalone_app._print_check()
        self.assertEqual(result, 2)
        self.assertIn("MISSING n8n MCP config", output.getvalue())

    def test_nonempty_mcp_server_list_is_recognized(self) -> None:
        raw = (
            '[{"server_label":"n8n","server_url":"https://mcp.example.invalid",'
            '"authorization":"Bearer offline-token"}]'
        )
        self.assertTrue(standalone_app._has_mcp_servers(raw))
        self.assertFalse(standalone_app._has_mcp_servers("[]"))
        self.assertFalse(standalone_app._has_mcp_servers("not-json"))


class TelegramClientTests(unittest.TestCase):
    def test_transport_error_never_exposes_bot_token(self) -> None:
        token = "123456:never-log-this"
        request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/getUpdates")
        client = mock.Mock()
        client.post.side_effect = httpx.ConnectError(
            f"failed at https://api.telegram.org/bot{token}/getUpdates", request=request
        )
        bot = standalone_app.TelegramBotClient(token, client=client)
        with self.assertRaises(standalone_app.TelegramBotError) as caught:
            bot.call("getUpdates")
        self.assertNotIn(token, str(caught.exception))

    def test_http_client_internal_logging_is_disabled(self) -> None:
        logger = logging.getLogger("httpx")
        logger.disabled = False
        standalone_app.configure_logging()
        self.assertTrue(logger.disabled)


class PairingTests(unittest.TestCase):
    def test_only_exact_one_time_start_code_can_pair(self) -> None:
        updates = [
            update("hello", user_id=99),
            update("/start wrong", user_id=98),
            update("/start expected-code", user_id=42),
        ]
        found = configure_telegram.find_private_user(updates, "expected-code")
        self.assertIsNotNone(found)
        self.assertEqual(found[:2], (42, 42))
        self.assertIsNone(configure_telegram.find_private_user(updates, "missing"))


class CommandTests(unittest.TestCase):
    def make_app(self):
        session, bot = FakeSession(), FakeBot()
        app = standalone_app.StandaloneApp(
            session=session,
            bot=bot,
            agent_factory=lambda **kwargs: FakeAgent(kwargs["objective"]),
        )
        return app, session, bot

    def test_start_sends_authenticated_mini_app_button(self) -> None:
        with mock.patch.dict(os.environ, ENV, clear=True):
            app, session, bot = self.make_app()
            app.process_update(update("/start"))
            self.assertEqual(len(bot.messages), 1)
            self.assertEqual(bot.messages[0][0], 42)
            self.assertEqual(bot.messages[0][2], "https://voice.example.invalid/")
            self.assertIsNotNone(session.transport.handler)

    def test_unauthorized_user_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, ENV, clear=True):
            app, _session, bot = self.make_app()
            app.process_update(update("/start", user_id=99))
            self.assertEqual(bot.messages, [])

    def test_call_command_uses_request_call_api(self) -> None:
        with mock.patch.dict(os.environ, ENV, clear=True):
            app, _session, bot = self.make_app()
            with mock.patch.object(app, "request_call", return_value=True) as request_call:
                app.process_update(update("/call Проверь окна"))
            request_call.assert_called_once_with(
                42, objective="Проверь окна", source="telegram"
            )
            self.assertIn("Готовлю", bot.messages[0][1])

    def test_call_command_really_queues_one_call_and_sends_ack(self) -> None:
        class BlockingSession(FakeSession):
            def __init__(self) -> None:
                super().__init__()
                self.placing = threading.Event()
                self.release = threading.Event()

            def place_call(self, *, answer_timeout, callee) -> bool:
                self.calls.append(("place_call", answer_timeout, callee))
                self.placing.set()
                self.release.wait(2.0)
                self.connected = True
                return True

        with mock.patch.dict(os.environ, ENV, clear=True):
            session, bot = BlockingSession(), FakeBot()
            app = standalone_app.StandaloneApp(
                session=session,
                bot=bot,
                agent_factory=lambda **kwargs: FakeAgent(kwargs["objective"]),
            )
            app.process_update(update("/call"))
            self.assertTrue(session.placing.wait(1.0))
            thread = app._call_thread
            self.assertIsNotNone(thread)
            self.assertEqual(bot.messages[0][0], 42)
            self.assertIn("Готовлю", bot.messages[0][1])
            self.assertEqual(session.calls[0][2], "telegram:42")
            session.release.set()
            thread.join(2.0)
            self.assertFalse(thread.is_alive())

    def test_allowed_user_cannot_hang_up_someone_elses_call(self) -> None:
        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        env = dict(ENV, TELEGRAM_ALLOWED_USER_IDS="42,99")
        with mock.patch.dict(os.environ, env, clear=True):
            app, session, bot = self.make_app()
            app._call_thread = AliveThread()  # type: ignore[assignment]
            app._call_user_id = 42
            app.process_update(update("/hangup", user_id=99))
            self.assertNotIn(("hangup",), session.calls)
            self.assertIn("другого пользователя", bot.messages[-1][1])
            app.process_update(update("/hangup", user_id=42))
            self.assertIn(("hangup",), session.calls)


class ConversationTests(unittest.TestCase):
    def test_voice_turn_runs_stt_openai_tts_and_closes_agent(self) -> None:
        agents: list[FakeAgent] = []

        def factory(*, objective):
            agent = FakeAgent(objective)
            agents.append(agent)
            return agent

        with mock.patch.dict(os.environ, ENV, clear=True):
            session, bot = FakeSession(), FakeBot()
            app = standalone_app.StandaloneApp(
                session=session, bot=bot, agent_factory=factory
            )
            app._call_worker(42, "Найти время")

        self.assertEqual(agents[0].objective, "Найти время")
        self.assertEqual(agents[0].requests, ["Найди окно на завтра"])
        self.assertTrue(agents[0].closed)
        spoken = [item[1] for item in session.calls if item[0] == "speak"]
        self.assertIn("Есть свободное время в десять.", spoken)
        self.assertIn(("hangup",), session.calls)

    def test_concurrent_call_request_is_rejected(self) -> None:
        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        with mock.patch.dict(os.environ, ENV, clear=True):
            app = standalone_app.StandaloneApp(
                session=FakeSession(), bot=FakeBot(), agent_factory=FakeAgent
            )
            app._call_thread = AliveThread()  # type: ignore[assignment]
            self.assertFalse(app.request_call(42))
            self.assertFalse(app.request_call(99))

    def test_shutdown_hangs_up_call_and_closes_resources(self) -> None:
        with mock.patch.dict(os.environ, ENV, clear=True):
            session, bot = FakeSession(), FakeBot()
            app = standalone_app.StandaloneApp(session=session, bot=bot)
            app.shutdown()
            app.shutdown()
        self.assertTrue(app.stop_event.is_set())
        self.assertIn(("shutdown",), session.calls)
        self.assertTrue(bot.closed)

    def test_cancel_call_marks_queued_work_and_hangs_up_media(self) -> None:
        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        with mock.patch.dict(os.environ, ENV, clear=True):
            session = FakeSession()
            app = standalone_app.StandaloneApp(session=session, bot=FakeBot())
            app._call_thread = AliveThread()  # type: ignore[assignment]
            app._call_user_id = 42
            self.assertTrue(app.cancel_call())
        self.assertTrue(app._call_cancel_event.is_set())
        self.assertIn(("hangup",), session.calls)

    def test_preflight_failure_returns_open_mini_app_to_idle(self) -> None:
        def fail_factory(**_kwargs):
            raise RuntimeError("offline MCP")

        with mock.patch.dict(os.environ, ENV, clear=True):
            session, bot = FakeSession(), FakeBot()
            app = standalone_app.StandaloneApp(
                session=session, bot=bot, agent_factory=fail_factory
            )
            app._call_worker(42, "check tools")

        self.assertEqual(session.transport.failed_requests, [42])
        self.assertTrue(any("n8n MCP" in item[1] for item in bot.messages))


class LifecycleTests(unittest.TestCase):
    def test_run_starts_gateway_polls_once_and_shuts_down(self) -> None:
        class OneUpdateBot(FakeBot):
            app = None

            def call(self, method, payload=None, *, timeout=35.0):
                self.calls.append((method, payload, timeout))
                if method == "getUpdates":
                    self.app.stop_event.set()
                    return [update("/status")]
                return True

        with mock.patch.dict(os.environ, ENV, clear=True):
            session, bot = FakeSession(), OneUpdateBot()
            app = standalone_app.StandaloneApp(session=session, bot=bot)
            bot.app = app
            app.run()

        self.assertEqual(session.calls[0], ("start_lib",))
        methods = [item[0] for item in bot.calls]
        self.assertEqual(methods[:3], ["deleteWebhook", "setChatMenuButton", "setMyCommands"])
        self.assertEqual(bot.calls[0][1], {"drop_pending_updates": True})
        self.assertIn("getUpdates", methods)
        self.assertTrue(any("Standalone-ассистент" in item[1] for item in bot.messages))
        self.assertIn(("shutdown",), session.calls)
        self.assertTrue(bot.closed)


class MiniAppRequestTests(unittest.TestCase):
    def test_authenticated_request_call_signal_uses_registered_hook(self) -> None:
        transport = WebRTCTransport()
        requested: list[int] = []
        transport.set_call_request_handler(
            lambda user_id: requested.append(user_id) is None
        )

        class FakeWS:
            def __init__(self) -> None:
                self.messages = []

            async def send_json(self, value) -> None:
                self.messages.append(value)

        ws = FakeWS()
        asyncio.run(
            transport._handle_ws_message(ws, 42, {"type": "request_call"})
        )
        self.assertEqual(requested, [42])
        self.assertEqual(
            ws.messages,
            [{"type": "call_request", "accepted": True, "reason": "queued"}],
        )

    def test_notification_targets_requested_private_user(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "offline",
                "TELEGRAM_CHAT_ID": "100",
                "WEBRTC_PUBLIC_URL": "https://voice.example.invalid",
            },
            clear=True,
        ):
            transport = WebRTCTransport()
        sent = []
        transport._bot_call = lambda method, payload: (
            sent.append((method, payload))
            or {"result": {"message_id": 7}}
        )
        context = SimpleNamespace(call_id="call-1", user_id=42, telegram_message_id=None)
        transport._send_call_notification(context)
        self.assertEqual(sent[0][1]["chat_id"], 42)
        self.assertEqual(context.telegram_message_id, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
