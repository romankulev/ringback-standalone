#!/usr/bin/env python3
"""Standalone Telegram/WebRTC voice assistant powered by OpenAI and n8n MCP.

This process is the application host.  It serves the Telegram Mini App and
WebRTC media, long-polls the Telegram Bot API, runs speech-to-text and
text-to-speech, and lets the OpenAI model call remote n8n MCP tools.  It
does not expose an MCP server and has no IDE or external agent-host dependency.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import httpx

APP_ROOT = Path(__file__).resolve().parent
ENV_FILE = APP_ROOT / ".env"
LOG = logging.getLogger("ringback-standalone")


def configure_logging() -> None:
    """Configure application logs without exposing credential-bearing URLs."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Telegram puts the bot token in the request path.  httpx/httpcore log the
    # complete URL for successful requests at INFO, so disable their internal
    # loggers and keep only our sanitized application-level messages.
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True


def load_env(path: Path = ENV_FILE) -> None:
    """Load a simple dotenv file without ever printing its values.

    Existing process variables win, which makes container/secret-manager
    injection predictable.  Quotes and ``$HOME``-style references are
    supported for compatibility with the supplied ``.env.example``.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        name = key.strip()
        if not name or name in os.environ:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = os.path.expandvars(os.path.expanduser(value))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _allowed_user_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.replace(",", " ").replace(";", " ").split():
        try:
            result.add(int(item))
        except ValueError:
            continue
    return result


def _has_mcp_servers(raw: str | None = None) -> bool:
    """Return True only for a non-empty, secure server configuration."""
    source = (raw if raw is not None else os.environ.get("MCP_SERVERS_JSON", "")).strip()
    try:
        from remote_mcp import load_server_configs

        configs = load_server_configs(source or "[]")
    except (ValueError, TypeError):
        return False
    return bool(configs)


class TelegramBotError(RuntimeError):
    """A sanitized Bot API failure which never includes the token-bearing URL."""


class TelegramBotClient:
    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        if not token.strip():
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self._token = token.strip()
        self._base_url = f"https://api.telegram.org/bot{self._token}"
        self._client = client or httpx.Client(timeout=httpx.Timeout(35.0))
        self._owns_client = client is None

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 35.0,
    ) -> Any:
        try:
            response = self._client.post(
                f"{self._base_url}/{method}", json=payload or {}, timeout=timeout
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # httpx errors may contain the request URL (and therefore the bot
            # token), so expose only the exception class.
            raise TelegramBotError(
                f"Telegram method {method} failed ({type(exc).__name__})"
            ) from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = str(body.get("description", "Bot API error")) if isinstance(body, dict) else "Bot API error"
            description = description.replace(self._token, "[secret]")[:300]
            raise TelegramBotError(f"Telegram method {method} failed: {description}")
        return body.get("result")

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        web_app_url: str = "",
        web_app_label: str = "Открыть Ringback",
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if web_app_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": web_app_label, "web_app": {"url": web_app_url}}
                ]]
            }
        return self.call("sendMessage", payload, timeout=15.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class StandaloneApp:
    """Own the complete server lifecycle and serialize all voice calls."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        bot: Any | None = None,
        agent_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.public_url = os.environ.get("WEBRTC_PUBLIC_URL", "").strip().rstrip("/")
        self.default_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.allowed_users = _allowed_user_ids(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
        )
        self.default_objective = os.environ.get(
            "STANDALONE_OBJECTIVE",
            "Помочь пользователю, используя актуальные данные из n8n MCP.",
        ).strip()
        self.greeting = os.environ.get(
            "STANDALONE_GREETING",
            "Здравствуйте! Я голосовой ассистент. Чем могу помочь?",
        ).strip()
        self.answer_timeout = float(os.environ.get("STANDALONE_ANSWER_TIMEOUT", "45"))
        self.max_turns = max(1, int(os.environ.get("AUTONOMOUS_MAX_TURNS", "12")))
        self.silence_retries = max(0, int(os.environ.get("STANDALONE_SILENCE_RETRIES", "2")))
        self.poll_timeout = max(1, min(50, int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "20"))))
        # Old queued /call updates must not unexpectedly ring a user after a
        # server restart.  Operators can explicitly retain them when needed.
        self.drop_pending_updates = _env_bool("TELEGRAM_DROP_PENDING_UPDATES", True)

        if session is None:
            # Import only after main() has loaded .env: voice_agent reads its
            # model paths and timing settings at import time.
            from voice_agent import CallSession

            session = CallSession()
        if agent_factory is None:
            from openai_agent import OpenAIAgent

            agent_factory = OpenAIAgent
        self.session = session
        self.bot = bot or TelegramBotClient(self.bot_token)
        self.agent_factory = agent_factory
        self.stop_event = threading.Event()
        self._call_lock = threading.RLock()
        self._call_thread: threading.Thread | None = None
        self._call_user_id: int | None = None
        self._call_cancel_event = threading.Event()
        self._closed = False

        transport = getattr(self.session, "transport", None)
        set_handler = getattr(transport, "set_call_request_handler", None)
        if callable(set_handler):
            set_handler(self._request_call_from_mini_app)

    @staticmethod
    def configuration_errors() -> list[str]:
        errors: list[str] = []
        required = (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_ALLOWED_USER_IDS",
            "VOICE_TELEGRAM_USER_ID",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
        )
        for name in required:
            if not os.environ.get(name, "").strip():
                errors.append(f"{name} is not configured")
        public_url = os.environ.get("WEBRTC_PUBLIC_URL", "").strip()
        dev_mode = _env_bool("TELEGRAM_DEV_MODE")
        if not public_url:
            errors.append("WEBRTC_PUBLIC_URL is not configured")
        elif not dev_mode and not public_url.startswith("https://"):
            errors.append("WEBRTC_PUBLIC_URL must use HTTPS")
        if not _allowed_user_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")):
            errors.append("TELEGRAM_ALLOWED_USER_IDS has no valid numeric user id")
        if not _has_mcp_servers():
            errors.append("MCP_SERVERS_JSON has no active n8n MCP server")
        policy = os.environ.get("MCP_TOOL_POLICY", "read_only").strip().lower()
        if policy != "read_only":
            errors.append("MCP_TOOL_POLICY must be read_only")
        stt = os.environ.get("VOICE_STT", "auto").strip().lower()
        tts = os.environ.get("VOICE_TTS", "auto").strip().lower()
        eleven_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if stt in {"elevenlabs", "eleven-labs", "scribe"} and not eleven_key:
            errors.append("ELEVENLABS_API_KEY is required for cloud speech-to-text")
        if tts in {"elevenlabs", "eleven-labs"}:
            if not eleven_key:
                errors.append("ELEVENLABS_API_KEY is required for cloud text-to-speech")
            if not os.environ.get("ELEVENLABS_VOICE_ID", "").strip():
                errors.append("ELEVENLABS_VOICE_ID is required for cloud text-to-speech")
        return errors

    @property
    def busy(self) -> bool:
        with self._call_lock:
            thread = self._call_thread
            return bool(thread and thread.is_alive())

    def _is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed_users

    def _request_call_from_mini_app(self, user_id: int) -> bool:
        return self.request_call(user_id, source="mini_app")

    def request_call(
        self,
        user_id: int,
        *,
        objective: str = "",
        source: str = "telegram",
    ) -> bool:
        """Queue one call for an allowed user; refuse every concurrent request."""
        if self.stop_event.is_set() or not self._is_allowed(user_id):
            return False
        objective = (objective.strip() or self.default_objective)[:1000]
        with self._call_lock:
            if self._call_thread and self._call_thread.is_alive():
                return False
            self._call_cancel_event.clear()
            thread = threading.Thread(
                target=self._call_worker,
                args=(user_id, objective),
                name=f"ringback-call-{user_id}",
                daemon=True,
            )
            self._call_thread = thread
            self._call_user_id = user_id
            LOG.info("Call queued from %s", source)
            thread.start()
        return True

    def _safe_notify(self, chat_id: int | str, text: str) -> None:
        try:
            self.bot.send_message(chat_id, text)
        except Exception as exc:
            LOG.warning("Telegram notification failed (%s)", type(exc).__name__)

    def _call_worker(self, user_id: int, objective: str) -> None:
        agent = None
        answered = False
        try:
            # Discover n8n MCP tools before ringing.  A broken MCP endpoint then
            # fails cleanly in chat instead of leaving the user in a dead call.
            agent = self.agent_factory(objective=objective)
            if self.stop_event.is_set() or self._call_cancel_event.is_set():
                return
            answered = bool(
                self.session.place_call(
                    answer_timeout=self.answer_timeout,
                    callee=f"telegram:{user_id}",
                )
            )
            if not answered:
                self._safe_notify(user_id, "Звонок не был принят. Можно попробовать ещё раз командой /call.")
                return

            self.session.speak(self.greeting)
            user_text = self.session.listen()
            silent = 0
            turns = 0
            while not self.stop_event.is_set() and not self.session.disconnected:
                if not user_text:
                    silent += 1
                    if silent > self.silence_retries:
                        self.session.speak("Я вас не слышу, поэтому завершаю звонок. До свидания!")
                        break
                    self.session.speak("Не расслышала. Повторите, пожалуйста.")
                    user_text = self.session.listen()
                    continue

                silent = 0
                answer, end_call = agent.reply(user_text)
                turns += 1
                answer = answer.strip() or "Извините, не смогла сформулировать ответ."
                if end_call or turns >= self.max_turns:
                    self.session.speak(answer)
                    break
                turn = self.session.speak_interruptible(answer, listen_after=True)
                if turn.get("ended"):
                    break
                user_text = str(turn.get("user") or "").strip()
        except Exception as exc:
            # Log only the class: provider errors can contain credential-bearing
            # request URLs or headers in their string representation.
            LOG.error("Call worker failed (%s)", type(exc).__name__)
            if answered and not self.session.disconnected:
                try:
                    self.session.speak(
                        "Извините, сервис сейчас недоступен. Попробуйте позже."
                    )
                except Exception:
                    pass
            else:
                notify_failed = getattr(
                    getattr(self.session, "transport", None),
                    "notify_call_request_failed",
                    None,
                )
                if callable(notify_failed):
                    notify_failed(user_id)
                self._safe_notify(
                    user_id,
                    "Не удалось подготовить голосовой вызов. Проверьте OpenAI и n8n MCP.",
                )
        finally:
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    pass
            try:
                self.session.hangup()
            except Exception:
                pass
            with self._call_lock:
                self._call_user_id = None
                self._call_thread = None

    def _mini_app_payload(self) -> tuple[str, str]:
        return (
            "Ringback работает самостоятельно.\n\n"
            "Команды:\n"
            "/call — позвонить ассистенту\n"
            "/status — проверить состояние\n"
            "/hangup — завершить звонок",
            f"{self.public_url}/",
        )

    def process_update(self, update: dict[str, Any]) -> None:
        """Process one Bot API update.  Public for deterministic offline tests."""
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private":
            return
        try:
            user_id = int(sender.get("id"))
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return
        if not self._is_allowed(user_id):
            LOG.warning("Ignored a Telegram command from an unauthorized user")
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        first, _, tail = text.partition(" ")
        command = first.split("@", 1)[0].lower()

        if command in {"/start", "/help"}:
            help_text, url = self._mini_app_payload()
            self.bot.send_message(chat_id, help_text, web_app_url=url)
        elif command == "/call":
            if self.request_call(user_id, objective=tail, source="telegram"):
                self.bot.send_message(
                    chat_id,
                    "Готовлю звонок. Откройте уведомление Ringback и нажмите «Ответить».",
                )
            else:
                self.bot.send_message(chat_id, "Сейчас уже идёт или готовится другой звонок.")
        elif command == "/hangup":
            if self.cancel_call(requester_id=user_id):
                self.bot.send_message(chat_id, "Звонок завершается.")
            elif self.busy:
                self.bot.send_message(chat_id, "Нельзя завершить звонок другого пользователя.")
            else:
                self.bot.send_message(chat_id, "Сейчас нет активного звонка.")
        elif command == "/status":
            state = "занят" if self.busy else "готов"
            transport_status = str(self.session.status())
            self.bot.send_message(chat_id, f"Standalone-ассистент: {state}.\n{transport_status}")

    def _configure_bot(self) -> None:
        # Long polling cannot coexist with a Telegram webhook.
        self.bot.call(
            "deleteWebhook",
            {"drop_pending_updates": self.drop_pending_updates},
            timeout=15.0,
        )
        if self.default_chat_id and self.public_url:
            self.bot.call(
                "setChatMenuButton",
                {
                    "chat_id": self.default_chat_id,
                    "menu_button": {
                        "type": "web_app",
                        "text": "Ringback",
                        "web_app": {"url": f"{self.public_url}/"},
                    },
                },
                timeout=15.0,
            )
        self.bot.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "call", "description": "Позвонить ассистенту"},
                    {"command": "status", "description": "Состояние сервиса"},
                    {"command": "hangup", "description": "Завершить звонок"},
                ]
            },
            timeout=15.0,
        )

    def run(self) -> None:
        """Start WebRTC and consume Telegram updates until shutdown."""
        self.session.start_lib()
        self._configure_bot()
        LOG.info("Standalone server started")
        offset: int | None = None
        failures = 0
        try:
            while not self.stop_event.is_set():
                payload: dict[str, Any] = {
                    "timeout": self.poll_timeout,
                    "allowed_updates": ["message"],
                }
                if offset is not None:
                    payload["offset"] = offset
                try:
                    updates = self.bot.call(
                        "getUpdates", payload, timeout=float(self.poll_timeout + 10)
                    )
                    failures = 0
                except Exception as exc:
                    failures += 1
                    LOG.warning("Telegram polling failed (%s)", type(exc).__name__)
                    self.stop_event.wait(min(10.0, float(2 ** min(failures, 3))))
                    continue
                for update in updates if isinstance(updates, list) else []:
                    try:
                        update_id = int(update.get("update_id"))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    offset = update_id + 1
                    try:
                        self.process_update(update)
                    except Exception as exc:
                        LOG.warning("Telegram update failed (%s)", type(exc).__name__)
        finally:
            self.shutdown()

    def request_shutdown(self) -> None:
        self.stop_event.set()
        self.cancel_call()

    def cancel_call(self, requester_id: int | None = None) -> bool:
        """Cancel a call, optionally only when ``requester_id`` owns it."""
        with self._call_lock:
            thread = self._call_thread
            if not thread or not thread.is_alive():
                return False
            if requester_id is not None and requester_id != self._call_user_id:
                return False
            self._call_cancel_event.set()
        try:
            self.session.hangup()
        except Exception:
            pass
        return True

    def shutdown(self) -> None:
        with self._call_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._call_thread
        self.request_shutdown()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10.0)
        try:
            self.session.shutdown()
        finally:
            try:
                self.bot.close()
            except Exception:
                pass
        LOG.info("Standalone server stopped")


def _print_check() -> int:
    errors = StandaloneApp.configuration_errors()
    checks = {
        "Telegram bot": "TELEGRAM_BOT_TOKEN" not in " ".join(errors),
        "Telegram user": not any("TELEGRAM_" in item and "BOT_TOKEN" not in item for item in errors),
        "Mini App HTTPS": not any("WEBRTC_PUBLIC_URL" in item for item in errors),
        "OpenAI": not any("OPENAI" in item for item in errors),
        "ElevenLabs speech": not any("ELEVENLABS" in item for item in errors),
        "n8n MCP config": _has_mcp_servers(),
    }
    for label, ok in checks.items():
        print(f"{'OK' if ok else 'MISSING':7} {label}")
    if errors or not checks["n8n MCP config"]:
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="check .env without contacting Telegram, OpenAI, or n8n",
    )
    args = parser.parse_args(argv)
    load_env()
    if args.check:
        return _print_check()
    errors = StandaloneApp.configuration_errors()
    if errors:
        for item in errors:
            print(f"Configuration error: {item}", file=sys.stderr)
        return 2

    configure_logging()
    app = StandaloneApp()

    def stop(_signum, _frame) -> None:
        app.request_shutdown()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
    try:
        app.run()
    except KeyboardInterrupt:
        app.request_shutdown()
    except Exception as exc:
        LOG.error("Standalone server failed (%s)", type(exc).__name__)
        app.shutdown()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
