#!/usr/bin/env python3
"""One command to verify the WebRTC voice engine OFFLINE — no phone call.

    python3 tests/run_all.py

The default suite is deliberately deterministic and dependency-light: Telegram auth,
ICE parsing, PCM snapshots/RMS, the remote Streamable-HTTP n8n MCP client, and an
OpenAI tool round are checked with local data and fakes. It never needs Telegram,
a microphone, bot/API keys, an MCP host installation, whisper.cpp, aiortc, or network
access.

The historical whisper/AEC/harvest probes remain available as individual scripts for
manual media diagnostics; they are not suitable as a clean-install unit-test gate.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SYS = sys.executable

SUITE = [
    ("WebRTC auth, ICE, and inbound PCM", "test_webrtc_transport.py", SYS),
    ("Standalone Telegram/OpenAI application", "test_standalone_app.py", SYS),
    ("Remote MCP configuration, SSE session, and tool policy", "test_remote_mcp.py", SYS),
    ("OpenAI MCP tool-call round", "test_openai_agent.py", SYS),
    ("ElevenLabs cloud speech-to-text", "test_cloud_stt.py", SYS),
]


def main():
    results = []
    for name, script, py in SUITE:
        print(f"\n{'=' * 70}\n# {name}\n{'=' * 70}")
        rc = subprocess.run([py, "-u", os.path.join(HERE, script)]).returncode
        results.append((name, rc == 0))
    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    allok = all(ok for _, ok in results)
    print("\nOFFLINE RESULT:", "ALL PASS" if allok else "FAILURES ABOVE")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
