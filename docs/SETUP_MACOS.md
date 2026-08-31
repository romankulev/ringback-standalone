# Установка Ringback Standalone на macOS

## Требования

- macOS 13+;
- Python 3.10+;
- [Homebrew](https://brew.sh/);
- токен Telegram-бота, прямой OpenAI API key, ElevenLabs API key + voice ID;
- публичный HTTPS-домен и защищённый n8n MCP production endpoint.

## Установка

Из корня проекта:

```bash
./setup.sh
```

Скрипт установит только `ffmpeg`, Python-зависимости и создаст `.venv`. Обычный профиль использует ElevenLabs Scribe v2 + TTS, поэтому whisper.cpp, Piper и локальные AI-модели не качаются. Существующий `.env` при повторном запуске не затирается.

## Настройка

1. Заполните `.env`: Telegram bot token, `WEBRTC_PUBLIC_URL`, OpenAI, ElevenLabs key/voice ID и активный n8n MCP Trigger URL. OpenAI key возьмите из защищённой конфигурации проекта «ИИ телефония», не передавая его через чат:

```dotenv
OPENAI_API_KEY="..."
OPENAI_MODEL="gpt-5.6-luna"
OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions"
```

   Официальная [карточка GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) подтверждает Chat Completions и function calling. Responses API для текущей реализации не обязателен.
2. n8n URL должен быть HTTPS и передавать Bearer token или секретный custom header. Legacy URL отвечает без отдельной авторизации, поэтому приложение намеренно его отклоняет; используйте защищённый production Trigger.
3. Первый запус pairing-команды создаст одноразовую `/start <код>`:

```bash
.venv/bin/python configure_telegram.py discover --write
```

Отправьте боту в личном чате точно показанную команду, затем повторите:

```bash
.venv/bin/python configure_telegram.py discover --write
.venv/bin/python configure_telegram.py configure
```

Первый запуск специально возвращает код 2; второй запишет ID и ротирует код. Обычный `/start` без кода не подходит.

4. Проверьте конфигурацию и запустите:

```bash
./run_app.sh --check
./run_app.sh
```

## Локальный fallback

Только если нужен локальный Whisper/Piper:

```bash
INSTALL_LOCAL_VOICE=1 ./setup.sh
```

После установки задайте `VOICE_STT=local` и `VOICE_TTS=piper` в `.env`. Это opt-in профиль, а не часть обычной установки.

## Приватность

В облачном профиле WAV реплики идёт в ElevenLabs Scribe v2; транскрипт, цель звонка и данные MCP — напрямую в OpenAI API; текст ответа — в ElevenLabs TTS. Ringback не ведёт постоянный архив: временный WAV удаляется, а RAM-буферы освобождаются. Retention у ElevenLabs и OpenAI определяется их условиями и настройками аккаунта.

## HTTPS для теста

Для быстрой проверки можно дать локальному `127.0.0.1:8765` публичный HTTPS-адрес через защищённый туннель. Для постоянной работы используйте домен и reverse proxy. Туннель не заменяет TURN-сервер для WebRTC-медиа.

## Диагностика

```bash
curl http://127.0.0.1:8765/health
.venv/bin/python tests/run_all.py
```

Если микрофон не запрашивается, проверьте HTTPS URL и разрешение Telegram на микрофон. Если страница открывается, но звук не соединяется на мобильной сети, добавьте TURN в `WEBRTC_ICE_SERVERS_JSON`.
