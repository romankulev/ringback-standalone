#!/usr/bin/env python3
"""Pair a Telegram user and configure the Ringback Mini App menu button.

The script reads the gitignored ``.env`` file, never prints the bot token, and
can write discovered numeric IDs back without touching any other secret.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import httpx

APP = Path(__file__).resolve().parent
ENV_FILE = APP / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = os.path.expandvars(
            os.path.expanduser(value.strip().strip('"').strip("'"))
        )


def bot_call(method: str, payload: dict | None = None) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty in .env")
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload or {},
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # httpx includes the request URL in its exception text. Telegram puts
        # the bot token in that URL, so expose only the status code.
        raise RuntimeError(
            f"Telegram Bot API returned HTTP {exc.response.status_code}"
        ) from None
    except httpx.RequestError:
        raise RuntimeError("Telegram Bot API is unreachable") from None
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError("Telegram Bot API returned invalid JSON") from None
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram Bot API error"))
    return body["result"]


def replace_env_values(values: dict[str, str], path: Path = ENV_FILE) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f'{key}="{remaining.pop(key)}"')
                continue
        output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.extend(f'{key}="{value}"' for key, value in remaining.items())
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


def find_private_user(
    updates: list[dict], pairing_code: str
) -> tuple[int, int, str] | None:
    expected = pairing_code.strip()
    if not expected:
        return None
    for update in reversed(updates):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = str(message.get("text") or "").strip()
        command, _, payload = text.partition(" ")
        is_pairing = command.split("@", 1)[0].lower() == "/start" and payload == expected
        if (
            is_pairing
            and chat.get("type") == "private"
            and sender.get("id")
            and chat.get("id")
        ):
            name = sender.get("username") or sender.get("first_name") or "Telegram user"
            return int(sender["id"]), int(chat["id"]), str(name)
    return None


def cmd_discover(write: bool) -> int:
    pairing_code = os.environ.get("TELEGRAM_PAIRING_CODE", "").strip()
    if not pairing_code:
        pairing_code = secrets.token_urlsafe(18)
        replace_env_values({"TELEGRAM_PAIRING_CODE": pairing_code})
        print("Создан одноразовый код привязки. Отправьте боту точную команду:")
        print(f"/start {pairing_code}")
        print("Затем снова запустите discover --write.")
        return 2
    updates = bot_call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    found = find_private_user(updates, pairing_code)
    if not found:
        print("Команда с текущим кодом привязки не найдена. Отправьте боту:")
        print(f"/start {pairing_code}")
        return 2
    user_id, chat_id, name = found
    print(f"Found {name}: user_id={user_id}, chat_id={chat_id}")
    if write:
        replace_env_values(
            {
                "TELEGRAM_CHAT_ID": str(chat_id),
                "TELEGRAM_ALLOWED_USER_IDS": str(user_id),
                "VOICE_TELEGRAM_USER_ID": str(user_id),
                # Rotate immediately so a copied/replayed Telegram update can
                # never authorize a later pairing run.
                "TELEGRAM_PAIRING_CODE": secrets.token_urlsafe(18),
            }
        )
        print("Saved the numeric IDs to .env (the bot token was not changed).")
    return 0


def cmd_configure() -> int:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    public_url = os.environ.get("WEBRTC_PUBLIC_URL", "").strip().rstrip("/")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID is empty; run discover --write first")
    if not public_url.startswith("https://"):
        raise RuntimeError("WEBRTC_PUBLIC_URL must be a public HTTPS URL")

    bot = bot_call("getMe")
    bot_call(
        "setChatMenuButton",
        {
            "chat_id": chat_id,
            "menu_button": {
                "type": "web_app",
                "text": "Ringback",
                "web_app": {"url": public_url + "/"},
            },
        },
    )
    bot_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Ringback подключён. Откройте мини‑апку и разрешите доступ к микрофону.",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "Открыть Ringback", "web_app": {"url": public_url + "/"}}
                ]]
            },
        },
    )
    print(f"Configured the Mini App menu for @{bot.get('username', 'bot')} and sent a test button.")
    return 0


def cmd_status() -> int:
    required = {
        "TELEGRAM_BOT_TOKEN": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        "TELEGRAM_CHAT_ID": bool(os.environ.get("TELEGRAM_CHAT_ID", "").strip()),
        "TELEGRAM_ALLOWED_USER_IDS": bool(
            os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        ),
        "WEBRTC_PUBLIC_URL (HTTPS)": os.environ.get("WEBRTC_PUBLIC_URL", "")
        .strip()
        .startswith("https://"),
    }
    for name, configured in required.items():
        print(f"{'OK' if configured else 'MISSING':7} {name}")
    stt = os.environ.get("VOICE_STT", "auto").strip().lower()
    tts = os.environ.get("VOICE_TTS", "auto").strip().lower()
    eleven_needed = stt in {"elevenlabs", "eleven-labs", "scribe"} or tts in {
        "elevenlabs",
        "eleven-labs",
    }
    eleven = bool(os.environ.get("ELEVENLABS_API_KEY", "").strip()) and (
        tts not in {"elevenlabs", "eleven-labs"}
        or bool(os.environ.get("ELEVENLABS_VOICE_ID", "").strip())
    )
    openai = all(
        os.environ.get(name, "").strip()
        for name in ("OPENAI_API_KEY", "OPENAI_MODEL")
    )
    try:
        from remote_mcp import load_server_configs

        mcp_servers = bool(load_server_configs())
    except (ValueError, TypeError):
        mcp_servers = False
    eleven_status = "OK" if (not eleven_needed or eleven) else "MISSING"
    print(f"{eleven_status:7} ElevenLabs cloud speech")
    print(f"{'OK' if openai else 'MISSING':7} OpenAI standalone brain")
    print(f"{'OK' if mcp_servers else 'MISSING':7} n8n MCP server configuration")
    return 0 if all(required.values()) and openai and mcp_servers and (not eleven_needed or eleven) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="find the latest private /start user")
    discover.add_argument("--write", action="store_true", help="save IDs to .env")
    sub.add_parser("configure", help="set the chat menu button and send a test button")
    sub.add_parser("status", help="show missing configuration without revealing secrets")
    args = parser.parse_args()
    load_env()
    try:
        if args.command == "discover":
            return cmd_discover(args.write)
        if args.command == "configure":
            return cmd_configure()
        return cmd_status()
    except RuntimeError as exc:
        print(f"Configuration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
