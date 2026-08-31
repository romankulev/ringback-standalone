"""platform_compat.py — OS and speech-provider seams for the standalone app.

The standalone voice engine is otherwise platform-neutral; OS-specific bits live
here so the application and WebRTC layers stay clean:
  - detached_popen_kwargs(): spawn a child that outlives the parent (POSIX vs Windows)
  - lib_path_var():          name of the dynamic-linker search-path env var per OS
  - hid_idle_seconds():      seconds since last user input (presence detection)
  - transcribe_wav_elevenlabs(): WAV -> text through ElevenLabs Scribe
  - synthesize_to_wav():     text -> 16 kHz mono 16-bit WAV via ElevenLabs,
                             optional Piper, say, espeak, SAPI, or custom command

Nothing here imports the WebRTC stack, so it is safe to import anywhere.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import httpx

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt" or sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")

# Piper neural TTS (cross-platform default). "Available" means the binary is on PATH
# AND the voice model file exists; otherwise we transparently fall back to the OS voice.
PIPER_BIN = os.environ.get("VOICE_PIPER_BIN", "piper")
PIPER_MODEL = os.environ.get(
    "VOICE_PIPER_MODEL", os.path.expanduser("~/.piper-voices/ru_RU-irina-medium.onnx"))

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _temp_path(suffix: str) -> str:
    """Reserve a private temporary path without the race inherent in ``mktemp``."""
    fd, path = tempfile.mkstemp(prefix="ringback-", suffix=suffix)
    os.close(fd)
    return path


# ---- speech-to-text ---------------------------------------------------------
async def transcribe_wav_elevenlabs(
    wav_path: str,
    *,
    language_code: str | None = "ru",
    client: httpx.AsyncClient | None = None,
) -> str:
    """Transcribe one WAV with ElevenLabs' asynchronous Scribe REST API.

    The caller may supply an ``httpx.AsyncClient`` (used by offline tests and by
    applications that pool connections).  File reading is sent to a worker thread,
    and the HTTP request is asynchronous, so this function never blocks its event
    loop.  Errors deliberately omit request URLs, headers and response bodies because
    provider diagnostics can contain credentials or customer audio metadata.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    model_id = os.environ.get("ELEVENLABS_STT_MODEL_ID", "").strip() or "scribe_v2"
    if not api_key:
        raise RuntimeError(
            "VOICE_STT=elevenlabs requires ELEVENLABS_API_KEY"
        )
    if not os.path.isfile(wav_path):
        raise RuntimeError("ElevenLabs STT audio file is unavailable")

    try:
        audio = await asyncio.to_thread(_read_bytes, wav_path)
    except OSError as exc:
        raise RuntimeError(
            f"ElevenLabs STT could not read audio ({type(exc).__name__})"
        ) from None

    data = {
        "model_id": model_id,
        "tag_audio_events": "false",
        "diarize": "false",
        "timestamps_granularity": "none",
        "file_format": "other",
    }
    normalized_language = (language_code or "").strip().lower()
    if normalized_language and normalized_language != "auto":
        data["language_code"] = normalized_language
    files = {"file": ("turn.wav", audio, "audio/wav")}
    headers = {"xi-api-key": api_key}

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        response = await http.post(
            ELEVENLABS_STT_URL,
            headers=headers,
            data=data,
            files=files,
        )
    except Exception as exc:
        raise RuntimeError(
            f"ElevenLabs STT request failed ({type(exc).__name__})"
        ) from None
    finally:
        if owns_client:
            try:
                await http.aclose()
            except Exception as exc:
                raise RuntimeError(
                    f"ElevenLabs STT connection cleanup failed ({type(exc).__name__})"
                ) from None

    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"ElevenLabs STT returned HTTP {response.status_code}")
    try:
        payload = response.json()
        text = payload.get("text") if isinstance(payload, dict) else None
    except Exception:
        text = None
    if not isinstance(text, str):
        raise RuntimeError("ElevenLabs STT returned an invalid response")
    return text


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as src:
        return src.read()


def transcribe_wav_elevenlabs_sync(
    wav_path: str,
    *,
    language_code: str | None = "ru",
) -> str:
    """Synchronous adapter for the dedicated call-worker thread.

    ``StandaloneApp`` runs every ``CallSession`` on its own worker thread.  Refuse
    use from an active asyncio loop so an accidental direct call cannot freeze the
    Telegram/WebRTC event loop; async callers use :func:`transcribe_wav_elevenlabs`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            transcribe_wav_elevenlabs(wav_path, language_code=language_code)
        )
    raise RuntimeError(
        "ElevenLabs STT sync adapter cannot run inside an active event loop"
    )


# ---- detached child processes ------------------------------------------------
def detached_popen_kwargs() -> dict:
    """subprocess.Popen kwargs to fully detach a child so it outlives this process and
    isn't reaped when we exit (whisper-server, the call driver). POSIX starts a
    new session; Windows uses detached / new-process-group creation flags."""
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) \
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


# ---- dynamic-linker search path ----------------------------------------------
def lib_path_var() -> str:
    """Name of the env var the OS uses to find shared libraries at runtime."""
    if IS_MAC:
        return "DYLD_LIBRARY_PATH"
    if IS_WIN:
        return "PATH"            # Windows resolves DLLs via PATH
    return "LD_LIBRARY_PATH"     # Linux / other Unix


# ---- presence / idle detection -----------------------------------------------
def hid_idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input. Large => the user is away.

    The watchdog/stop-hook uses this to gate phone escalation (only escalate when away).
    Returns 0.0 ("present" — the conservative, do-not-escalate default) when idle can't
    be determined (e.g. Wayland, headless). Force it with RINGBACK_PRESENCE=present|absent.
    """
    override = os.environ.get("RINGBACK_PRESENCE", "").strip().lower()
    if override == "present":
        return 0.0
    if override == "absent":
        return 1e9               # effectively "always away"
    if IS_MAC:
        return _idle_macos()
    if IS_WIN:
        return _idle_windows()
    return _idle_linux()


def _idle_macos() -> float:
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                return int(line.split("=")[-1].strip()) / 1e9
    except Exception:
        pass
    return 0.0


def _idle_windows() -> float:
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
            return max(0.0, millis / 1000.0)
    except Exception:
        pass
    return 0.0


def _idle_linux() -> float:
    # X11: xprintidle prints idle time in milliseconds.
    exe = shutil.which("xprintidle")
    if exe:
        try:
            out = subprocess.run([exe], capture_output=True, text=True, timeout=5).stdout.strip()
            return max(0.0, int(out) / 1000.0)
        except Exception:
            pass
    # GNOME (Mutter) idle monitor over D-Bus -> milliseconds.
    gdbus = shutil.which("gdbus")
    if gdbus:
        try:
            out = subprocess.run(
                [gdbus, "call", "--session",
                 "--dest", "org.gnome.Mutter.IdleMonitor",
                 "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                 "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                capture_output=True, text=True, timeout=5).stdout
            digits = "".join(ch for ch in out if ch.isdigit())
            if digits:
                return max(0.0, int(digits) / 1000.0)
        except Exception:
            pass
    # Wayland / headless: no standard idle source. Treat as present (don't auto-escalate)
    # unless RINGBACK_PRESENCE=absent was set above.
    return 0.0


# ---- text-to-speech ----------------------------------------------------------
def piper_available() -> bool:
    return bool(shutil.which(PIPER_BIN)) and os.path.exists(PIPER_MODEL)


def tts_engine() -> str:
    """Select TTS. VOICE_TTS may be piper, elevenlabs, say, espeak, or sapi.

    ``auto`` deliberately prefers free local speech so merely adding an ElevenLabs
    key never starts billable requests without an explicit VOICE_TTS=elevenlabs.
    """
    choice = os.environ.get("VOICE_TTS", "auto").strip().lower()
    if choice and choice != "auto":
        return choice
    if piper_available():
        return "piper"
    if IS_MAC:
        return "say"
    if IS_WIN:
        return "sapi"
    return "espeak"


def _ffmpeg_to_16k_mono(src: str, dst: str) -> None:
    """Normalize any TTS output for STT and the WebRTC audio bridge."""
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src,
                    "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", dst], check=True)


def _synth_say(text: str) -> str:
    aiff = _temp_path(".aiff")
    subprocess.run(["say", "-o", aiff, text], check=True)
    return aiff


def _synth_piper(text: str) -> str:
    wav = _temp_path(".wav")
    # piper reads the text on stdin and writes a WAV to -f/--output_file. The matching
    # <model>.onnx.json config must sit next to the .onnx model.
    subprocess.run([PIPER_BIN, "-m", PIPER_MODEL, "-f", wav],
                   input=text, text=True, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return wav


def _synth_espeak(text: str) -> str:
    wav = _temp_path(".wav")
    exe = shutil.which("espeak-ng") or shutil.which("espeak") or "espeak-ng"
    subprocess.run([exe, "-w", wav, text], check=True)
    return wav


def _synth_elevenlabs(text: str) -> str:
    """Generate one complete utterance with ElevenLabs' synchronous TTS API."""
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", "").strip() or "eleven_multilingual_v2"
    if not api_key or not voice_id:
        raise RuntimeError(
            "VOICE_TTS=elevenlabs requires ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID"
        )
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        + urllib.parse.quote(voice_id, safe="")
        + "?output_format=mp3_44100_128"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": text, "model_id": model_id}).encode("utf-8"),
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    mp3 = _temp_path(".mp3")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(mp3, "wb") as out:
            out.write(response.read())
    except Exception as exc:
        _rm(mp3)
        # Never propagate urllib's provider/request representation: keep auth headers
        # and response bodies out of application logs.
        raise RuntimeError(
            f"ElevenLabs TTS request failed ({type(exc).__name__})"
        ) from None
    return mp3


def _synth_sapi(text: str) -> str:
    wav = _temp_path(".wav")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        "$s.Speak([Console]::In.ReadToEnd()); "
        "$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   input=text, text=True, check=True)
    return wav


def _synth_custom(template: str, text: str) -> str:
    """VOICE_TTS_CMD is a command template with {text} and {out} placeholders, e.g.
    'mytts --say {text} --wav {out}'. It must write a WAV file to {out}."""
    wav = _temp_path(".wav")
    cmd = [a.replace("{out}", wav).replace("{text}", text) for a in shlex.split(template)]
    subprocess.run(cmd, check=True)
    return wav


def _dispatch(engine: str, text: str) -> str:
    if engine == "piper":
        return _synth_piper(text)
    if engine == "say":
        return _synth_say(text)
    if engine in ("elevenlabs", "eleven-labs"):
        return _synth_elevenlabs(text)
    if engine in ("espeak", "espeak-ng"):
        return _synth_espeak(text)
    if engine == "sapi":
        return _synth_sapi(text)
    raise RuntimeError(f"unknown VOICE_TTS engine: {engine!r}")


def _os_native_engine() -> str:
    return "say" if IS_MAC else ("sapi" if IS_WIN else "espeak")


def synthesize_to_wav(text: str, out_wav: str) -> str:
    """Render ``text`` to a 16 kHz mono 16-bit PCM WAV at ``out_wav``.

    Engine selected by VOICE_TTS (default 'auto': Piper if installed, else the OS-native
    voice). VOICE_TTS_CMD overrides everything with a custom command template. If the
    selected engine FAILS at runtime, we fall back to the OS-native voice so TTS never
    hard-fails — e.g. a misconfigured Piper on macOS still degrades to `say`.
    """
    custom = os.environ.get("VOICE_TTS_CMD", "").strip()
    if custom:
        produced = _synth_custom(custom, text)
    else:
        engine = tts_engine()
        try:
            produced = _dispatch(engine, text)
        except Exception:
            fallback = _os_native_engine()
            if engine == fallback:
                raise
            produced = _dispatch(fallback, text)
    try:
        _ffmpeg_to_16k_mono(produced, out_wav)
    finally:
        _rm(produced)
    return out_wav
