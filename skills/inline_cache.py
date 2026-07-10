"""
Кэш для инлайн-режима — сопоставляет ссылку с уже загруженными в Telegram
file_id (видео/гифки/фото). Когда кто угодно присылает такую же ссылку
повторно, бот отдаёт готовый file_id мгновенно, без повторного скачивания —
это даёт бейдж "Cached" в результате, как у @wivio_bot.

Хранится на диске в JSON, как users.json в bot.py — простая база для
одного процесса, без отдельной БД.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("skills.inline_cache")

_DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent.parent)))
CACHE_FILE = _DATA_DIR / "inline_cache.json"
_lock = asyncio.Lock()
_cache: dict[str, dict] | None = None


_builtin_set = set


def token_for(url: str) -> str:
    """Короткий стабильный токен для ссылки — используется в /start
    deep-link (например, чтобы открыть полную карусель фото в боте,
    раз Telegram не даёt вставить медиагруппу прямо в инлайн-сообщение).
    Payload у /start ограничен 64 символами и алфавитом [A-Za-z0-9_-],
    поэтому саму ссылку туда не засунуть — передаём этот хэш."""
    return hashlib.sha256(url.strip().encode()).hexdigest()[:16]


def _read_from_disk() -> dict[str, dict]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("inline_cache.json повреждён (%s) — начинаю с пустого кэша", e)
    return {}


async def _save_to_disk() -> None:
    tmp_path = CACHE_FILE.with_suffix(".tmp")
    try:
        await asyncio.to_thread(
            tmp_path.write_text, json.dumps(_cache, ensure_ascii=False), encoding="utf-8"
        )
        await asyncio.to_thread(tmp_path.replace, CACHE_FILE)
    except OSError as e:
        log.warning("Не удалось сохранить inline_cache.json: %s", e)


def get(url: str) -> dict | None:
    """Возвращает {'url': str, 'items': [{'kind', 'file_id', 'message_id'}, ...],
    'title': str, 'ts': float} либо None, если такой ссылки/токена ещё не было в кэше."""
    global _cache
    if _cache is None:
        _cache = _read_from_disk()
    return _cache.get(url.strip())


async def set(url: str, items: list[dict], title: str) -> None:
    global _cache
    async with _lock:
        if _cache is None:
            _cache = _read_from_disk()
        url = url.strip()
        entry = {"url": url, "items": items, "title": title, "ts": time.time()}
        _cache[url] = entry
        _cache[token_for(url)] = entry
        await _save_to_disk()


def iter_entries() -> list[dict]:
    """Уникальные записи кэша (каждая лежит на диске под двумя ключами —
    url и token_for(url), здесь отдаётся одна копия на запись). Нужно для
    скрипта очистки, который ходит по всем записям и решает, что устарело."""
    global _cache
    if _cache is None:
        _cache = _read_from_disk()
    seen_ids = _builtin_set()
    result = []
    for entry in _cache.values():
        if id(entry) in seen_ids:
            continue
        seen_ids.add(id(entry))
        result.append(entry)
    return result


async def delete(url: str) -> None:
    """Убирает запись из кэша по обоим ключам (url и его токену)."""
    global _cache
    async with _lock:
        if _cache is None:
            _cache = _read_from_disk()
        url = url.strip()
        _cache.pop(url, None)
        _cache.pop(token_for(url), None)
        await _save_to_disk()


async def clear_all() -> int:
    """Полностью стирает кэш — и в памяти, и на диске. Возвращает количество
    уникальных записей, которые были удалены (см. iter_entries — на диске
    каждая запись лежит под двумя ключами, здесь считаем по одной на запись)."""
    global _cache
    async with _lock:
        if _cache is None:
            _cache = _read_from_disk()
        count = len(iter_entries())
        _cache = {}
        await _save_to_disk()
        return count
