import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger("skills.cobalt")

COBALT_API_URL = os.getenv("COBALT_API_URL") or "http://cobalt-api:9000/"
COBALT_API_KEY = os.getenv("COBALT_API_KEY") or None
PROXY_URL = os.getenv("PROXY_URL") or None

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")
AUDIO_EXTS = (".mp3", ".m4a", ".ogg", ".opus", ".wav")
MAX_CAROUSEL_ITEMS = 100

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class CobaltError(Exception):
    pass


def _headers() -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    return headers


def _ext_from_url(url: str, default: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
        if len(ext) <= 6:
            return ext
    return default


def _safe_name(name: str, fallback: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base or fallback


def _picker_filename(item: dict, index: int, fallback_ext: str) -> str:
    name = item.get("filename")
    if name:
        return f"{index}_{_safe_name(name, f'file{fallback_ext}')}"
    return f"file_{index}{_ext_from_url(item.get('url', ''), fallback_ext)}"


async def _post(session: aiohttp.ClientSession, url: str) -> dict:
    try:
        async with session.post(
            COBALT_API_URL,
            json={"url": url},
            headers=_headers(),
            timeout=aiohttp.ClientTimeout(total=60),
            proxy=PROXY_URL,
        ) as resp:
            payload = await resp.json(content_type=None)
    except aiohttp.ClientError as e:
        raise CobaltError(f"Не удалось связаться с cobalt: {e}") from e
    except asyncio.TimeoutError as e:
        raise CobaltError("cobalt не ответил вовремя") from e

    if not isinstance(payload, dict):
        raise CobaltError("cobalt вернул неожиданный ответ")

    status = payload.get("status")
    if status == "error":
        text = (((payload.get("error") or {}).get("code")) or "неизвестная ошибка")
        raise CobaltError(f"cobalt не смог обработать ссылку ({text})")

    return payload


async def _fetch_file(session: aiohttp.ClientSession, file_url: str, dest: Path) -> Path | None:
    try:
        async with session.get(
            file_url,
            timeout=aiohttp.ClientTimeout(total=300, sock_read=60),
            proxy=PROXY_URL,
            headers={"User-Agent": UA},
        ) as r:
            if r.status != 200:
                return None
            with dest.open("wb") as f:
                async for chunk in r.content.iter_chunked(1 << 16):
                    f.write(chunk)
            return dest
    except Exception:
        log.warning("cobalt: не удалось скачать файл %s", file_url)
        dest.unlink(missing_ok=True)
        return None


def _split_media(paths: list[Path]) -> tuple[Path | None, Path | None, list[Path] | None]:
    video: Path | None = None
    audio: Path | None = None
    photos: list[Path] = []
    for p in paths:
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            video = video or p
        elif ext in AUDIO_EXTS:
            audio = audio or p
        else:
            photos.append(p)
    return video, audio, (photos or None)


async def download_via_cobalt(url: str, tmp_dir: str) -> tuple[Path | None, Path | None, list[Path] | None, Path | None]:
    tmp = Path(tmp_dir)
    async with aiohttp.ClientSession() as session:
        payload = await _post(session, url)
        status = payload.get("status")

        if status in ("tunnel", "redirect", "stream"):
            file_url = payload.get("url")
            if not file_url:
                raise CobaltError("cobalt не вернул ссылку на файл")
            default_name = f"media{_ext_from_url(file_url, '.mp4')}"
            raw_name = payload.get("filename")
            filename = _safe_name(raw_name, default_name) if raw_name else default_name
            dest = await _fetch_file(session, file_url, tmp / filename)
            if not dest:
                raise CobaltError("не удалось скачать файл, отданный cobalt")
            video, audio, photos = _split_media([dest])
            return video, audio, photos, None

        if status == "picker":
            picker = payload.get("picker") or []
            audio_url = payload.get("audio")

            sem = asyncio.Semaphore(6)

            async def _dl(i: int, item: dict) -> Path | None:
                async with sem:
                    item_url = item.get("url")
                    if not item_url:
                        return None
                    fallback = ".mp4" if item.get("type") == "video" else ".jpg"
                    name = _picker_filename(item, i, fallback)
                    return await _fetch_file(session, item_url, tmp / name)

            limited = picker[:MAX_CAROUSEL_ITEMS]
            results = await asyncio.gather(*(_dl(i, it) for i, it in enumerate(limited)))
            downloaded = [p for p in results if p]

            audio_path: Path | None = None
            if audio_url:
                audio_path = await _fetch_file(session, audio_url, tmp / f"audio{_ext_from_url(audio_url, '.mp3')}")

            video, picker_audio, photos = _split_media(downloaded)
            return video, (audio_path or picker_audio), photos, None

        if status == "local-processing":
            raise CobaltError(
                "cobalt отдал медиа с постобработкой на стороне клиента "
                "(local-processing) — этот режим бот пока не поддерживает"
            )

        raise CobaltError(f"cobalt вернул статус {status!r}, с которым бот не умеет работать")
