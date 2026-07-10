import logging
import subprocess
from pathlib import Path

log = logging.getLogger("skills.photo_video")


MAX_HEIGHT = 1024


class PhotoVideoError(Exception):
    """Что-то пошло не так при склейке — текст ошибки уйдёт пользователю."""


def photo_to_video(photo_path: Path, audio_path: Path) -> Path | None:
    video_path = photo_path.with_name(f"{photo_path.stem}_slide.mp4")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(photo_path),
                "-i", str(audio_path),
                "-c:v", "libx264", "-tune", "stillimage",
                "-profile:v", "high", "-level", "4.1",
                "-c:a", "aac", "-b:a", "192k",


                "-vf", f"scale=-2:if(gt(ih\\,{MAX_HEIGHT})\\,{MAX_HEIGHT}\\,trunc(ih/2)*2),setsar=1",
                "-pix_fmt", "yuv420p",
                "-shortest",
                "-movflags", "+faststart",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError:
        log.warning("ffmpeg не установлен — фото+звук не собрать в видео")
        return None
    except subprocess.CalledProcessError as e:
        log.warning("Не удалось собрать видео из фото и звука: %s", e.stderr.decode(errors="ignore")[:200])
        return None
    except Exception as e:
        log.warning("Не удалось собрать видео из фото и звука: %s", e)
        return None

    return video_path if video_path.exists() else None
