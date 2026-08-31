#!/usr/bin/env python3
"""Offline tests for the optional ElevenLabs Scribe speech-to-text backend."""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import platform_compat as pc  # noqa: E402
import voice_agent as va  # noqa: E402


def _wav_file() -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        # The provider accepts WAV; a complete audio fixture is unnecessary because
        # every request is intercepted before it leaves this process.
        handle.write(b"RIFF" + b"\x00" * 40)
        return handle.name
    finally:
        handle.close()


class ElevenLabsRequestTests(unittest.TestCase):
    def test_async_request_uses_scribe_v2_and_russian(self) -> None:
        wav = _wav_file()
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["key"] = request.headers.get("xi-api-key")
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = request.read()
            return httpx.Response(200, json={"language_code": "ru", "text": "  Привет   мир  "})

        async def run() -> str:
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await pc.transcribe_wav_elevenlabs(
                    wav, language_code="ru", client=client
                )

        try:
            with mock.patch.dict(
                os.environ,
                {
                    "ELEVENLABS_API_KEY": "offline-test-key",
                    "ELEVENLABS_STT_MODEL_ID": "",
                },
                clear=False,
            ):
                result = asyncio.run(run())
        finally:
            os.remove(wav)

        self.assertEqual(result, "  Привет   мир  ")
        self.assertEqual(seen["path"], "/v1/speech-to-text")
        self.assertEqual(seen["key"], "offline-test-key")
        self.assertIn("multipart/form-data", str(seen["content_type"]))
        body = seen["body"]
        self.assertIsInstance(body, bytes)
        self.assertIn(b'name="model_id"', body)
        self.assertIn(b"scribe_v2", body)
        self.assertIn(b'name="language_code"', body)
        self.assertIn(b"\r\nru\r\n", body)
        self.assertIn(b'filename="turn.wav"', body)

    def test_missing_key_fails_before_network(self) -> None:
        wav = _wav_file()
        try:
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "ELEVENLABS_API_KEY"):
                    asyncio.run(pc.transcribe_wav_elevenlabs(wav))
        finally:
            os.remove(wav)

    def test_http_error_does_not_expose_secret_or_response(self) -> None:
        wav = _wav_file()
        secret = "never-log-this-key"
        response_secret = "provider-body-with-customer-data"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text=response_secret)

        async def run() -> str:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await pc.transcribe_wav_elevenlabs(wav, client=client)

        try:
            with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": secret}, clear=False):
                with self.assertRaises(RuntimeError) as caught:
                    asyncio.run(run())
        finally:
            os.remove(wav)

        message = str(caught.exception)
        self.assertEqual(message, "ElevenLabs STT returned HTTP 401")
        self.assertNotIn(secret, message)
        self.assertNotIn(response_secret, message)

    def test_sync_adapter_refuses_active_event_loop(self) -> None:
        async def run() -> None:
            with self.assertRaisesRegex(RuntimeError, "active event loop"):
                pc.transcribe_wav_elevenlabs_sync("unused.wav")

        asyncio.run(run())

    def test_tts_transport_error_does_not_expose_api_key(self) -> None:
        secret = "tts-key-that-must-stay-private"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "ELEVENLABS_API_KEY": secret,
                    "ELEVENLABS_VOICE_ID": "offline-voice",
                },
                clear=False,
            ),
            mock.patch.object(
                pc.urllib.request,
                "urlopen",
                side_effect=RuntimeError(f"provider accidentally echoed {secret}"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                pc._synth_elevenlabs("Привет")
        self.assertEqual(
            str(caught.exception), "ElevenLabs TTS request failed (RuntimeError)"
        )
        self.assertNotIn(secret, str(caught.exception))


class VoiceAgentSelectionTests(unittest.TestCase):
    def test_elevenlabs_dispatch_cleans_text_without_starting_whisper(self) -> None:
        wav = _wav_file()
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {"VOICE_STT": "elevenlabs", "WHISPER_LANGUAGE": "ru"},
                    clear=False,
                ),
                mock.patch.object(
                    va, "transcribe_wav_elevenlabs_sync", return_value="  Да,   свободно.  "
                ) as cloud,
                mock.patch.object(va, "_wsrv_ready", side_effect=AssertionError("local STT used")),
            ):
                result = va._transcribe_stream(wav)
        finally:
            os.remove(wav)

        self.assertEqual(result, "Да, свободно.")
        cloud.assert_called_once_with(wav, language_code=va.WHISPER_LANGUAGE)

    def test_cloud_capture_does_not_make_billable_interim_requests(self) -> None:
        clock = [0.0]

        def now() -> float:
            return clock[0]

        def advance(seconds: float) -> None:
            clock[0] += seconds

        snapshot = mock.Mock(return_value="unused.wav")
        with (
            mock.patch.dict(os.environ, {"VOICE_STT": "elevenlabs"}, clear=False),
            mock.patch.object(va.time, "time", side_effect=now),
            mock.patch.object(va.time, "sleep", side_effect=advance),
            mock.patch.object(
                va, "_transcribe_stream", side_effect=AssertionError("interim cloud request")
            ),
        ):
            result = va._capture_turn(
                snapshot,
                lambda: False,
                max_sec=2.0,
                start_timeout=1.0,
                end_silence=0.3,
                energy_fn=lambda: 0.0,
            )

        self.assertEqual(result, "")
        snapshot.assert_not_called()

    def test_local_and_auto_keep_whisper_backend(self) -> None:
        for value in ("local", "auto"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"VOICE_STT": value}, clear=False
            ):
                self.assertEqual(va._stt_engine(), "local")

    def test_missing_cloud_key_is_rejected_before_ringing(self) -> None:
        session = object.__new__(va.CallSession)
        session.transport = mock.Mock()
        session.log = [{"who": "user", "text": "previous call"}]
        with mock.patch.dict(
            os.environ,
            {"VOICE_STT": "elevenlabs", "ELEVENLABS_API_KEY": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "ELEVENLABS_API_KEY"):
                session.place_call()
        session.transport.place_call.assert_not_called()
        self.assertEqual(session.log, [])

    def test_hangup_erases_transient_conversation_text(self) -> None:
        session = object.__new__(va.CallSession)
        session.transport = mock.Mock(reason="ended")
        session.log = [{"who": "user", "text": "private"}]
        session.hangup()
        self.assertEqual(session.log, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
