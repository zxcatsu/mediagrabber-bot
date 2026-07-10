import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaAnimation,
    ReplyKeyboardMarkup, KeyboardButton, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, ChosenInlineResult, InlineQueryResultArticle,
    InlineQueryResultCachedVideo, InlineQueryResultCachedPhoto,
    InlineQueryResultCachedMpeg4Gif, InputTextMessageContent,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from cachetools import TTLCache


from skills.video import (
    VideoDownloadError, FileTooLargeError, download_media, cleanup_download,
)
from skills.photo_video import photo_to_video
from skills.gif import GifError, video_to_gif
from skills.voice import text_to_speech
from skills import inline_cache
from skills import stats

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_API_URL = os.getenv("TELEGRAM_API_URL", "http://telegram-bot-api:8081")
session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_URL, is_local=True))
bot = Bot(token=TELEGRAM_TOKEN, session=session)

LOCAL_API_DIR = Path(os.getenv("LOCAL_API_DIR", "/var/lib/telegram-bot-api"))
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))

BOT_VERSION = "1.1.0"
UPDATE_REPO = os.getenv("UPDATE_REPO", "zxcatsu/mediagrabber-bot")
UPDATE_CHECK = os.getenv("UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "no", "")

_admin_ids_raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "0")
ADMIN_IDS = {int(x) for x in _admin_ids_raw.split(",") if x.strip().lstrip("-").isdigit()}


_cache_chat_raw = os.getenv("CACHE_CHAT_ID")
CACHE_CHAT_ID = int(_cache_chat_raw) if _cache_chat_raw and _cache_chat_raw.lstrip("-").isdigit() else None


PLACEHOLDER_PHOTO_FILE_ID = os.getenv("PLACEHOLDER_PHOTO_FILE_ID")

if not TELEGRAM_TOKEN:
    raise SystemExit("Не найден TELEGRAM_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media_bot")

BOT_START_TIME = time.time()

dp = Dispatcher()

URL_PATTERN = re.compile(r"https?://\S+")
GIF_TRIGGER_RE = re.compile(r"(?<!\w)гифка(?!\w)", re.IGNORECASE)
_busy_downloads: set[int] = set()
_inline_processing: dict[str, asyncio.Task] = {}
_bot_username: str | None = None


_CAROUSEL_SEND_DELAY_SEC = 3


async def _call_with_flood_retry(coro_factory, max_retries: int = 5):
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            log.warning(
                "Флуд-лимит Telegram, жду %s сек. (попытка %s/%s)",
                e.retry_after, attempt + 1, max_retries,
            )
            await asyncio.sleep(e.retry_after + 1)
    return await coro_factory()


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days: parts.append(f"{days}д")
    if hours: parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    return " ".join(parts)


def _format_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "Б" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def _sum_sizes(*paths_or_lists) -> int:
    total = 0
    for item in paths_or_lists:
        if not item:
            continue
        candidates = item if isinstance(item, list) else [item]
        for p in candidates:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


async def _get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.get_me()).username
    return _bot_username


def _has_gif_trigger(text: str | None) -> bool:
    return bool(text and GIF_TRIGGER_RE.search(text))


def _platform_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "tiktok" in host:
        return "TikTok"
    if "instagram" in host:
        return "Instagram"
    if "pinterest" in host or host == "pin.it" or host.endswith(".pin.it"):
        return "Pinterest"
    if "youtu" in host:
        return "YouTube"
    if "vk.com" in host or "vkvideo.ru" in host or host.endswith(".vk.com"):
        return "VK Видео"
    return "Видео"


def _is_youtube(url: str) -> bool:
    return "youtu" in urlparse(url).netloc.lower()


def _is_vk_video(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "vk.com" in host or "vkvideo.ru" in host or host.endswith(".vk.com")


def _wants_upfront_quality(url: str) -> bool:
    return _is_youtube(url) or _is_vk_video(url)

_NO_PROGRESS_HOSTS_SUBSTR = ("tiktok", "instagram", "pinterest", "twitter")
_NO_PROGRESS_HOSTS_EXACT = ("pin.it", "x.com")


def _wants_progress_bar(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if host in _NO_PROGRESS_HOSTS_EXACT:
        return False
    if any(host.endswith(f".{h}") for h in _NO_PROGRESS_HOSTS_EXACT):
        return False
    return not any(s in host for s in _NO_PROGRESS_HOSTS_SUBSTR)


QUALITIES = [("360p", 360), ("480p", 480), ("720p", 720), ("1080p", 1080), ("1440p", 1440), ("2160p", 2160)]
INLINE_MAX_HEIGHT = 1080 
_quality_pending = TTLCache(maxsize=1000, ttl=3600)


USERS_FILE = DATA_DIR / "users.json"
_users_lock = asyncio.Lock()
_users_cache: set[int] | None = None


def _read_users_from_disk() -> set[int]:
    if USERS_FILE.exists():
        try:
            return set(json.loads(USERS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("users.json повреждён или нечитаем (%s) — начинаю с пустой базы", e)
    return set()


def load_users() -> set[int]:
    global _users_cache
    if _users_cache is None:
        _users_cache = _read_users_from_disk()
    return set(_users_cache)


async def save_user(user_id: int) -> None:
    global _users_cache
    async with _users_lock:
        if _users_cache is None:
            _users_cache = _read_users_from_disk()
        if user_id in _users_cache:
            return
        _users_cache.add(user_id)
        tmp_path = USERS_FILE.with_suffix(".tmp")
        try:
            await asyncio.to_thread(
                tmp_path.write_text, json.dumps(list(_users_cache)), encoding="utf-8"
            )
            await asyncio.to_thread(tmp_path.replace, USERS_FILE)
        except OSError as e:
            log.warning("Не удалось сохранить users.json: %s", e)


async def remove_users(user_ids: list[int]) -> None:
    global _users_cache
    if not user_ids:
        return
    async with _users_lock:
        if _users_cache is None:
            _users_cache = _read_users_from_disk()
        _users_cache.difference_update(user_ids)
        tmp_path = USERS_FILE.with_suffix(".tmp")
        try:
            await asyncio.to_thread(
                tmp_path.write_text, json.dumps(list(_users_cache)), encoding="utf-8"
            )
            await asyncio.to_thread(tmp_path.replace, USERS_FILE)
        except OSError as e:
            log.warning("Не удалось сохранить users.json: %s", e)


class AdminState(StatesGroup):
    waiting_for_broadcast_msg = State()
    waiting_for_broadcast_confirm = State()

class VoiceState(StatesGroup):
    waiting_for_voice = State()


VOICES = {
    "👨 Дмитрий (RU)": "ru-RU-DmitryNeural",
    "👩 Светлана (RU)": "ru-RU-SvetlanaNeural",
    "👩 Айгуль (KZ)": "kk-KZ-AigulNeural",
    "👨 Даулет (KZ)": "kk-KZ-DauletNeural"
}

USERS_PAGE_SIZE = 10


def _admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Сделать рассылку")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🗑 Очистить инлайн-кэш")],
            [KeyboardButton(text="❌ Отмена")]
        ],
        resize_keyboard=True
    )


@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    users = load_users()
    uptime = _format_uptime(time.time() - BOT_START_TIME)
    cache_entries = len(inline_cache.iter_entries())

    await message.answer(
        f"👑 **Админ-панель**\n\n"
        f"Пользователей в базе: `{len(users)}`\n"
        f"Аптайм бота: `{uptime}`\n"
        f"Записей в инлайн-кэше: `{cache_entries}`\n\n"
        f"Выбери действие на клавиатуре ниже:",
        parse_mode="Markdown",
        reply_markup=_admin_keyboard()
    )

@dp.message(F.text == "📊 Статистика", F.from_user.id.in_(ADMIN_IDS))
async def admin_stats(message: Message) -> None:
    s = stats.summary()
    text = (
        f"📊 **Статистика**\n\n"
        f"Скачано ссылок всего: `{s['downloads_total']}`\n"
        f"Скачано сегодня: `{s['downloads_today']}`\n"
        f"Пользователей сегодня: `{s['active_today']}`\n\n"
        f"По функциям (всего):\n"
        f"  📥 Скачивание: `{s['downloads_total']}`\n"
        f"  🌀 Гифки: `{s['gif_total']}`\n"
        f"  🎙 Озвучка: `{s['voice_total']}`\n\n"
        f"Трафика отдано: `{_format_bytes(s['bytes_total'])}`"
    )
    await message.reply(text, parse_mode="Markdown")


def _users_page_keyboard(page: int) -> tuple[InlineKeyboardMarkup, int]:
    users_list = stats.list_users()
    total_pages = max(1, (len(users_list) + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * USERS_PAGE_SIZE
    chunk = users_list[start:start + USERS_PAGE_SIZE]

    rows = []
    for uid, info in chunk:
        label = info.get("first_name") or (f"@{info['username']}" if info.get("username") else str(uid))
        total_uses = sum(info.get("counts", {}).values())
        rows.append([InlineKeyboardButton(text=f"{label} · {total_uses}", callback_data=f"user_view:{uid}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"users_page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"users_page:{page + 1}"))
    rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows), len(users_list)


@dp.message(F.text == "👥 Пользователи", F.from_user.id.in_(ADMIN_IDS))
async def admin_users_list(message: Message) -> None:
    kb, total = _users_page_keyboard(0)
    if total == 0:
        await message.reply("Пока никто не оставил следов в статистике — попользуются ботом, появятся тут.")
        return
    await message.reply(f"👥 Всего в статистике: {total} чел.\nВыбери пользователя:", reply_markup=kb)


@dp.callback_query(F.data.startswith("users_page:"), F.from_user.id.in_(ADMIN_IDS))
async def admin_users_page(callback: CallbackQuery) -> None:
    await callback.answer()
    page = int(callback.data.split(":", 1)[1])
    kb, _ = _users_page_keyboard(page)
    try:
        await callback.message.edit_text("Выбери пользователя:", reply_markup=kb)
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("user_view:"), F.from_user.id.in_(ADMIN_IDS))
async def admin_user_view(callback: CallbackQuery) -> None:
    await callback.answer()
    _, uid_str, page_str = callback.data.split(":", 2)
    uid = int(uid_str)
    info = stats.user_info(uid)
    if not info:
        await callback.message.edit_text("Не нашёл этого пользователя в статистике.")
        return

    name = info.get("first_name") or "—"
    username = f"@{info['username']}" if info.get("username") else "—"
    counts = info.get("counts", {})
    sources = info.get("sources", {})
    last_seen = time.strftime("%d.%m.%Y %H:%M", time.localtime(info.get("last_seen", 0)))
    first_seen = time.strftime("%d.%m.%Y %H:%M", time.localtime(info.get("first_seen", 0)))
    platform = info.get("last_platform") or "—"

    text = (
        f"👤 **{name}** ({username})\n"
        f"ID: `{uid}`\n\n"
        f"Первый визит: `{first_seen}`\n"
        f"Последний визит: `{last_seen}`\n\n"
        f"📥 Скачиваний: `{counts.get('download', 0)}`\n"
        f"🌀 Гифок: `{counts.get('gif', 0)}`\n"
        f"🎙 Озвучек: `{counts.get('voice', 0)}`\n\n"
        f"В ЛС: `{sources.get('private', 0)}`, инлайном: `{sources.get('inline', 0)}`\n"
        f"Трафика: `{_format_bytes(info.get('bytes', 0))}`\n"
        f"Последняя платформа: `{platform}`"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ К списку", callback_data=f"users_page:{page_str}")
    ]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)


@dp.message(F.text == "🗑 Очистить инлайн-кэш", F.from_user.id.in_(ADMIN_IDS))
async def admin_clear_cache_ask(message: Message) -> None:
    count = len(inline_cache.iter_entries())
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, очистить", callback_data="clearcache_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="clearcache_cancel"),
    ]])
    await message.reply(
        f"Точно очистить инлайн-кэш? Записей сейчас: {count}.\nЭто действие необратимо.",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "clearcache_confirm", F.from_user.id.in_(ADMIN_IDS))
async def admin_clear_cache_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    count = await inline_cache.clear_all()
    await callback.message.edit_text(f"🗑 Инлайн-кэш очищен, удалено записей: {count}")


@dp.callback_query(F.data == "clearcache_cancel", F.from_user.id.in_(ADMIN_IDS))
async def admin_clear_cache_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Отменено, кэш не тронут.")


@dp.message(F.text == "📢 Сделать рассылку", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_start(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_for_broadcast_msg)
    await message.reply("Отправь сообщение для рассылки (можно с фото/видео). \nДля отмены нажми '❌ Отмена'.")

@dp.message(F.text == "❌ Отмена", F.from_user.id.in_(ADMIN_IDS))
async def admin_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    from aiogram.types import ReplyKeyboardRemove
    await message.reply("Действие отменено.", reply_markup=ReplyKeyboardRemove())

@dp.message(StateFilter(AdminState.waiting_for_broadcast_msg), F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_preview(message: Message, state: FSMContext) -> None:
    if message.text == "❌ Отмена":
        await admin_cancel(message, state)
        return

    users = load_users()
    if not users:
        await message.reply("База пользователей пуста.")
        await state.clear()
        return

    await state.update_data(broadcast_chat_id=message.chat.id, broadcast_message_id=message.message_id)
    await state.set_state(AdminState.waiting_for_broadcast_confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить всем", callback_data="broadcast_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel"),
    ]])
    await message.reply(f"Сообщение выше ⬆️ уйдёт {len(users)} чел. Отправляем?", reply_markup=kb)


@dp.callback_query(F.data == "broadcast_cancel", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.")


@dp.callback_query(F.data == "broadcast_confirm", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_confirm_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    src_chat_id = data.get("broadcast_chat_id")
    src_message_id = data.get("broadcast_message_id")
    await state.clear()

    users = load_users()
    if not users or not src_chat_id or not src_message_id:
        await callback.message.edit_text("Не нашёл сообщение для рассылки — начни заново через '📢 Сделать рассылку'.")
        return

    total = len(users)
    await callback.message.edit_text(f"Рассылка начата... ⏳ 0/{total}")

    success = 0
    blocked: list[int] = []
    for i, user_id in enumerate(users, start=1):
        for attempt in range(2):
            try:
                await bot.copy_message(chat_id=user_id, from_chat_id=src_chat_id, message_id=src_message_id)
                success += 1
                await asyncio.sleep(0.05)
                break
            except TelegramRetryAfter as e:
                log.warning("Флуд-лимит Telegram, жду %s сек.", e.retry_after)
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramForbiddenError:
                blocked.append(user_id)
                break
            except Exception as e:
                log.warning(f"Не удалось отправить юзеру {user_id}: {e}")
                break

        if i % 20 == 0 or i == total:
            try:
                await callback.message.edit_text(f"Рассылка идёт... ⏳ {i}/{total}")
            except TelegramBadRequest:
                pass

    if blocked:
        await remove_users(blocked)

    text = f"✅ Рассылка завершена!\nУспешно доставлено: {success} из {total}"
    if blocked:
        text += f"\n🚫 Заблокировали бота и удалены из базы: {len(blocked)}"
    await callback.message.edit_text(text)

@dp.message(Command("placeholder"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_get_placeholder_id(message: Message) -> None:
    if not message.reply_to_message or not message.reply_to_message.photo:
        await message.reply("Ответь этой командой на фото — пришлю его file_id.")
        return
    file_id = message.reply_to_message.photo[-1].file_id
    await message.reply(f"`{file_id}`", parse_mode="Markdown")


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    if message.from_user:
        await save_user(message.from_user.id)

    payload = command.args
    if payload and payload.startswith("carousel_"):
        await _send_full_carousel(message, payload.removeprefix("carousel_"))
        return

    await message.answer(
        "Привет! Я бот для работы с медиа. Что я умею:\n\n"
        "📥Скачивать фото,видео,гифки и музыку — просто отправь ссылку (TikTok, YouTube, Pinterest, Instagram и др.)\n"
        "🌀Делать гифки — отправь видео с подписью `гифка`\n"
        "🎙Озвучивать текст — напиши `озвучь <твой текст>`\n\n"
        f"💡Также можешь набрать в любом чате `@{await _get_bot_username()} <ссылка>` "
        "и отправить медиа прямо туда, без переключения в этот диалог, для этого нажми на фото и отправь в чат, далее фото поменяется на твоё медиа."
    )


async def _send_full_carousel(message: Message, token: str) -> None:
    entry = inline_cache.get(token)
    if not entry or not entry.get("items"):
        await message.answer("Не нашёл эту карусель — возможно, кэш почистили. Пришли ссылку ещё раз в чат.")
        return


    media = [InputMediaPhoto(media=item["file_id"]) for item in entry["items"] if item["kind"] == "photo"]
    if not media:
        await message.answer("Не нашёл фото в этой карусели 🤷")
        return

    for start in range(0, len(media), 10):
        if start > 0:
            await asyncio.sleep(_CAROUSEL_SEND_DELAY_SEC)
        chunk = media[start:start + 10]
        await _call_with_flood_retry(lambda c=chunk: message.answer_media_group(media=c))

PROGRESS_EDIT_INTERVAL = 3.0  


def _progress_bar(percent: int, width: int = 12) -> str:
    filled = max(0, min(width, round(width * percent / 100)))
    return "▓" * filled + "░" * (width - filled)


def _fmt_size(n: float | None) -> str:
    if not n:
        return ""
    for unit in ("Б/с", "КБ/с", "МБ/с", "ГБ/с"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ/с"


async def _safe_edit_status(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await message.edit_text(text)
        except Exception:
            pass
    except TelegramBadRequest:
        pass
    except Exception:
        log.debug("Не удалось обновить статус прогресса", exc_info=True)


def _make_progress_hook(loop: asyncio.AbstractEventLoop, status_message: Message, label: str = "Качаю"):
    state = {"last_ts": 0.0, "last_percent": -1}

    def hook(d: dict) -> None:
        status = d.get("status")
        if status == "finished":
            asyncio.run_coroutine_threadsafe(
                _safe_edit_status(status_message, f"⌛ {label}: обрабатываю файл..."), loop
            )
            return
        if status != "downloading":
            return

        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        if not total:
            return
        percent = max(0, min(100, int(downloaded * 100 / total)))

        now = time.monotonic()
        if percent == state["last_percent"] or now - state["last_ts"] < PROGRESS_EDIT_INTERVAL:
            return
        state["last_percent"] = percent
        state["last_ts"] = now

        speed_str = _fmt_size(d.get("speed"))
        eta = d.get("eta")
        eta_str = f" · осталось ~{eta}с" if eta else ""
        extra = f"  {speed_str}" if speed_str else ""

        text = f"⌛ {label}: {percent}%\n{_progress_bar(percent)}{extra}{eta_str}"
        asyncio.run_coroutine_threadsafe(_safe_edit_status(status_message, text), loop)

    return hook


async def _ask_quality(message: Message, url: str, heights: list[int] | None = None, prompt: str = "Выбери качество видео 👇") -> None:
    token = f"{message.chat.id}:{message.message_id}"
    _quality_pending[token] = url

    allowed = set(heights) if heights else None
    builder = InlineKeyboardBuilder()
    for label, height in QUALITIES:
        if allowed is None or height in allowed:
            builder.button(text=label, callback_data=f"ytq:{height}:{token}")
    builder.adjust(3)

    await message.reply(prompt, reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("ytq:"))
async def process_quality(callback: CallbackQuery) -> None:
    await callback.answer()
    _, height_str, token = callback.data.split(":", 2)
    height = int(height_str)

    url = _quality_pending.pop(token, None)
    if not url:
        await callback.message.edit_text("Ссылка устарела — пришли её ещё раз 🙏")
        return

    quality_label = next((label for label, h in QUALITIES if h == height), f"{height}p")

    user_id = callback.from_user.id if callback.from_user else 0
    if user_id in _busy_downloads:
        await callback.message.edit_text("Подожди, твоя предыдущая ссылка ещё обрабатывается ⏳")
        return
    _busy_downloads.add(user_id)

    await callback.message.edit_text(f"⌛ Качаю в {quality_label}...")
    video_path, audio_path = None, None

    try:
        loop = asyncio.get_running_loop()
        progress_hook = (
            _make_progress_hook(loop, callback.message, label=f"Качаю в {quality_label}")
            if _wants_progress_bar(url) else None
        )
        video_path, audio_path, _photos, _gif = await download_media(url, max_height=height, progress_hook=progress_hook)

        if not video_path:
            await callback.message.edit_text("Не нашёл видео по этой ссылке 🤷")
            return

        await callback.message.delete()
        await callback.message.answer_video(FSInputFile(str(video_path.resolve())), caption="", request_timeout=180)
        if audio_path:
            await callback.message.answer_audio(FSInputFile(str(audio_path.resolve())), caption="", request_timeout=180)

        await stats.record(
            user_id, "download", "private",
            username=callback.from_user.username if callback.from_user else None,
            first_name=callback.from_user.first_name if callback.from_user else None,
            platform=_platform_name(url),
            size_bytes=_sum_sizes(video_path, audio_path),
        )

    except FileTooLargeError as e:
        if e.available_heights:
            await callback.message.edit_text("Даже в этом качестве файл великоват — выбери пониже:")
            await _ask_quality(callback.message, url, e.available_heights)
        else:
            await callback.message.edit_text("Файл слишком большой даже в минимальном качестве 😔")
    except VideoDownloadError as e:
        await callback.message.edit_text(f"Не вышло скачать: {e}")
    except Exception:
        log.exception("Ошибка при скачивании видео в качестве %s", quality_label)
        await callback.message.edit_text("Что-то пошло не так 🤷")
    finally:
        _busy_downloads.discard(user_id)
        cleanup_download(video_path, audio_path)


@dp.message(F.text.regexp(URL_PATTERN))
async def handle_url(message: Message) -> None:
    if message.from_user: await save_user(message.from_user.id)

    url = URL_PATTERN.search(message.text).group(0)

    if _wants_upfront_quality(url):
        await _ask_quality(message, url)
        return

    user_id = message.from_user.id if message.from_user else 0
    if user_id in _busy_downloads:
        await message.reply("Подожди, твоя предыдущая ссылка ещё обрабатывается ⏳")
        return
    _busy_downloads.add(user_id)

    status = await message.reply("⌛")
    video_path, audio_path, photos_list, gif_path = None, None, None, None
    slide_audio_path = None
    found_media = False

    try:
        loop = asyncio.get_running_loop()
        progress_hook = _make_progress_hook(loop, status) if _wants_progress_bar(url) else None
        video_path, audio_path, photos_list, gif_path = await download_media(url, progress_hook=progress_hook)


        if not gif_path and not video_path and photos_list and len(photos_list) == 1 and audio_path:
            converted = await asyncio.to_thread(photo_to_video, photos_list[0], audio_path)
            if converted:
                video_path = converted
                photos_list = None
                slide_audio_path = audio_path
                audio_path = None

        if gif_path:

            await message.answer_animation(FSInputFile(str(gif_path.resolve())), request_timeout=180)
            found_media = True
        elif photos_list:


            for start in range(0, len(photos_list), 10):
                if start > 0:
                    await asyncio.sleep(_CAROUSEL_SEND_DELAY_SEC)
                chunk = photos_list[start:start + 10]
                media_group = [
                    InputMediaPhoto(
                        media=FSInputFile(str(p.resolve())),
                        caption="" if i == 0 and start == 0 else None,
                    )
                    for i, p in enumerate(chunk)
                ]
                await _call_with_flood_retry(
                    lambda mg=media_group: message.answer_media_group(media=mg, request_timeout=180)
                )
            if audio_path:
                await message.answer_audio(FSInputFile(str(audio_path.resolve())), caption="", request_timeout=180)
            found_media = True
        elif video_path:

            await message.answer_video(FSInputFile(str(video_path.resolve())), caption="", request_timeout=180)
            if audio_path:
                await message.answer_audio(FSInputFile(str(audio_path.resolve())), caption="", request_timeout=180)
            found_media = True
        else:
            await message.reply("Не нашёл, что скачивать по этой ссылке 🤷")

        if found_media:
            await stats.record(
                user_id, "download", "private",
                username=message.from_user.username if message.from_user else None,
                first_name=message.from_user.first_name if message.from_user else None,
                platform=_platform_name(url),
                size_bytes=_sum_sizes(video_path, audio_path, gif_path, photos_list),
            )

    except FileTooLargeError as e:
        if e.available_heights:
            await _ask_quality(message, url, e.available_heights, "Файл большой — выбери качество 👇")
        else:
            await message.reply("Файл слишком большой даже в минимальном качестве 😔")
    except VideoDownloadError as e:
        await message.reply(f"Не вышло скачать: {e}")
    except Exception:
        log.exception("Ошибка при скачивании")
        await message.reply("Что-то пошло не так 🤷")
    finally:
        _busy_downloads.discard(user_id)
        await status.delete()
        cleanup_download(video_path, audio_path, gif_path, slide_audio_path, photos_list)

@dp.message(F.func(lambda m: _has_gif_trigger(m.text) or _has_gif_trigger(m.caption)))
async def handle_gif(message: Message) -> None:
    if message.from_user: await save_user(message.from_user.id)

    source = message if message.video else message.reply_to_message
    if not source or not source.video:
        await message.reply("Пришли видео с подписью «гифка» 🎬")
        return

    status = await message.reply("Превращаю в гифку... 🌀")
    tmp_dir = DATA_DIR / "tmp_gif"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    video_path = tmp_dir / f"src_{message.message_id}.mp4"

    try:
        tg_file = await bot.get_file(source.video.file_id)
        raw_path = Path(tg_file.file_path)

        if raw_path.is_absolute() and raw_path.exists():
            src = raw_path
        else:


            matches = list(LOCAL_API_DIR.rglob(raw_path.name))
            if not matches:
                raise GifError("не нашёл скачанный файл на сервере telegram-bot-api")
            src = matches[0]

        await asyncio.to_thread(shutil.copy, src, video_path)
        src.unlink(missing_ok=True)

        gif_path = await asyncio.to_thread(video_to_gif, video_path)
        await message.answer_animation(FSInputFile(str(gif_path.resolve())))
        await stats.record(
            message.from_user.id if message.from_user else 0, "gif", "private",
            username=message.from_user.username if message.from_user else None,
            first_name=message.from_user.first_name if message.from_user else None,
            size_bytes=_sum_sizes(gif_path),
        )
    except GifError as e:
        await message.reply(f"Не вышло сделать гифку: {e}")
    except Exception:
        log.exception("Ошибка при создании гифки")
        await message.reply("Что-то пошло не так при создании гифки 🤷")
    finally:
        await status.delete()
        video_path.unlink(missing_ok=True)
        if 'gif_path' in locals() and gif_path.exists():
            gif_path.unlink(missing_ok=True)

@dp.message(F.text.lower().startswith("озвучь"))
async def handle_voice(message: Message, state: FSMContext) -> None:
    if message.from_user: await save_user(message.from_user.id)

    text_to_say = message.text[6:].strip(" :,-")
    if not text_to_say:
        await message.reply("А что озвучивать? Напиши: «озвучь привет всем» 🎙")
        return

    await state.update_data(text_to_say=text_to_say)
    await state.set_state(VoiceState.waiting_for_voice)

    builder = InlineKeyboardBuilder()
    for name, voice_id in VOICES.items():
        builder.row(InlineKeyboardButton(text=name, callback_data=f"tts_voice:{voice_id}"))

    await message.reply(
        "Выбери, каким голосом озвучить этот текст: 👇",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(StateFilter(VoiceState.waiting_for_voice), F.data.startswith("tts_voice:"))
async def process_tts_voice(callback: CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    text_to_say = user_data.get("text_to_say")

    if not text_to_say:
        await callback.answer("Ошибка: текст потерялся. Попробуй написать команду заново.", show_alert=True)
        await state.clear()
        return

    voice_id = callback.data.split(":")[1]

    await callback.message.edit_text("Озвучиваю... 🎙")

    path = None
    try:
        path, kind = await text_to_speech(text_to_say, voice_id)

        if kind == "voice":
            await callback.message.answer_voice(FSInputFile(path))
        else:
            await callback.message.answer_audio(FSInputFile(path))

        await callback.message.delete()

        await stats.record(
            callback.from_user.id if callback.from_user else 0, "voice", "private",
            username=callback.from_user.username if callback.from_user else None,
            first_name=callback.from_user.first_name if callback.from_user else None,
            size_bytes=path.stat().st_size if path.exists() else 0,
        )

    except TelegramBadRequest as e:
        if "VOICE_MESSAGES_FORBIDDEN" in str(e):
            await callback.message.edit_text("У вас в настройках Telegram запрещены голосовые сообщения 🔇")
        else:
            raise e
    except Exception:
        log.exception("Ошибка озвучки")
        await callback.message.edit_text("Не вышло озвучить 🤷")
    finally:
        await state.clear()
        if path is not None:
            path.unlink(missing_ok=True)


HELP_INLINE_RESULT = InlineQueryResultArticle(
    id="help",
    title="Отправь картинку, которая появится",
    description="после того,как вы вставите ссылку",
    thumb_url="https://i.ibb.co/kgWmWQqX/Gemini-Generated-Image-98ma1u98ma1u98ma.jpg",
    input_message_content=InputTextMessageContent(
        message_text="Вставь ссылку на TikTok,Instagram,Youtube,Pinterest и т.д ..."
    ),
)

def _make_processing_result() -> InlineQueryResultArticle | InlineQueryResultCachedPhoto:
    noop_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏳ Идёт скачивание...", callback_data="noop")]])

    return InlineQueryResultCachedPhoto(
        id="processing",
        photo_file_id=PLACEHOLDER_PHOTO_FILE_ID,
        caption="⏳ Подождите ...",
        reply_markup=noop_kb,
    )

def _error_inline_result(text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=f"error_{hashlib.md5(text.encode()).hexdigest()[:8]}",
        title="Не вышло 🤷",
        description=text[:100],
        input_message_content=InputTextMessageContent(message_text=f"Не вышло: {text}"),
    )


def _inline_results_from_entry(entry: dict, id_prefix: str) -> list:
    title = entry["title"]
    results = []
    for i, item in enumerate(entry["items"]):
        rid = f"{id_prefix}_{i}"
        if item["kind"] == "video":
            results.append(InlineQueryResultCachedVideo(id=rid, video_file_id=item["file_id"], title=title))
        elif item["kind"] == "animation":
            results.append(InlineQueryResultCachedMpeg4Gif(id=rid, mpeg4_file_id=item["file_id"], title=title))
        elif item["kind"] == "photo":
            results.append(InlineQueryResultCachedPhoto(id=rid, photo_file_id=item["file_id"], title=title))
    return results


async def _build_inline_results(url: str) -> tuple[list[dict], str, int]:
    if CACHE_CHAT_ID is None:
        raise VideoDownloadError("инлайн-режим не настроен (нет CACHE_CHAT_ID в .env)")


    cached = inline_cache.get(url)
    if cached:
        return cached["items"], cached["title"], 0

    max_height = INLINE_MAX_HEIGHT if _wants_upfront_quality(url) else None
    video_path, audio_path, photos_list, gif_path = await download_media(url, max_height)


    slide_audio_path = None
    if not gif_path and not video_path and photos_list and len(photos_list) == 1 and audio_path:
        converted = await asyncio.to_thread(photo_to_video, photos_list[0], audio_path)
        if converted:
            video_path = converted
            photos_list = None
            slide_audio_path = audio_path
            audio_path = None


    title = _platform_name(url)[:90]

    items: list[dict] = []
    size_bytes = 0
    try:
        if gif_path:
            msg = await bot.send_animation(CACHE_CHAT_ID, FSInputFile(str(gif_path.resolve())), request_timeout=180)
            items.append({"kind": "animation", "file_id": msg.animation.file_id, "message_id": msg.message_id})
        elif photos_list:


            async def _upload_chunk(chunk: list[Path]) -> list[dict]:
                media_group = [InputMediaPhoto(media=FSInputFile(str(p.resolve()))) for p in chunk]
                messages = await _call_with_flood_retry(
                    lambda mg=media_group: bot.send_media_group(CACHE_CHAT_ID, media=mg, request_timeout=180)
                )
                return [
                    {"kind": "photo", "file_id": msg.photo[-1].file_id, "message_id": msg.message_id}
                    for msg in messages
                ]

            for start in range(0, len(photos_list), 10):
                if start > 0:
                    await asyncio.sleep(_CAROUSEL_SEND_DELAY_SEC)
                items.extend(await _upload_chunk(photos_list[start:start + 10]))
        elif video_path:
            msg = await bot.send_video(CACHE_CHAT_ID, FSInputFile(str(video_path.resolve())), request_timeout=180)
            items.append({"kind": "video", "file_id": msg.video.file_id, "message_id": msg.message_id})
        else:
            raise VideoDownloadError("не нашёл, что показывать по этой ссылке")

        size_bytes = _sum_sizes(video_path, audio_path, gif_path, photos_list)
    finally:
        cleanup_download(video_path, audio_path, gif_path, slide_audio_path, photos_list)

    if not items:
        raise VideoDownloadError("не вышло загрузить медиа для предпросмотра")

    await inline_cache.set(url, items, title)
    return items, title, size_bytes


def _on_inline_task_done(url: str, task: asyncio.Task) -> None:
    _inline_processing.pop(url, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.warning("Фоновая инлайн-обработка %s завершилась с ошибкой: %s", url, exc)


def _get_or_start_inline_task(url: str) -> asyncio.Task:
    task = _inline_processing.get(url)
    if task is None:
        task = asyncio.create_task(_build_inline_results(url))
        task.add_done_callback(lambda t: _on_inline_task_done(url, t))
        _inline_processing[url] = task
    return task


async def _safe_answer_inline(inline_query: InlineQuery, **kwargs) -> None:
    try:
        await inline_query.answer(**kwargs)
    except TelegramBadRequest as e:
        if "query is too old" in str(e) or "QUERY_ID_INVALID" in str(e):
            log.warning("Инлайн-запрос истёк до ответа: %s", e)
        else:
            raise


@dp.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery) -> None:


    await callback.answer()


@dp.inline_query()
async def handle_inline_query(inline_query: InlineQuery) -> None:
    if inline_query.from_user:
        await save_user(inline_query.from_user.id)

    match = URL_PATTERN.search(inline_query.query or "")
    if not match:
        await _safe_answer_inline(inline_query, results=[HELP_INLINE_RESULT], cache_time=1, is_personal=True)
        return

    url = match.group(0)


    cached = inline_cache.get(url)
    if cached:
        await _safe_answer_inline(
            inline_query,
            results=_inline_results_from_entry(cached, "cached"),
            cache_time=600, is_personal=False,
        )
        if inline_query.from_user:
            await stats.record(
                inline_query.from_user.id, "download", "inline",
                username=inline_query.from_user.username,
                first_name=inline_query.from_user.first_name,
                platform=_platform_name(url),
            )
        return


    _get_or_start_inline_task(url)
    await _safe_answer_inline(inline_query, results=[_make_processing_result()], cache_time=1, is_personal=True)


@dp.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult) -> None:
    if chosen.result_id != "processing":
        return

    if not chosen.inline_message_id:
        log.warning(
            "Нет inline_message_id в chosen_inline_result — включи Inline Feedback "
            "в @BotFather (/setinlinefeedback), иначе заглушку не подменить"
        )
        return

    match = URL_PATTERN.search(chosen.query or "")
    if not match:
        return
    url = match.group(0)

    task = _get_or_start_inline_task(url)
    empty_kb = InlineKeyboardMarkup(inline_keyboard=[])
    try:
        items, title, size_bytes = await task
    except VideoDownloadError as e:
        try:
            await bot.edit_message_caption(
                inline_message_id=chosen.inline_message_id, caption=f"Не вышло: {e}", reply_markup=empty_kb
            )
        except TelegramBadRequest:
            pass
        return
    except Exception:
        log.exception("Ошибка фоновой инлайн-обработки %s", url)
        try:
            await bot.edit_message_caption(
                inline_message_id=chosen.inline_message_id, caption="Что-то пошло не так 🤷", reply_markup=empty_kb
            )
        except TelegramBadRequest:
            pass
        return


    item = items[0]
    media_cls = {"video": InputMediaVideo, "animation": InputMediaAnimation, "photo": InputMediaPhoto}[item["kind"]]

    if chosen.from_user:
        await stats.record(
            chosen.from_user.id, "download", "inline",
            username=chosen.from_user.username,
            first_name=chosen.from_user.first_name,
            platform=_platform_name(url),
            size_bytes=size_bytes,
        )

    kb = empty_kb
    if len(items) > 1:
        deep_link = f"https://t.me/{await _get_bot_username()}?start=carousel_{inline_cache.token_for(url)}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"📸 Ещё {len(items) - 1} фото — открыть в боте", url=deep_link)
        ]])

    try:
        await bot.edit_message_media(
            inline_message_id=chosen.inline_message_id,
            media=media_cls(media=item["file_id"]),
            reply_markup=kb,
        )
    except TelegramBadRequest as e:
        log.warning("Не удалось подменить инлайн-сообщение для %s: %s", url, e)


NOTIFIED_FILE = DATA_DIR / "version_notified.json"
UPDATE_CHECK_INTERVAL_SEC = 6 * 3600


def _parse_version(tag: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _read_notified() -> str | None:
    if NOTIFIED_FILE.exists():
        try:
            return json.loads(NOTIFIED_FILE.read_text(encoding="utf-8")).get("tag")
        except (json.JSONDecodeError, OSError, ValueError):
            return None
    return None


def _write_notified(tag: str) -> None:
    try:
        NOTIFIED_FILE.write_text(json.dumps({"tag": tag}), encoding="utf-8")
    except OSError as e:
        log.warning("Не удалось сохранить version_notified.json: %s", e)


async def _fetch_latest_release() -> dict | None:
    url = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "mediagrabber-bot"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        log.warning("Не удалось проверить обновления: %s", e)
        return None


async def _notify_admins_update(tag: str, notes: str) -> None:
    body = notes.strip()
    if len(body) > 2000:
        body = body[:2000].rstrip() + "…"
    text = (
        f"🆕 Вышла новая версия бота: {tag} (у тебя {BOT_VERSION})\n\n"
        f"Что нового:\n{body}\n\n"
        f"Обновиться: git pull && docker compose up -d --build"
    )
    for admin_id in ADMIN_IDS:
        if admin_id == 0:
            continue
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception as e:
            log.warning("Не смог уведомить админа %s об обновлении: %s", admin_id, e)


async def _update_watcher() -> None:
    if not UPDATE_CHECK:
        return
    await asyncio.sleep(15)
    while True:
        release = await _fetch_latest_release()
        if release:
            tag = release.get("tag_name") or ""
            if _parse_version(tag) > _parse_version(BOT_VERSION) and _read_notified() != tag:
                await _notify_admins_update(tag, release.get("body") or "Список изменений — на странице релиза.")
                _write_notified(tag)
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_SEC)


@dp.message(Command("checkupdate"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_checkupdate(message: Message) -> None:
    status = await message.reply("🔎 Проверяю релизы на GitHub...")
    release = await _fetch_latest_release()
    if not release:
        await status.edit_text("Не удалось получить данные с GitHub (см. логи).")
        return

    tag = release.get("tag_name") or "?"
    is_newer = _parse_version(tag) > _parse_version(BOT_VERSION)

    if not is_newer:
        await status.edit_text(f"Обновлений нет. Последний релиз: {tag}, у тебя {BOT_VERSION}.")
        return

    await status.edit_text(f"Найдена новая версия: {tag} (у тебя {BOT_VERSION}). Отправляю уведомление всем админам...")
    await _notify_admins_update(tag, release.get("body") or "Список изменений — на странице релиза.")
    _write_notified(tag)


async def main() -> None:
    log.info("Бот-загрузчик v%s запускается... API=%s, LOCAL_API_DIR=%s, DATA_DIR=%s",
             BOT_VERSION, TELEGRAM_API_URL, LOCAL_API_DIR, DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_update_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
