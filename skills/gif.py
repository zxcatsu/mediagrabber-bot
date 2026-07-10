import logging
import subprocess
from pathlib import Path

log = logging.getLogger("skills.gif")

MAX_DURATION_SEC = 8
TARGET_WIDTH = 480
FPS = 12


class GifError(Exception):
    """Что-то пошло не так при конвертации — текст ошибки уйдёт пользователю."""


def video_to_gif(video_path: Path) -> Path:
    gif_path = video_path.with_suffix(".gif")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-t", str(MAX_DURATION_SEC),
                "-vf", f"fps={FPS},scale={TARGET_WIDTH}:-1:flags=lanczos",
                "-loop", "0",
                str(gif_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as e:
        raise GifError("на сервере не установлен ffmpeg — см. README") from e
    except subprocess.CalledProcessError as e:
        raise GifError(f"ошибка конвертации ({e.stderr.decode(errors='ignore')[:200]})") from e

    if not gif_path.exists():
        raise GifError("гифка не создалась")
    return gif_path
