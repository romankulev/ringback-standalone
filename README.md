# Ringback Standalone

Ringback — самостоятельный голосовой AI-ассистент для Telegram. Он работает как обычное серверное приложение: получает команды от Telegram-бота, ведёт голосовой разговор в Mini App через WebRTC, напрямую использует OpenAI API и обращается к удалённым n8n MCP-инструментам.

OpenAI является «мозгом» агента, а Ringback сам управляет ботом, звонком, речью и инструментами. Это один независимый серверный процесс без API-агрегатора, IDE-плагина или внешнего AI-хоста.

## Что умеет приложение

- Присылает в Telegram входящий AI-звонок с кнопкой «Ответить».
- Передаёт двусторонний звук между Telegram Mini App и сервером по WebRTC.
- Распознаёт русскую речь через ElevenLabs Scribe v2.
- Отвечает выбранным голосом ElevenLabs.
- Даёт модели OpenAI динамический список function tools из n8n.
- Может получать из YCLIENTS услуги, сотрудников, даты, свободные окна и проверять слот перед ответом.
- Поддерживает barge-in: пользователь может перебить ассистента.

## Архитектура

```text
Telegram Bot / Mini App                n8n MCP servers
          │                              │
          ▼                              ▼
   standalone_app.py ◀───── OpenAI API ───▶ YCLIENTS
          │
          ├── WSS + WebRTC ──────▶ Telegram microphone
          └── ElevenLabs Scribe v2 / TTS
```

Ringback сам является MCP-клиентом для n8n: он вызывает `initialize`, `tools/list` и `tools/call` по Streamable HTTP/SSE. Новые разрешённые ноды можно добавлять в n8n без переписывания Python-кода.

## Что понадобится

Обязательно:

1. Сервер с Python 3.10+ или Docker.
2. Telegram-бот и токен от `@BotFather`.
3. Telegram user/chat ID. Его можно записать в `.env` автоматически.
4. Публичный HTTPS-адрес Mini App, например `https://voice.example.com`.
5. OpenAI API key и модель `gpt-5.6-luna`.
6. ElevenLabs API key и voice ID. По умолчанию Scribe v2 распознаёт речь, а ElevenLabs TTS озвучивает ответ.
7. Актуальный production URL n8n MCP Trigger и обязательная защита: Bearer token или секретный custom header.

Для надёжной работы на мобильных сетях также нужен TURN-сервер. Локальные Whisper/Piper в обычную установку и Docker-образ не входят.

> Legacy n8n MCP URL из проекта «ИИ телефония» отвечает, но опубликован без отдельного Bearer/custom-header токена. Сначала защитите production endpoint, затем внесите URL и токен в `.env`. Не подставляйте test URL.

## Быстрый запуск

### 1. Установить

macOS:

```bash
./setup.sh
```

Ubuntu/Debian или WSL2:

```bash
./setup-linux.sh
```

Скрипт создаст `.venv`, установит лёгкие runtime-зависимости и `ffmpeg`. Локальные AI-модели не загружаются. Если `.env` ещё нет, он будет создан из `.env.example` с правами `0600`.

Детальные инструкции: [macOS](docs/SETUP_MACOS.md), [Linux](docs/SETUP_LINUX.md), [Windows](docs/SETUP_WINDOWS.md), [Docker](docs/SETUP_DOCKER.md).

### 2. Заполнить единый `.env`

Все настройки хранятся в одном корневом `.env`. Он игнорируется Git. Не вставляйте секреты в README, issue, логи или чат.

```dotenv
TELEGRAM_BOT_TOKEN="..."
TELEGRAM_CHAT_ID="123456789"
TELEGRAM_ALLOWED_USER_IDS="123456789"
VOICE_TELEGRAM_USER_ID="123456789"
WEBRTC_PUBLIC_URL="https://voice.example.com"

OPENAI_API_KEY="..."
OPENAI_MODEL="gpt-5.6-luna"
OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions"

ELEVENLABS_API_KEY="..."
ELEVENLABS_VOICE_ID="..."
ELEVENLABS_STT_MODEL_ID="scribe_v2"
ELEVENLABS_MODEL_ID="eleven_multilingual_v2"
VOICE_STT="elevenlabs"
VOICE_TTS="elevenlabs"

MCP_SERVERS_JSON='[{"server_label":"nami_booking","server_url":"https://YOUR-N8N-HOST/mcp/YOUR-CURRENT-ID","authorization":"Bearer ${N8N_MCP_TOKEN}"}]'
N8N_MCP_TOKEN="..."
MCP_TOOL_POLICY="read_only"
MCP_ALLOWED_TOOLS="nami_current_datetime,nami_get_services,nami_get_staff_for_service,nami_get_available_dates,nami_get_available_times,nami_check_slot"
```

Для custom header вместо Bearer:

```dotenv
MCP_SERVERS_JSON='[{"server_label":"nami_booking","server_url":"https://YOUR-N8N-HOST/mcp/YOUR-CURRENT-ID","headers":{"X-MCP-Token":"${N8N_MCP_TOKEN}"}}]'
```

Внешний n8n endpoint без HTTPS и без Bearer/custom-header защиты приложение отклоняет.

### 3. Привязать Telegram

1. Создайте бота через `@BotFather` и запишите токен в `.env`.
2. Первый раз запустите:

```bash
.venv/bin/python configure_telegram.py discover --write
```

Команда создаст одноразовый код и покажет точную команду вида `/start <длинный-код>`. Она специально завершится с кодом 2: это не ошибка.

3. Отправьте боту в личном чате именно показанную `/start <код>` целиком. Обычный `/start` без кода не привязывает аккаунт.

4. Запустите `discover --write` второй раз. Только теперь он запишет `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_USER_IDS` и `VOICE_TELEGRAM_USER_ID` в `.env`, а код сразу сменится.

```bash
.venv/bin/python configure_telegram.py discover --write
```

5. После настройки HTTPS-адреса добавьте Mini App в меню бота:

```bash
.venv/bin/python configure_telegram.py configure
```

Не публикуйте одноразовую `/start`-команду и не пересылайте её другим людям.

### 4. Запустить

```bash
./run_app.sh
```

Прямой запуск без shell-обёртки:

```bash
.venv/bin/python standalone_app.py
```

Проверка:

```bash
curl http://127.0.0.1:8765/health
```

## Команды Telegram-бота

- `/start` — показать Mini App и справку.
- `/call [цель]` — начать разговор; текст после команды станет целью агента.
- `/status` — показать состояние сервиса и звонка.
- `/hangup` — завершить ожидающий или активный звонок.

В Mini App есть также кнопка запуска разговора. Команды и подключения принимаются только от user ID из `TELEGRAM_ALLOWED_USER_IDS`.

## HTTPS и TURN

Telegram Mini App и доступ к микрофону требуют HTTPS. Обычно Ringback слушает `127.0.0.1:8765`, а Caddy или Nginx завершает TLS и передаёт HTTPS/WSS на локальный порт.

Минимальный Caddyfile:

```caddyfile
voice.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Для сложных NAT и мобильных сетей настройте coturn:

```dotenv
WEBRTC_ICE_SERVERS_JSON='[
  {"urls":["stun:stun.l.google.com:19302"]},
  {"urls":["turn:turn.example.com:3478?transport=udp","turns:turn.example.com:5349?transport=tcp"],"username":"USER","credential":"PASSWORD"}
]'
```

HTTPS-туннель не заменяет TURN: первый передаёт страницу и сигналинг, второй ретранслирует медиа при невозможности прямого пиринга.

## OpenAI API

`OPENAI_MODEL` — это модель, которая ведёт разговор и решает, когда вызывать n8n function tool. Для этого lightweight голосового профиля рекомендуется [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna): официальная документация описывает её как модель для cost-sensitive/high-volume нагрузок и подтверждает поддержку Chat Completions и function calling.

Приложение может оставаться на Chat Completions API с function tools; миграция на Responses API для этого релиза не обязательна. Поэтому default endpoint:

```dotenv
OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions"
```

`OPENAI_API_KEY` нужно взять из защищённой конфигурации проекта «ИИ телефония» и вставить локально в `.env`. Не переносите ключ в Git, логи, README или чат.

## n8n MCP и YCLIENTS

При старте Ringback получает из n8n актуальный `tools/list` и передаёт схемы в OpenAI Chat Completions как function tools. Для проверки окон YCLIENTS ожидаются:

- `nami_current_datetime`
- `nami_get_services`
- `nami_get_staff_for_service`
- `nami_get_available_dates`
- `nami_get_available_times`
- `nami_check_slot`

Имена не зашиты в агенте: `MCP_ALLOWED_TOOLS` может только сузить набор видимых read-only нод. Режим жёстко зафиксирован как `MCP_TOOL_POLICY=read_only`. Даже если вписать в allowlist имя с `create`, `book`, `set`, `update`, `delete`, `cancel` или другим изменяющим действием, код всё равно его отфильтрует. Запись, отмена и изменение YCLIENTS в этом релизе не поддерживаются.

## ElevenLabs: облачная речь по умолчанию

Один ElevenLabs API key используется для обоих направлений: Scribe v2 получает завершённую реплику в WAV и возвращает текст; TTS синтезирует ответ выбранным voice ID.

```dotenv
VOICE_TTS="elevenlabs"
VOICE_STT="elevenlabs"
ELEVENLABS_API_KEY="..."
ELEVENLABS_VOICE_ID="..."
ELEVENLABS_STT_MODEL_ID="scribe_v2"
ELEVENLABS_MODEL_ID="eleven_multilingual_v2"
```

Если model ID оставить пустым, код использует `scribe_v2` и `eleven_multilingual_v2`. API key и voice ID в стандартном облачном профиле обязательны.

## Опциональный локальный fallback

Обычный setup не качает Whisper/Piper. Если нужен отдельный локальный профиль, повторите нативную установку:

```bash
# macOS
INSTALL_LOCAL_VOICE=1 ./setup.sh

# Linux / WSL2
INSTALL_LOCAL_VOICE=1 ./setup-linux.sh
```

Затем явно переключите `.env`:

```dotenv
VOICE_STT="local"
VOICE_TTS="piper"
```

Стандартный Docker-образ специально остаётся лёгким и не содержит локальные модели; fallback ставится нативным setup-скриптом.

## Основные переменные `.env`

| Переменная | Нужна | Назначение |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | да | Токен бота и ключ проверки Mini App `initData` |
| `TELEGRAM_CHAT_ID` | да | Чат для уведомления о вызове |
| `TELEGRAM_ALLOWED_USER_IDS` | да | Разрешённые числовые user ID |
| `VOICE_TELEGRAM_USER_ID` | да | Адресат голосового вызова |
| `WEBRTC_PUBLIC_URL` | да | Публичный HTTPS URL Mini App |
| `WEBRTC_HOST`, `WEBRTC_PORT` | нет | Локальный HTTP/WSS listener |
| `WEBRTC_ICE_SERVERS_JSON` | production | STUN/TURN для WebRTC |
| `OPENAI_API_KEY` | да | Прямой OpenAI API key из проекта «ИИ телефония» |
| `OPENAI_MODEL` | да | Модель OpenAI; default `gpt-5.6-luna` |
| `OPENAI_BASE_URL` | нет | Default `https://api.openai.com/v1/chat/completions` |
| `MCP_SERVERS_JSON` | YCLIENTS | Активные n8n MCP endpoints |
| `MCP_ALLOWED_TOOLS` | рекомендуется | Дополнительно сужает read-only набор; не разрешает write-ноды |
| `N8N_MCP_TOKEN` | да для внешнего n8n | Bearer token или секрет для custom header |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | да | Scribe v2 STT и облачный TTS |
| `VOICE_STT`, `VOICE_TTS` | нет | `elevenlabs` по умолчанию; `local`/`piper` для опционального fallback |

Полный шаблон: [`.env.example`](.env.example).

## Docker

```bash
docker compose up -d --build
```

Готовый [`compose.yaml`](compose.yaml) публикует приложение только на
`127.0.0.1:8765`, добавляет healthcheck и автоматически перезапускает сервис.
Образ не собирает whisper.cpp, не ставит Piper и не копирует сотни мегабайт моделей. Он содержит только Python runtime, приложение, `ffmpeg` и его Python-зависимости.
Эквивалентный ручной запуск:

```bash
docker build -t ringback-standalone .
docker run -d \
  --name ringback \
  --restart unless-stopped \
  --env-file .env \
  -e WEBRTC_HOST=0.0.0.0 \
  -p 127.0.0.1:8765:8765 \
  ringback-standalone
```

Порт оставлен на loopback, чтобы он был доступен только reverse proxy. См. [Docker-инструкцию](docs/SETUP_DOCKER.md).

## Тесты

```bash
.venv/bin/python tests/run_all.py
```

Офлайн-тесты не используют живые Telegram, OpenAI, ElevenLabs или n8n ключи.

Локальную Mini App можно открыть в обычном браузере только в dev-режиме:

```dotenv
TELEGRAM_DEV_MODE="1"
TELEGRAM_DEV_USER_ID="1"
WEBRTC_HOST="127.0.0.1"
```

Не включайте `TELEGRAM_DEV_MODE` на публичном интерфейсе.

## Ограничение Telegram

Закрытую Mini App нельзя разбудить серверным WebSocket-событием, а боты не получают системный VoIP-экран Telegram. Если Mini App закрыта, бот присылает обычное push-сообщение с кнопкой «Ответить». Звук и показ push зависят от настроек Telegram, Focus и ОС.

## Безопасность

- Telegram `initData` проверяется на сервере по HMAC-SHA-256 и `auth_date`.
- Бот и WebRTC-сессии допускают только user ID из allowlist.
- Токены OpenAI, Telegram, ElevenLabs, TURN и n8n живут только в `.env` на сервере.
- В URL не передаются bot token и Telegram `initData`.
- OpenAI видит только read-only n8n-инструменты; allowlist не может обойти write-фильтр.

## Приватность и облачная обработка

Облачный профиль не является полностью локальным:

- каждая завершённая реплика пользователя в виде WAV отправляется в ElevenLabs Scribe v2;
- транскрипт диалога, цель звонка, схемы и результаты n8n MCP-инструментов отправляются напрямую в OpenAI API;
- текст ответа отправляется в ElevenLabs TTS.

Ringback не ведёт постоянный архив аудио и транскриптов. Для обработки реплики на сервере временно создаётся WAV-файл, который удаляется после STT; буферы и история диалога освобождаются после звонка/завершения процесса. Приложение само не сохраняет их в базе или в постоянном storage. Сроки хранения и использование данных у ElevenLabs и OpenAI определяются их текущими условиями и настройками аккаунта.

После изменения `.env` перезапускайте Ringback. Никогда не коммитьте `.env` и не передавайте его для диагностики.

## Лицензия

Apache-2.0. См. [LICENSE](LICENSE) и [NOTICE](NOTICE).
