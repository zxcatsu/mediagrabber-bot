import asyncio
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("skills.stats")

_DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent.parent)))
STATS_FILE = _DATA_DIR / "stats.json"
_lock = asyncio.Lock()
_data: dict | None = None

KINDS = ("download", "gif", "voice")
SOURCES = ("private", "inline")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _empty() -> dict:
    return {
        "users": {},
        "daily": {},
        "totals": {"download": 0, "gif": 0, "voice": 0, "bytes": 0},
    }


def _read_from_disk() -> dict:
    if STATS_FILE.exists():
        try:
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            data.setdefault("users", {})
            data.setdefault("daily", {})
            data.setdefault("totals", {"download": 0, "gif": 0, "voice": 0, "bytes": 0})
            return data
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("stats.json повреждён (%s) — начинаю с пустой статистики", e)
    return _empty()


async def _save_to_disk() -> None:
    tmp_path = STATS_FILE.with_suffix(".tmp")
    try:
        await asyncio.to_thread(
            tmp_path.write_text, json.dumps(_data, ensure_ascii=False), encoding="utf-8"
        )
        await asyncio.to_thread(tmp_path.replace, STATS_FILE)
    except OSError as e:
        log.warning("Не удалось сохранить stats.json: %s", e)


def _ensure_loaded() -> dict:
    global _data
    if _data is None:
        _data = _read_from_disk()
    return _data


async def record(
    user_id: int,
    kind: str,
    source: str,
    *,
    username: str | None = None,
    first_name: str | None = None,
    platform: str | None = None,
    size_bytes: int = 0,
) -> None:
    if kind not in KINDS or source not in SOURCES:
        log.warning("stats.record: неизвестный kind=%r/source=%r — пропускаю", kind, source)
        return

    async with _lock:
        data = _ensure_loaded()
        now = time.time()
        uid = str(user_id)

        user = data["users"].setdefault(uid, {
            "first_seen": now, "last_seen": now,
            "username": None, "first_name": None,
            "counts": {"download": 0, "gif": 0, "voice": 0},
            "sources": {"private": 0, "inline": 0},
            "bytes": 0,
            "last_platform": None,
        })
        user["last_seen"] = now
        if username:
            user["username"] = username
        if first_name:
            user["first_name"] = first_name
        user["counts"][kind] = user["counts"].get(kind, 0) + 1
        user["sources"][source] = user["sources"].get(source, 0) + 1
        user["bytes"] = user.get("bytes", 0) + size_bytes
        if platform:
            user["last_platform"] = platform

        day = data["daily"].setdefault(_today(), {
            "counts": {"download": 0, "gif": 0, "voice": 0}, "users": []
        })
        day["counts"][kind] = day["counts"].get(kind, 0) + 1
        if user_id not in day["users"]:
            day["users"].append(user_id)

        data["totals"][kind] = data["totals"].get(kind, 0) + 1
        data["totals"]["bytes"] = data["totals"].get("bytes", 0) + size_bytes

        await _save_to_disk()


def summary() -> dict:
    data = _ensure_loaded()
    today = data["daily"].get(_today(), {"counts": {}, "users": []})
    return {
        "users_total": len(data["users"]),
        "downloads_total": data["totals"].get("download", 0),
        "gif_total": data["totals"].get("gif", 0),
        "voice_total": data["totals"].get("voice", 0),
        "bytes_total": data["totals"].get("bytes", 0),
        "downloads_today": today["counts"].get("download", 0),
        "gif_today": today["counts"].get("gif", 0),
        "voice_today": today["counts"].get("voice", 0),
        "active_today": len(today["users"]),
    }


def user_info(user_id: int) -> dict | None:
    data = _ensure_loaded()
    return data["users"].get(str(user_id))


def list_users() -> list[tuple[int, dict]]:
    data = _ensure_loaded()
    items = [(int(uid), info) for uid, info in data["users"].items()]
    items.sort(key=lambda pair: pair[1].get("last_seen", 0), reverse=True)
    return items
