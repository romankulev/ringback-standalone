# Установка Ringback Standalone на Windows

Для Windows поддерживаются два практичных варианта: WSL2 и Docker Desktop. Облачный профиль ElevenLabs Scribe v2 + TTS не требует локальных AI-моделей. Нативный Windows-запуск всё равно не рекомендуется для серверного развёртывания.

## WSL2

В PowerShell от администра:

```powershell
wsl --install -d Ubuntu
```

После перезагрузки откройте Ubuntu и выполните:

```bash
cd /path/to/ringback
./setup-linux.sh
```

Заполните `.env`. Обязательны прямой OpenAI key, ElevenLabs API key/voice ID, HTTPS Mini App URL и опубликованный n8n MCP production URL. OpenAI key возьмите из защищённой конфигурации проекта «ИИ телефония»:

```dotenv
OPENAI_API_KEY="..."
OPENAI_MODEL="gpt-5.6-luna"
OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions"
```

[Официальная карточка GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) подтверждает Chat Completions и function calling, так что Responses API для этой реализации не обязателен. Внешний n8n принимается только с HTTPS + Bearer/custom-header авторизацией; legacy URL отвечает без такой защиты и поэтому отклоняется.

Затем запустите pairing первый раз:

```bash
.venv/bin/python configure_telegram.py discover --write
```

Он покажет одноразовую `/start <код>` и завершится с кодом 2. Отправьте боту точно эту команду и повторите:

```bash
.venv/bin/python configure_telegram.py discover --write
.venv/bin/python configure_telegram.py configure
./run_app.sh --check
./run_app.sh
```

Второй запуск запишет Telegram ID и ротирует код. Обычный `/start` без кода не привязывает аккаунт.

Для опционального локального fallback в WSL2 запустите `INSTALL_LOCAL_VOICE=1 ./setup-linux.sh`, затем задайте `VOICE_STT=local` и `VOICE_TTS=piper`.

При постоянном хостинге лучше перенести ту же конфигурацию на Linux VPS: WSL2 зависит от запущенного Windows-компьютера.

## Docker Desktop

Стандартный образ лёгкий: в нём нет whisper.cpp, Piper и локальных моделей. Перед запуском пройдите защищённый Telegram pairing по командам из [Docker-инструкции](SETUP_DOCKER.md).

```powershell
docker build -t ringback-standalone .
docker run -d --name ringback --restart unless-stopped `
  --env-file .env `
  -e WEBRTC_HOST=0.0.0.0 `
  -p 127.0.0.1:8765:8765 `
  ringback-standalone
```

Проверьте `http://127.0.0.1:8765/health`, затем опубликуйте его через HTTPS reverse proxy или защищённый туннель. Для мобильных сетей нужен внешний TURN-сервер; публикация TCP-порта 8765 его не заменяет.

Полный Docker-процесс: [SETUP_DOCKER.md](SETUP_DOCKER.md).

## Приватность

В облачном профиле WAV реплики передаётся ElevenLabs Scribe v2, транскрипт и MCP-данные — напрямую OpenAI API, текст ответа — ElevenLabs TTS. Ringback удаляет временный WAV и освобождает RAM-буферы, но retention на стороне ElevenLabs и OpenAI определяется их условиями и настройками.
