# Ringback Standalone в Docker

Стандартный Docker-образ — lightweight cloud profile. Он содержит Python-приложение, aiortc, `ffmpeg` и runtime-зависимости. whisper.cpp, Piper, compiler toolchain и сотни мегабайт локальных моделей в образ не входят.

По умолчанию ElevenLabs Scribe v2 распознаёт русскую речь, а ElevenLabs TTS озвучивает ответы. Поэтому `ELEVENLABS_API_KEY` и `ELEVENLABS_VOICE_ID` обязательны.

## 1. Подготовить `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Заполните как минимум:

- `TELEGRAM_BOT_TOKEN` и `WEBRTC_PUBLIC_URL`;
- `OPENAI_API_KEY`, `OPENAI_MODEL=gpt-5.6-luna` и `OPENAI_BASE_URL=https://api.openai.com/v1/chat/completions`;
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `VOICE_STT=elevenlabs`, `VOICE_TTS=elevenlabs`;
- текущий production n8n MCP Trigger URL в `MCP_SERVERS_JSON`.

`OPENAI_API_KEY` возьмите из защищённой конфигурации проекта «ИИ телефония» и запишите только в хостовый `.env`. Не печатайте его в логах и не передавайте в чат. [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) поддерживает Chat Completions и function calling; Responses API здесь не обязателен.

Внешний n8n endpoint должен:

- использовать `https://`;
- быть production URL опубликованного workflow;
- передавать Bearer token или секретный custom header.

Пример с Bearer:

```dotenv
MCP_SERVERS_JSON='[{"server_label":"nami_booking","server_url":"https://YOUR-N8N-HOST/mcp/YOUR-CURRENT-ID","authorization":"Bearer ${N8N_MCP_TOKEN}"}]'
N8N_MCP_TOKEN="..."
```

Пример с custom header:

```dotenv
MCP_SERVERS_JSON='[{"server_label":"nami_booking","server_url":"https://YOUR-N8N-HOST/mcp/YOUR-CURRENT-ID","headers":{"X-MCP-Token":"${N8N_MCP_TOKEN}"}}]'
```

Legacy n8n URL из сохранённой конфигурации отвечает без отдельной авторизации, поэтому приложение намеренно его отклоняет. Защитите production endpoint; тестовый URL не подходит.

`MCP_TOOL_POLICY` жёстко остаётся `read_only`. `MCP_ALLOWED_TOOLS` только сужает набор и не может включить mutating-ноды.

## 2. Собрать образ

```bash
docker compose build
```

В build context не попадает `.env`. Поскольку нет сборки C++ и загрузки моделей, обычный build значительно быстрее и легче прежнего local-AI образа.

## 3. Защищённая Telegram-привязка

На Linux/macOS запустите helper с подключённым хостовым `.env`:

```bash
docker compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/.env:/app/.env" \
  ringback python configure_telegram.py discover --write
```

Первый запуск создаст одноразовую команду `/start <код>` и завершится с ожидаемым кодом 2. Отправьте боту в личном чате точно эту команду и повторите тот же `docker compose run`. Второй запуск запишет Telegram ID в хостовый `.env` и ротирует код.

На Docker Desktop for Windows используйте PowerShell-вариант без Linux UID:

```powershell
docker compose run --rm --no-deps `
  -v "${PWD}/.env:/app/.env" `
  ringback python configure_telegram.py discover --write
```

После второго `discover --write` настройте кнопку Mini App аналогичной командой, заменив в конце `discover --write` на `configure`.

## 4. Запустить

```bash
docker compose up -d
docker compose ps
```

Готовый `compose.yaml`:

- передаёт `.env` только в runtime;
- публикует порт только на `127.0.0.1:8765`;
- добавляет healthcheck и `restart: unless-stopped`;
- корректно передаёт сигнал остановки через `init: true`.

Ручной эквивалент:

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

## 5. Reverse proxy и TURN

```caddyfile
voice.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Публичный URL должен совпадать с `WEBRTC_PUBLIC_URL` и URL меню Mini App. Для надёжного WebRTC между сетями добавьте внешний TURN-сервер в `WEBRTC_ICE_SERVERS_JSON`.

## 6. Проверка

```bash
curl http://127.0.0.1:8765/health
docker compose logs -f ringback
```

После изменения `.env` пересоздайте контейнер: `docker compose up -d --force-recreate`.

## Локальный fallback

Стандартный Docker-образ не содержит fallback-модели и не принимает `INSTALL_LOCAL_VOICE` как build arg. Для opt-in локального профиля используйте нативный setup:

```bash
# macOS
INSTALL_LOCAL_VOICE=1 ./setup.sh

# Linux / WSL2
INSTALL_LOCAL_VOICE=1 ./setup-linux.sh
```

И задайте `VOICE_STT=local`, `VOICE_TTS=piper`.

## Приватность

В облачном профиле каждый завершённый WAV-turn отправляется ElevenLabs Scribe v2, транскрипт и MCP-данные — напрямую OpenAI API, текст ответа — ElevenLabs TTS. Ringback удаляет временный WAV и освобождает RAM-буферы после звонка; постоянный архив не ведётся. Retention у ElevenLabs и OpenAI определяется их текущими условиями и настройками аккаунта.
