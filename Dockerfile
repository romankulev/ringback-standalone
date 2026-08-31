# Standalone Ringback Telegram Mini App + WebRTC voice server.
#
# Build:
#   docker build -t ringback .
# Run:
#   docker run --env-file .env -p 8765:8765 ringback

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# ffmpeg normalizes ElevenLabs audio for the WebRTC bridge. Speech recognition
# and synthesis run in ElevenLabs, so this image contains no local AI models.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app \
    VOICE_STT=elevenlabs \
    VOICE_TTS=elevenlabs \
    WEBRTC_HOST=0.0.0.0 \
    WEBRTC_PORT=8765 \
    TELEGRAM_DEV_MODE=0

COPY standalone_app.py run_app.py configure_telegram.py ./
COPY voice_agent.py webrtc_transport.py platform_compat.py aec.py ./
COPY openai_agent.py remote_mcp.py ./
COPY web/ ./web/

EXPOSE 8765

RUN useradd --create-home --uid 10001 ringback \
    && chown -R ringback:ringback /app
USER ringback

CMD ["python", "-u", "run_app.py"]
