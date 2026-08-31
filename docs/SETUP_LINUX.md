# Установка Ringback Standalone на Linux

Рекомендуется Ubuntu 22.04+ или Debian 12+ с Python 3.10+. Поддерживается также Fedora/RHEL с `dnf`.

## Установка

```bash
./setup-linux.sh
```

Скрипт ставит Python, `ffmpeg` и минимальные runtime-пакеты, создаёт `.venv` и `.env`. По умолчанию речь обрабатывают ElevenLabs Scribe v2 + TTS, поэтому whisper.cpp, Piper, compiler toolchain и локальные AI-модели не ставятся. Перезапуск установщика не затирает `.env`.

## Настройка и запуск

```bash
# Сначала заполните в .env bot token и cloud-ключи
.venv/bin/python configure_telegram.py discover --write
```

Первый запуск покажет одноразовую `/start <код>` и завершится с кодом 2. Отправьте боту именно эту команду целиком, затем:

```bash
.venv/bin/python configure_telegram.py discover --write
.venv/bin/python configure_telegram.py configure
./run_app.sh --check
./run_app.sh
```

Второй `discover --write` запишет только ID аккаунта, от которого пришёл правильный код, и сразу ротирует его.

В `.env` обязательны Telegram-токен/ID, HTTPS URL, прямой OpenAI key, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` и текущий production URL опубликованного n8n MCP workflow. OpenAI key возьмите из защищённой конфигурации проекта «ИИ телефония» и запишите его только локально:

```dotenv
OPENAI_API_KEY="..."
OPENAI_MODEL="gpt-5.6-luna"
OPENAI_BASE_URL="https://api.openai.com/v1/chat/completions"
```

[Официальная документация GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) подтверждает Chat Completions и function calling; Responses API в этом релизе не обязателен. Внешний n8n URL должен быть HTTPS и иметь Bearer/custom-header защиту. Legacy URL отвечает, но без такой авторизации приложение намеренно его отклоняет.

`MCP_ALLOWED_TOOLS` только сужает read-only набор. Он не может разрешить запись, отмену или другую mutating-ноду.

## Опциональный локальный fallback

```bash
INSTALL_LOCAL_VOICE=1 ./setup-linux.sh
```

Этот opt-in профиль дополнительно соберёт whisper.cpp, скачает Whisper/Piper и установит compiler toolchain. После него задайте в `.env`:

```dotenv
VOICE_STT="local"
VOICE_TTS="piper"
```

## Запуск как systemd-сервис

Создайте `/etc/systemd/system/ringback.service`, подставив реального пользователя и путь:

```ini
[Unit]
Description=Ringback standalone Telegram voice assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ringback
WorkingDirectory=/opt/ringback
ExecStart=/opt/ringback/run_app.sh
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ringback
sudo systemctl status ringback
```

Дайте `.env` права `0600` и владельца, от которого работает сервис.

## Reverse proxy и проверка

Caddy:

```caddyfile
voice.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

```bash
curl http://127.0.0.1:8765/health
journalctl -u ringback -f
.venv/bin/python tests/run_all.py
```

Не публикуйте порт 8765 во внешнюю сеть в обход reverse proxy. Для надёжного звука на мобильных сетях настройте coturn.

## Приватность

Каждый WAV-turn отправляется в ElevenLabs Scribe v2; транскрипт и MCP-данные — напрямую в OpenAI API; текст ответа — в ElevenLabs TTS. Ringback удаляет временный WAV и освобождает RAM-буферы, не создавая постоянного архива. Retention у ElevenLabs и OpenAI зависит от их условий и настроек аккаунта.
