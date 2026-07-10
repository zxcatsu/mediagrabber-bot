import asyncio
import tempfile
from pathlib import Path

import edge_tts

MAX_CHARS = 800


async def _synthesize_mp3(text: str, voice: str) -> Path:
    text = text[:MAX_CHARS]
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(tmp.name)
    return Path(tmp.name)


async def text_to_speech(text: str, voice: str) -> tuple[Path, str]:
    mp3_path = await _synthesize_mp3(text, voice)
    ogg_path = mp3_path.with_suffix(".ogg")

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(mp3_path),
            "-c:a", "libopus", "-b:a", "32k", str(ogg_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0 and ogg_path.exists():
            mp3_path.unlink(missing_ok=True)
            return ogg_path, "voice"
    except FileNotFoundError:
        pass

    return mp3_path, "audio"
