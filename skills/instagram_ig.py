"""
Скачивание Instagram через instagrapi (приватный API) — замена yt-dlp
Сессия хранится в DATA_DIR/instagram_session.json (instagrapi
dump_settings/load_settings), обновляется после каждого использования.
"""

import asyncio
import logging
import os
import threading
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ClientLoginRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
)

log = logging.getLogger("skills.instagram_ig")

_DATA_DIR = Path(os.getenv("DATA_DIR", "."))
SESSION_FILE = _DATA_DIR / "instagram_session.json"
SEED_COOKIES_FILE = os.getenv("COOKIES_FILE") or "cookies_instagram.txt"
IG_PROXY_URL = os.getenv("PROXY_URL_IG") or os.getenv("PROXY_URL") or None

VIDEO_EXTS = (".mp4", ".mov", ".m4v")

_client: Client | None = None
_client_lock = threading.Lock()


class InstagramAuthError(Exception):
    """Не удалось получить рабочую сессию instagrapi."""

def _extract_sessionid_from_netscape(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[5] == "sessionid":
                    return parts[6]
    except OSError as e:
        log.warning("Не удалось прочитать %s для бутстрапа instagrapi: %s", path, e)
    return None


def _dump(cl: Client) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        cl.dump_settings(str(SESSION_FILE))
    except OSError as e:
        log.warning("Не удалось сохранить сессию instagrapi: %s", e)


def _build_client() -> Client:
    cl = Client()
    if IG_PROXY_URL:
        cl.set_proxy(IG_PROXY_URL)

    if SESSION_FILE.exists():
        cl.load_settings(str(SESSION_FILE))
        try:
            cl.get_timeline_feed()  
            log.info("instagrapi: восстановил сохранённую сессию")
            return cl
        except (LoginRequired, ClientLoginRequired):
            log.warning("Сохранённая сессия instagrapi истекла — пробую перелогиниться по sessionid")

    sessionid = _extract_sessionid_from_netscape(SEED_COOKIES_FILE)
    if not sessionid:
        raise InstagramAuthError(
            f"Нет рабочей сессии instagrapi и не нашёл sessionid в {SEED_COOKIES_FILE} для бутстрапа. "
            "Положи туда свежий Netscape cookies.txt с валидным sessionid и попробуй снова."
        )
    cl.login_by_sessionid(sessionid)
    log.info("instagrapi: залогинился по sessionid из %s, сохраняю сессию", SEED_COOKIES_FILE)
    _dump(cl)
    return cl


def _get_client(force_rebuild: bool = False) -> Client:
    global _client
    with _client_lock:
        if _client is None or force_rebuild:
            _client = _build_client()
        return _client


def _download_sync(url: str, tmp_dir: str) -> tuple[Path | None, Path | None, list[Path] | None, Path | None]:
    tmp = Path(tmp_dir)

    try:
        cl = _get_client()
        media_pk = cl.media_pk_from_url(url)
        info = cl.media_info(media_pk)
    except (LoginRequired, ClientLoginRequired):
        cl = _get_client(force_rebuild=True)
        media_pk = cl.media_pk_from_url(url)
        info = cl.media_info(media_pk)

    try:
        if info.media_type == 2:  
            video_path = cl.video_download(media_pk, folder=tmp)
            _dump(cl)
            return Path(video_path), None, None, None

        if info.media_type == 1:  
            photo_path = cl.photo_download(media_pk, folder=tmp)
            _dump(cl)
            return None, None, [Path(photo_path)], None

        if info.media_type == 8: 
            paths = [Path(p) for p in cl.album_download(media_pk, folder=tmp)]
            video_path = next((p for p in paths if p.suffix.lower() in VIDEO_EXTS), None)
            photos = [p for p in paths if p.suffix.lower() not in VIDEO_EXTS]
            _dump(cl)
            return video_path, None, (photos or None), None

        raise InstagramAuthError(f"Неизвестный тип медиа Instagram (media_type={info.media_type})")

    except (ChallengeRequired, PleaseWaitFewMinutes) as e:
        raise InstagramAuthError(
            "Instagram запросил подтверждение (challenge) или временно ограничил аккаунт. "
            "Зайди на аккаунт вручную через приложение/сайт и подтверди, что это ты, потом попробуй снова."
        ) from e


async def download_via_instagrapi(url: str, tmp_dir: str) -> tuple[Path | None, Path | None, list[Path] | None, Path | None]:
    return await asyncio.to_thread(_download_sync, url, tmp_dir)
