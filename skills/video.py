import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp

log = logging.getLogger("skills.video")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        log.warning("%s=%r не число — беру дефолт %s", name, raw, default)
        return default


MAX_FILE_SIZE_MB = _env_int("MAX_FILE_SIZE_MB", 300)
MAX_FILE_SIZE_MB_HQ = _env_int("MAX_FILE_SIZE_MB_HQ", 1500)
MAX_VIDEO_HEIGHT = _env_int("MAX_VIDEO_HEIGHT", 720)

TMP_PREFIX = "media_dl_"
TIKWM_API = "https://www.tikwm.com/api/"
PINTEREST_API = "https://www.pinterest.com/resource/PinResource/get/"
PINTEREST_PIN_ID_RE = re.compile(r"/pin/(?:[\w-]+--)?(\d+)")
IMAGE_EXTS = ("jpg", "jpeg", "webp", "png", "gif")
MAX_CAROUSEL_ITEMS = 100

PROXY_URL = os.getenv("PROXY_URL") or None
PROXY_URL_RU = os.getenv("PROXY_URL_RU") or None
COOKIES_FILE = os.getenv("COOKIES_FILE") or "cookies_instagram.txt"

_DATA_DIR = Path(os.getenv("DATA_DIR", "."))
WORKING_COOKIES = _DATA_DIR / "cookies_instagram.working.txt"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class VideoDownloadError(Exception):
    """Ошибка скачивания — её текст уходит пользователю."""


class FileTooLargeError(VideoDownloadError):
    def __init__(self, current_height: int, available_heights: list[int]):
        self.current_height = current_height
        self.available_heights = available_heights
        super().__init__("файл слишком большой для текущего качества")


def cleanup_download(*paths_or_lists) -> None:
    for item in paths_or_lists:
        if not item:
            continue
        candidates = item if isinstance(item, (list, tuple)) else [item]
        for p in candidates:
            if not p:
                continue
            parent = Path(p).parent
            if parent.name.startswith(TMP_PREFIX):
                shutil.rmtree(parent, ignore_errors=True)
                return


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in urlparse(url).netloc.lower()


def _is_pinterest(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "pinterest." in host or host == "pin.it" or host.endswith(".pin.it")


def _is_instagram(url: str) -> bool:
    return "instagram.com" in urlparse(url).netloc.lower()


def _is_ru_domain(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith((".ru", ".su")) or host in ("vk.com",) or host.endswith(".vk.com")


def _proxy_for(url: str) -> str | None:
    if _is_ru_domain(url) and PROXY_URL_RU:
        return PROXY_URL_RU
    return PROXY_URL


def _current_cookies_source() -> str | None:
    if WORKING_COOKIES.exists():
        return str(WORKING_COOKIES)
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        return COOKIES_FILE
    return None


def _persist_cookies(tmp_cookies: Path) -> None:
    try:
        if tmp_cookies.exists():
            WORKING_COOKIES.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(tmp_cookies, WORKING_COOKIES)
    except OSError as e:
        log.warning("Не удалось сохранить обновлённые куки: %s", e)


def _base_opts(
    tmp_dir: str,
    max_filesize_mb: int = MAX_FILE_SIZE_MB,
    use_cookies: bool = False,
    progress_hook=None,
    proxy_url: str | None = PROXY_URL,
) -> dict:
    opts = {
        "outtmpl": f"{tmp_dir}/%(autonumber)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "max_filesize": max_filesize_mb * 1024 * 1024,
        "http_headers": {"User-Agent": UA},
    }
    if proxy_url:
        opts["proxy"] = proxy_url
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if use_cookies:
        source = _current_cookies_source()
        if source:
            try:
                cookies_copy = Path(tmp_dir) / "cookies.txt"
                shutil.copy(source, cookies_copy)
                opts["cookiefile"] = str(cookies_copy)
            except OSError as e:
                log.warning("Не удалось скопировать файл кук (%s) — качаю без них", e)
    return opts


def _has_real_video(formats: list[dict]) -> bool:
    return any(f.get("vcodec") not in (None, "none") for f in formats)


def _is_image_only(info: dict) -> bool:
    formats = info.get("formats") or []
    if formats:
        has_images = any(f.get("ext", "").lower() in IMAGE_EXTS for f in formats)
        return has_images and not _has_real_video(formats)
    return info.get("ext", "").lower() in IMAGE_EXTS


def _lower_heights(current_height: int) -> list[int]:
    ladder = [360, 480, 720, 1080, 1440, 2160]
    return [h for h in ladder if h < current_height]


def _has_audio_stream(video_path: Path) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=20,
        )
        return bool(result.stdout.strip())
    except Exception as e:
        log.warning("ffprobe не смог проверить наличие звука: %s", e)
        return True


def _extract_audio_local(video_path: Path) -> Path | None:
    if not _has_audio_stream(video_path):
        log.info("В скачанном видео %s нет аудиодорожки", video_path)
        return None
    audio_path = video_path.with_name("audio.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(audio_path)],
            check=True, capture_output=True, timeout=60,
        )
        return audio_path if audio_path.exists() else None
    except FileNotFoundError:
        log.warning("ffmpeg не установлен — пропускаю извлечение звука")
        return None
    except Exception as e:
        log.warning("Не удалось достать звук локально: %s", e)
        return None


def _download_one_video(url: str, tmp_dir: str, base_opts: dict, name: str = "video", max_height: int | None = None) -> Path | None:
    height = max_height or MAX_VIDEO_HEIGHT
    video_opts = {
        **base_opts,
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/best",
        "merge_output_format": "mp4",
        "outtmpl": f"{tmp_dir}/{name}.%(ext)s",
    }
    with yt_dlp.YoutubeDL(video_opts) as ydl:
        ydl.download([url])
    return next(Path(tmp_dir).glob(f"{name}.*"), None)


def _download_one_photo(url: str, tmp_dir: str, base_opts: dict, name: str) -> Path | None:
    photo_opts = {**base_opts, "format": "best", "outtmpl": f"{tmp_dir}/{name}.%(ext)s"}
    with yt_dlp.YoutubeDL(photo_opts) as ydl:
        ydl.download([url])
    return next(Path(tmp_dir).glob(f"{name}.*"), None)


def _download_slideshow_audio(url: str, tmp_dir: str, base_opts: dict) -> Path | None:
    audio_opts = {
        **base_opts,
        "format": "bestaudio/best",
        "outtmpl": f"{tmp_dir}/audio.%(ext)s",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    try:
        with yt_dlp.YoutubeDL(audio_opts) as ydl:
            ydl.download([url])
        candidate = Path(tmp_dir) / "audio.mp3"
        return candidate if candidate.exists() else None
    except Exception:
        log.warning("Не удалось достать звук слайдшоу — пропускаю")
        return None


def _download_entries(tmp_dir: str, base_opts: dict, entries: list[dict], max_height: int | None = None) -> tuple[Path | None, Path | None, list[Path] | None]:
    photos: list[Path] = []
    video_path: Path | None = None
    for i, entry in enumerate(entries):
        if len(photos) >= MAX_CAROUSEL_ITEMS:
            break
        entry_url = entry.get("webpage_url") or entry.get("url")
        if not entry_url:
            continue
        try:
            if _is_image_only(entry):
                photo = _download_one_photo(entry_url, tmp_dir, base_opts, f"photo_{i}")
                if photo:
                    photos.append(photo)
            elif video_path is None:
                video_path = _download_one_video(entry_url, tmp_dir, base_opts, "video", max_height=max_height)
        except Exception:
            log.warning("Не удалось скачать элемент карусели #%s", i)
            continue
    audio_path = _extract_audio_local(video_path) if video_path else None
    return video_path, audio_path, (photos or None)


def _download_via_ytdlp(url: str, tmp_dir: str, max_height: int | None = None, progress_hook=None) -> tuple[Path | None, Path | None, list[Path] | None]:
    hq = bool(max_height) and max_height > MAX_VIDEO_HEIGHT
    max_filesize_mb = MAX_FILE_SIZE_MB_HQ if hq else MAX_FILE_SIZE_MB
    use_cookies = _is_instagram(url)
    base_opts = _base_opts(
        tmp_dir, max_filesize_mb, use_cookies=use_cookies,
        progress_hook=progress_hook, proxy_url=_proxy_for(url),
    )

    try:
        with yt_dlp.YoutubeDL({**base_opts, "skip_download": True}) as probe:
            info = probe.extract_info(url, download=False)

        entries = info.get("entries")
        if entries:
            return _download_entries(tmp_dir, base_opts, list(entries), max_height)

        if _is_image_only(info):
            photo = _download_one_photo(url, tmp_dir, base_opts, "photo_0")
            photos = [photo] if photo else []
            audio_path = _download_slideshow_audio(url, tmp_dir, base_opts)
            return None, audio_path, (photos or None)

        video_path = _download_one_video(url, tmp_dir, base_opts, max_height=max_height)
        if not video_path:
            _raise_if_too_large(info, max_filesize_mb, max_height)
        audio_path = _extract_audio_local(video_path) if video_path else None
        return video_path, audio_path, None
    finally:
        if use_cookies:
            cookies_copy = Path(tmp_dir) / "cookies.txt"
            if cookies_copy.exists():
                _persist_cookies(cookies_copy)


def _raise_if_too_large(info: dict, max_filesize_mb: int, max_height: int | None) -> None:
    formats = info.get("formats") or []
    if not _has_real_video(formats):
        return
    sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats if f.get("vcodec") not in (None, "none")
    ]
    limit = max_filesize_mb * 1024 * 1024
    if sizes and min(sizes) > limit:
        current = max_height or MAX_VIDEO_HEIGHT
        raise FileTooLargeError(current, _lower_heights(current))


async def _fetch_file(session: aiohttp.ClientSession, file_url: str, dest: Path) -> Path | None:
    try:
        async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=60), proxy=PROXY_URL) as r:
            if r.status != 200:
                return None
            dest.write_bytes(await r.read())
            return dest
    except Exception:
        log.warning("tikwm: не получилось скачать файл %s", file_url)
        return None


async def _download_via_tikwm(url: str, tmp_dir: str) -> tuple[Path | None, Path | None, list[Path] | None]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            TIKWM_API, data={"url": url, "hd": "1"},
            timeout=aiohttp.ClientTimeout(total=30), proxy=PROXY_URL,
        ) as resp:
            if resp.status != 200:
                raise VideoDownloadError(f"tikwm.com ответил кодом {resp.status}")
            payload = await resp.json(content_type=None)

        if payload.get("code") != 0:
            raise VideoDownloadError(f"tikwm.com: {payload.get('msg', 'неизвестная ошибка')}")

        data = payload.get("data") or {}
        images = data.get("images") or []
        video_path: Path | None = None
        photos: list[Path] = []

        if images:
            sem = asyncio.Semaphore(6)

            async def _dl(i: int, img_url: str) -> Path | None:
                async with sem:
                    return await _fetch_file(session, img_url, Path(tmp_dir) / f"photo_{i}.jpg")

            limited = images[:MAX_CAROUSEL_ITEMS]
            results = await asyncio.gather(*(_dl(i, u) for i, u in enumerate(limited)))
            photos = [p for p in results if p]
        else:
            play_url = data.get("play") or data.get("wmplay") or data.get("hdplay")
            if play_url:
                video_path = await _fetch_file(session, play_url, Path(tmp_dir) / "video.mp4")

        audio_path = None
        if video_path:
            audio_path = await asyncio.to_thread(_extract_audio_local, video_path)
            if audio_path is None and data.get("music"):
                log.info("tikwm: видео без звука, достаю музыку отдельной ссылкой")
                audio_path = await _fetch_file(session, data["music"], Path(tmp_dir) / "audio.mp3")
        elif data.get("music"):
            audio_path = await _fetch_file(session, data["music"], Path(tmp_dir) / "audio.mp3")

        return video_path, audio_path, (photos or None)


def _best_pinterest_image(images: dict | None) -> str | None:
    if not isinstance(images, dict):
        return None
    orig = images.get("orig")
    if isinstance(orig, dict) and orig.get("url"):
        return orig["url"]
    best_url, best_width = None, -1
    for size in images.values():
        if not isinstance(size, dict):
            continue
        img_url = size.get("url")
        if not img_url:
            continue
        try:
            width = int(size.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        if width > best_width:
            best_url, best_width = img_url, width
    return best_url


async def _resolve_pinterest_pin_id(session: aiohttp.ClientSession, url: str) -> str | None:
    match = PINTEREST_PIN_ID_RE.search(url)
    if match:
        return match.group(1)
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=20), proxy=PROXY_URL,
            headers={"User-Agent": UA}, allow_redirects=True,
        ) as resp:
            match = PINTEREST_PIN_ID_RE.search(str(resp.url))
            return match.group(1) if match else None
    except Exception:
        log.warning("Pinterest: не удалось развернуть короткую ссылку %s", url)
        return None


async def _download_via_pinterest(url: str, tmp_dir: str) -> tuple[Path | None, Path | None, list[Path] | None, Path | None]:
    async with aiohttp.ClientSession() as session:
        pin_id = await _resolve_pinterest_pin_id(session, url)
        if not pin_id:
            raise VideoDownloadError("не удалось определить ID пина Pinterest")

        query = {"data": json.dumps({"options": {"field_set_key": "unauth_react_main_pin", "id": pin_id}})}
        headers = {"X-Pinterest-PWS-Handler": "www/[username].js", "User-Agent": UA}

        async with session.get(
            PINTEREST_API, params=query, headers=headers,
            timeout=aiohttp.ClientTimeout(total=20), proxy=PROXY_URL,
        ) as resp:
            if resp.status != 200:
                raise VideoDownloadError(f"Pinterest API ответил кодом {resp.status}")
            payload = await resp.json(content_type=None)

        pin_data = (payload.get("resource_response") or {}).get("data") or {}
        image_url = _best_pinterest_image(pin_data.get("images"))
        if not image_url:
            pages = ((pin_data.get("story_pin_data") or {}).get("pages")) or [{}]
            for block in pages[0].get("blocks") or []:
                image_url = _best_pinterest_image((block.get("image") or {}).get("images"))
                if image_url:
                    break

        if not image_url:
            raise VideoDownloadError("не нашёл картинку или гифку в этом пине")

        clean_url = image_url.split("?")[0]
        ext = clean_url.rsplit(".", 1)[-1].lower() if "." in clean_url else "jpg"
        if ext not in IMAGE_EXTS:
            ext = "jpg"

        dest = await _fetch_file(session, image_url, Path(tmp_dir) / f"pin_0.{ext}")
        if not dest:
            raise VideoDownloadError("не вышло скачать картинку с Pinterest")

        if ext == "gif":
            return None, None, None, dest
        return None, None, [dest], None


async def download_media(url: str, max_height: int | None = None, progress_hook=None) -> tuple[Path | None, Path | None, list[Path] | None, Path | None]:
    tmp_dir = tempfile.mkdtemp(prefix=TMP_PREFIX)
    try:
        if _is_tiktok(url):
            try:
                video_path, audio_path, photos = await _download_via_tikwm(url, tmp_dir)
                if any((video_path, audio_path, photos)):
                    return video_path, audio_path, photos, None
            except Exception as e:
                log.warning("tikwm не справился: %s", e)
            try:
                video_path, audio_path, photos = await asyncio.to_thread(_download_via_ytdlp, url, tmp_dir, max_height, progress_hook)
                if any((video_path, audio_path, photos)):
                    return video_path, audio_path, photos, None
            except Exception as e:
                raise VideoDownloadError(f"Не получилось скачать ни через tikwm, ни через yt-dlp ({e})") from e
            raise VideoDownloadError("Не удалось скачать TikTok")

        if _is_pinterest(url):
            try:
                video_path, audio_path, photos = await asyncio.to_thread(_download_via_ytdlp, url, tmp_dir, max_height, progress_hook)
                if any((video_path, audio_path)):
                    return video_path, audio_path, None, None
            except Exception as e:
                log.info("yt-dlp не справился с Pinterest, возможно это фото или гиф: %s", e)
            try:
                result = await _download_via_pinterest(url, tmp_dir)
                if any(result):
                    return result
            except Exception as e:
                raise VideoDownloadError(f"Не получилось скачать ни через yt-dlp, ни через Pinterest ({e})") from e

        try:
            video_path, audio_path, photos = await asyncio.to_thread(_download_via_ytdlp, url, tmp_dir, max_height, progress_hook)
            if any((video_path, audio_path, photos)):
                return video_path, audio_path, photos, None
        except FileTooLargeError:
            raise
        except Exception as e:
            raise VideoDownloadError(str(e)) from e

        raise VideoDownloadError("Не нашёл медиа по этой ссылке")
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
