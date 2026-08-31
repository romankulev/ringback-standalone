#!/usr/bin/env bash
# One-shot Linux setup for the standalone Telegram Mini App + WebRTC server.
# Supports Debian/Ubuntu (apt) and Fedora/RHEL (dnf).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
VENV_PY="$VENV_DIR/bin/python"
WHISPER_DIR="${WHISPER_DIR:-$HOME/build/whisper.cpp}"
WHISPER_MODEL_DIR="${WHISPER_MODEL_DIR:-$HOME/.whisper-models}"
PIPER_DIR="${PIPER_DIR:-$HOME/.piper-voices}"
PIPER_VOICE="${PIPER_VOICE:-ru_RU-irina-medium}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
INSTALL_LOCAL_VOICE="${INSTALL_LOCAL_VOICE:-0}"

say_step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

local_voice_enabled() {
  case "$(printf '%s' "$INSTALL_LOCAL_VOICE" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

download_model() {
  local url="$1" destination="$2" minimum_bytes="$3"
  local partial="${destination}.part" current_size=0 partial_size=0

  if [ -f "$destination" ]; then
    current_size="$(wc -c < "$destination" | tr -d ' ')"
    if [ "$current_size" -ge "$minimum_bytes" ]; then
      return 0
    fi

    echo "  найден неполный $(basename "$destination"), продолжаю загрузку ..."
    if [ -f "$partial" ]; then
      partial_size="$(wc -c < "$partial" | tr -d ' ')"
    fi
    if [ "$current_size" -gt "$partial_size" ]; then
      mv "$destination" "$partial"
    fi
  fi

  curl -fL --retry 8 --retry-all-errors --retry-delay 2 \
    --connect-timeout 30 --progress-bar -C - "$url" -o "$partial"

  current_size="$(wc -c < "$partial" | tr -d ' ')"
  if [ "$current_size" -lt "$minimum_bytes" ]; then
    echo "Загрузка $(basename "$destination") не завершена ($current_size байт)." >&2
    return 1
  fi
  mv "$partial" "$destination"
}

[ "$(uname)" = "Linux" ] || {
  echo "Этот установщик предназначен для Linux." >&2
  exit 1
}
[ -n "$PYTHON_BIN" ] || {
  echo "Не найден python3." >&2
  exit 1
}

say_step "1/4 — системные зависимости"
if command -v apt-get >/dev/null 2>&1; then
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -qq
  packages=(curl ca-certificates python3 python3-pip python3-venv ffmpeg)
  if local_voice_enabled; then
    packages+=(build-essential cmake git xz-utils pkg-config python3-dev espeak-ng)
  fi
  $SUDO apt-get install -y --no-install-recommends "${packages[@]}"
elif command -v dnf >/dev/null 2>&1; then
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  packages=(curl ca-certificates python3 python3-pip ffmpeg)
  if local_voice_enabled; then
    packages+=(gcc gcc-c++ make cmake git xz pkgconf-pkg-config python3-devel espeak-ng)
  fi
  $SUDO dnf install -y "${packages[@]}"
else
  echo "Установите Python 3 + venv, ffmpeg, curl и CA certificates." >&2
  exit 1
fi

say_step "2/4 — Python-окружение"
if [ ! -x "$VENV_PY" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_PY" -m pip install --quiet --upgrade pip setuptools wheel
"$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt"

say_step "3/4 — голосовой профиль"
if local_voice_enabled; then
  mkdir -p "$LOCAL_BIN" "$(dirname "$WHISPER_DIR")"
  if [ ! -x "$LOCAL_BIN/whisper-cli" ] || [ ! -x "$LOCAL_BIN/whisper-server" ]; then
    if [ ! -d "$WHISPER_DIR/.git" ]; then
      git clone --depth 1 https://github.com/ggerganov/whisper.cpp "$WHISPER_DIR"
    fi
    cmake -S "$WHISPER_DIR" -B "$WHISPER_DIR/build" \
      -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF >/dev/null
    cmake --build "$WHISPER_DIR/build" -j --target whisper-cli whisper-server
    cp "$WHISPER_DIR/build/bin/whisper-cli" \
       "$WHISPER_DIR/build/bin/whisper-server" "$LOCAL_BIN/"
  fi

  mkdir -p "$WHISPER_MODEL_DIR"
  download_model \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin" \
    "$WHISPER_MODEL_DIR/ggml-base.bin" 140000000
  download_model \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin" \
    "$WHISPER_MODEL_DIR/ggml-small.bin" 470000000

  mkdir -p "$PIPER_DIR"
  "$VENV_PY" -m pip install --quiet piper-tts==1.7.0
  piper_base="https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium"
  download_model "$piper_base/$PIPER_VOICE.onnx" \
    "$PIPER_DIR/$PIPER_VOICE.onnx" 50000000
  download_model "$piper_base/$PIPER_VOICE.onnx.json" \
    "$PIPER_DIR/$PIPER_VOICE.onnx.json" 1000
else
  echo "  Облачный профиль ElevenLabs: локальные AI-модели не устанавливаются."
fi

say_step "4/4 — локальная конфигурация"
if [ ! -f "$APP_DIR/.env" ]; then
  install -m 600 "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Создан $APP_DIR/.env"
else
  echo "Существующий $APP_DIR/.env сохранён без изменений"
fi
chmod 600 "$APP_DIR/.env"

cat <<EOF

Установка завершена.

1. Заполните $APP_DIR/.env. Для рабочего звонка обязательны Telegram-токен,
   ID пользователя/чата, публичный HTTPS URL, OPENAI_API_KEY и точная
   модель OPENAI_MODEL, ELEVENLABS_API_KEY и ELEVENLABS_VOICE_ID.
2. Добавьте действующий n8n MCP Trigger URL в MCP_SERVERS_JSON, чтобы ассистент
   видел инструменты YCLIENTS.
3. Запустите защищённую привязку Telegram; помощник выдаст одноразовую
   команду /start, после чего повторите ту же команду:

     $VENV_PY $APP_DIR/configure_telegram.py discover --write

4. Запустите самостоятельное приложение:

     $APP_DIR/run_app.sh

   До запуска можно проверить заполнение без сетевых запросов:

     $APP_DIR/run_app.sh --check

По умолчанию речь обрабатывает ElevenLabs, поэтому локальные AI-модели не
нужны. Для полностью локального резерва повторите установку так:

  INSTALL_LOCAL_VOICE=1 $APP_DIR/setup-linux.sh

Для стабильного WebRTC на мобильных сетях добавьте свой TURN-сервер в
WEBRTC_ICE_SERVERS_JSON.
EOF
