<div align="center">

# 📥 MediaGrabber Bot

**Telegram-бот для скачивания видео, фото, гифок и музыки по ссылке**

Работает с TikTok, YouTube, Instagram, Pinterest и сотнями других сайтов.
Отдаёт файлы до 2 ГБ через локальный Bot API. Запускается одной командой в Docker.

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

### [▶️ Протестировать бота](https://t.me/allvideabot)

</div>

---

## Содержание

- [Возможности](#возможности)
- [Как это работает](#как-это-работает)
- [Быстрый старт (Docker)](#быстрый-старт-docker)
- [Настройки](#настройки)
- [Инлайн-режим](#инлайн-режим)
- [Instagram (cobalt)](#instagram-cobalt)
- [Запуск без Docker](#запуск-без-docker)
- [Структура проекта](#структура-проекта)
- [Лицензия](#лицензия)

---

## Возможности

| | |
|---|---|
| 📥 **Скачивание** | Видео, фото, фото-карусели, гифки и аудио по ссылке |
| 🎚 **Выбор качества** | Если файл больше лимита — бот предложит качество пониже. Для YouTube и VK Видео качество спрашивается сразу, не дожидаясь ошибки |
| 📊 **Прогресс-бар** | Живой процент/скорость/ETA прямо в статус-сообщении во время скачивания (для сайтов, где это применимо — см. ниже) |
| 🌀 **Видео → гифка** | Отправь видео с подписью «гифка» |
| 🎙 **Озвучка текста** | `озвучь <текст>` — синтез речи через edge-tts |
| 🔎 **Инлайн-режим** | `@имя_бота <ссылка>` в любом чате, с кэшем file_id — повторные ссылки отдаются мгновенно |
| 👑 **Админка** | Статистика, рассылка, список пользователей, очистка кэша, ручная проверка обновлений (`/checkupdate`) |

## Как это работает

Через обычный (облачный) Bot API бот может отдавать файлы **только до 50 МБ**.
Чтобы снять это ограничение, используется локальный
[Telegram Bot API](https://github.com/tdlib/telegram-bot-api) в режиме `--local` —
с ним лимит поднимается **до 2 ГБ** (это потолок самого Telegram). По умолчанию в
конфиге стоит 1.5 ГБ (`MAX_FILE_SIZE_MB_HQ`), при желании можно поднять до 2000.

`docker compose` поднимает два контейнера:

- **`telegram-bot-api`** — локальный API-сервер (режим `--local`, снимает лимит 50 МБ)
- **`bot`** — сам бот на aiogram

Они делят один том с файлами, а базы бота хранятся в отдельном томе и переживают
пересборку.

> Бот можно запустить и **без** локального Bot API (просто `python bot.py` против
> облачного `api.telegram.org`) — тогда всё работает, но отправка файлов
> ограничена 50 МБ. Docker-сборка ниже поднимает локальный сервер за тебя.

### Откуда качается медиа

- **Instagram** — через self-hosted [cobalt](https://github.com/imputnet/cobalt).
  Без куки, логина и пароля: рядом с ботом поднимается сервис `cobalt-api`,
  и бот просто отдаёт ему ссылку.
- **TikTok** — сначала [tikwm](https://www.tikwm.com/), при неудаче — yt-dlp.
- **Pinterest** — сначала yt-dlp, при неудаче — внутренний API самого Pinterest.
- **Всё остальное** (YouTube, VK Видео, Twitter/X, Reddit и сотни других сайтов) — yt-dlp.

## Установка (Docker)

Ниже — установка с нуля на чистом сервере (Ubuntu/Debian). Если Docker и git уже
стоят — сразу переходи к шагу 3.

### Шаг 1. Установи Docker

```bash
# Ставим Docker одним официальным скриптом
curl -fsSL https://get.docker.com | sh

# (по желанию) чтобы docker работал без sudo
sudo usermod -aG docker $USER
# после этой команды перелогинься в терминал
```

Проверь, что всё встало:

```bash
docker --version
docker compose version
```

### Шаг 2. Установи git (если нет)

```bash
sudo apt update && sudo apt install -y git
```

### Шаг 3. Скачай проект

```bash
git clone https://github.com/zxcatsu/mediagrabber-bot.git
cd mediagrabber-bot
```

### Шаг 4. Получи ключи

- **Токен бота** — создай бота у [@BotFather](https://t.me/BotFather), он пришлёт токен
- **`api_id` и `api_hash`** — залогинься на https://my.telegram.org → *API development tools* → создай приложение
- **Свой Telegram ID** (для админки) — узнай у [@getmyid_bot](https://t.me/getmyid_bot)

### Шаг 5. Заполни `.env`

```bash
cp .env.example .env
nano .env      # или любой другой редактор
```

Обязательно заполни четыре поля:

```env
TELEGRAM_TOKEN=токен_от_BotFather
ADMIN_IDS=твой_telegram_id
TELEGRAM_API_ID=api_id_с_my.telegram.org
TELEGRAM_API_HASH=api_hash_с_my.telegram.org
```

Остальное — по желанию (все переменные с комментариями есть в `.env.example`).

### Шаг 6. Запусти

```bash
docker compose up -d --build
```

Первая сборка займёт пару минут (качается образ, ставится ffmpeg и зависимости).

### Шаг 7. Проверь, что бот жив

```bash
docker compose logs -f bot
```

Если видишь строку `Бот-загрузчик запускается...` — всё работает, пиши боту в Telegram.

### Полезные команды

```bash
docker compose ps              # статус контейнеров
docker compose logs -f bot     # логи бота (Ctrl+C чтобы выйти)
docker compose restart bot     # перезапустить после правки .env
docker compose down            # остановить всё
docker compose up -d --build   # пересобрать и поднять заново
/checkupdate                   # для проверки обновлений в лс бота
```

## Обновление

Когда выходит новая версия, обновиться можно двумя командами из папки проекта:

```bash
git pull
docker compose up -d --build
```

`git pull` подтянет свежий код, а `docker compose up -d --build` пересоберёт
образ и перезапустит контейнеры. Твой `.env` и базы бота (том `bot-data`) при
этом не трогаются — настройки и статистика сохраняются.

> Если делал локальные правки и `git pull` ругается на конфликты — сохрани свои
> изменения (`git stash`), обновись, затем верни (`git stash pop`). `.env` в
> репозиторий не входит, его `git pull` не перезапишет.

### Уведомления о новых версиях

Бот сам раз в 6 часов проверяет [релизы на GitHub](https://github.com/zxcatsu/mediagrabber-bot/releases)
и, если вышла версия новее твоей, **пишет каждому админу** из `ADMIN_IDS`
сообщение с номером версии, списком изменений и командой для обновления.
Уведомление приходит один раз на версию (не спамит при перезапусках).
Отключить: `UPDATE_CHECK=0` в `.env`.

## Настройки

Лимиты скачивания задаются в `.env`:

| Переменная | По умолчанию | Описание |
|---|:---:|---|
| `MAX_FILE_SIZE_MB` | `300` | Лимит размера файла в обычном качестве |
| `MAX_FILE_SIZE_MB_HQ` | `1500` | Лимит, когда выбрано качество выше дефолтного (макс. 2000 — потолок Telegram) |
| `MAX_VIDEO_HEIGHT` | `720` | Потолок высоты видео по умолчанию |
| `PROXY_URL` | — | Общий прокси для yt-dlp/запросов (при блокировках по IP) |
| `PROXY_URL_RU` | — | Отдельный прокси для `.ru`-доменов (VK и т.п.) — их CDN часто плохо отдаёт трафик за границу. Если не задан, используется `PROXY_URL` |
| `COBALT_API_URL` | `http://cobalt-api:9000/` | Адрес инстанса cobalt для Instagram. Задан в `docker-compose.yml`, менять нужно только если cobalt на другом хосте/порту |
| `COBALT_API_KEY` | — | Ключ, если твой инстанс cobalt требует авторизацию. Для локального инстанса из `docker-compose.yml` не нужен |
| `UPDATE_CHECK` | `1` | Уведомлять админов о новых версиях (`0` — выключить) |

## Инлайн-режим

Чтобы работал `@имя_бота <ссылка>` в любом чате, в [@BotFather](https://t.me/BotFather):

- `/setinline` — включить инлайн-режим и задать placeholder-текст
- `/setinlinefeedback` → **Enabled** — иначе заглушку «качаю» нельзя подменить на видео

Затем:

1. Заведи приватный чат/канал, добавь туда бота с правом отправки и положи его
   `chat_id` в `.env` как `CACHE_CHAT_ID`.
2. Отправь боту в личку картинку-заглушку, ответь на неё командой `/placeholder`
   и положи полученный `file_id` в `.env` как `PLACEHOLDER_PHOTO_FILE_ID`.

## Instagram (cobalt)

Instagram скачивается через self-hosted [cobalt](https://github.com/imputnet/cobalt) —
никаких куки, логина, пароля и сессий. Сервис `cobalt-api` уже прописан в
`docker-compose.yml` и поднимается вместе с ботом; бот ходит к нему по
внутреннему адресу `http://cobalt-api:9000/`. `docker compose up -d` поднимает
всё разом — отдельная настройка не нужна.

Хочешь использовать внешний инстанс cobalt (свой на другом хосте или
публичный из списка [instances.cobalt.best](https://instances.cobalt.best)) —
убери сервис `cobalt-api` из `docker-compose.yml` и задай в `.env`:

```env
COBALT_API_URL=https://твой-инстанс/
# COBALT_API_KEY=...   # только если инстанс требует ключ
```

## Запуск без Docker

Нужны Python 3.11+, `ffmpeg` и запущенный `telegram-bot-api`:

```bash
pip install -r requirements.txt
# в .env укажи: TELEGRAM_API_URL, LOCAL_API_DIR, DATA_DIR
python bot.py
```

## Структура проекта

```
.
├── bot.py                 # точка входа, хендлеры, админка, инлайн-режим
├── skills/
│   ├── video.py           # скачивание (yt-dlp + tikwm + Pinterest API)
│   ├── cobalt.py          # скачивание Instagram через cobalt
│   ├── photo_video.py     # склейка фото + звук в видео (ffmpeg)
│   ├── gif.py             # видео → gif (ffmpeg)
│   ├── voice.py           # озвучка текста (edge-tts)
│   ├── inline_cache.py    # кэш file_id для инлайн-режима
│   └── stats.py           # статистика использования
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Лицензия

[MIT](LICENSE)
