#!/usr/bin/env python3
"""Pure WebRTC transport tests: no Telegram, media device, secrets, or network."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import struct
import sys
import unittest
import urllib.parse
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from webrtc_transport import (  # noqa: E402
    InboundAudioBuffer,
    _parse_ice_servers,
    validate_telegram_init_data,
)


BOT_TOKEN = "123456:test-only-token"
NOW = 2_000_000_000


def _signed_init_data(*, auth_date: int = NOW, user_id: int = 4242) -> str:
    """Build Telegram initData locally using the documented Web App HMAC."""
    values = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test-query",
        "user": json.dumps(
            {"id": user_id, "first_name": "Test", "username": "offline_user"},
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urllib.parse.urlencode(values)


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_signature_returns_decoded_user(self) -> None:
        payload = validate_telegram_init_data(
            _signed_init_data(), BOT_TOKEN, now=NOW, max_age=300
        )

        self.assertEqual(payload["query_id"], "AAE-test-query")
        self.assertEqual(payload["user"]["id"], 4242)
        self.assertEqual(payload["user"]["username"], "offline_user")

    def test_bad_signature_is_rejected(self) -> None:
        fields = dict(urllib.parse.parse_qsl(_signed_init_data()))
        fields["user"] = json.dumps({"id": 9999})

        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            validate_telegram_init_data(
                urllib.parse.urlencode(fields), BOT_TOKEN, now=NOW, max_age=300
            )

    def test_stale_signed_payload_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "stale"):
            validate_telegram_init_data(
                _signed_init_data(auth_date=NOW - 301),
                BOT_TOKEN,
                now=NOW,
                max_age=300,
            )


class IceConfigurationTests(unittest.TestCase):
    def test_browser_shaped_ice_configuration_is_preserved(self) -> None:
        raw = json.dumps(
            [
                {"urls": "stun:stun.example.test:3478"},
                {
                    "urls": ["turn:turn.example.test:3478?transport=udp"],
                    "username": "ringback",
                    "credential": 12345,
                },
            ]
        )

        self.assertEqual(
            _parse_ice_servers(raw),
            [
                {"urls": "stun:stun.example.test:3478"},
                {
                    "urls": ["turn:turn.example.test:3478?transport=udp"],
                    "username": "ringback",
                    "credential": "12345",
                },
            ],
        )

    def test_empty_configuration_needs_no_turn_service(self) -> None:
        self.assertEqual(_parse_ice_servers("  "), [])

    def test_invalid_configuration_is_rejected(self) -> None:
        for raw in ('{"urls":"stun:example.test"}', '[{"username":"missing"}]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _parse_ice_servers(raw)


class InboundAudioBufferTests(unittest.TestCase):
    def test_cursor_snapshot_and_rms_cover_only_new_pcm(self) -> None:
        audio = InboundAudioBuffer()
        audio.append(struct.pack("<2h", 100, -100))
        cursor = audio.cursor()
        audio.append(struct.pack("<4h", 1200, -1200, 1200, -1200))

        self.assertEqual(audio.bytes_since(cursor), 8)
        self.assertEqual(audio.bytes_since(-100), 12)
        self.assertAlmostEqual(audio.tail_rms(cursor, seconds=1.0), 1200.0)
        self.assertTrue(math.isfinite(audio.tail_rms()))

        snapshot = audio.snapshot(cursor)
        self.addCleanup(lambda: os.path.exists(snapshot) and os.remove(snapshot))
        with wave.open(snapshot, "rb") as captured:
            self.assertEqual(captured.getnchannels(), 1)
            self.assertEqual(captured.getsampwidth(), 2)
            self.assertEqual(captured.getframerate(), 16000)
            self.assertEqual(
                captured.readframes(captured.getnframes()),
                struct.pack("<4h", 1200, -1200, 1200, -1200),
            )

        self.assertEqual(audio.snapshot(audio.cursor()), "")
        self.assertEqual(audio.tail_rms(audio.cursor()), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
