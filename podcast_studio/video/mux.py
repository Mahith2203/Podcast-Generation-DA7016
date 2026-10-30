"""ffmpeg: picture list + the finished episode audio -> one MP4 that plays everywhere (H.264 + AAC, faststart)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .. import audio, tuning


class MuxError(RuntimeError):
    """Raised when the video cannot be encoded."""
    pass


def ffprobe_path() -> Optional[str]:
    """Path of `ffprobe` (on PATH, or next to `ffmpeg`), or None if it is not installed."""
    p = shutil.which("ffprobe")
    if p:
        return p
    ff = audio.ffmpeg_path()
    if ff:
        cand = Path(ff).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if cand.exists():
            return str(cand)
    return None


def has_x264() -> bool:
    """True if the installed ffmpeg can encode H.264 video (`libx264`)."""
    ff = audio.ffmpeg_path()
    if not ff:
        return False
    try:
        out = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
        return "libx264" in out
    except Exception:  # noqa: BLE001
        return False


def require_tools() -> None:
    """Fail fast (before any model call or drawing) when ffmpeg or its H.264 encoder is missing."""
    if not audio.ffmpeg_path():
        raise MuxError("ffmpeg was not found. Install it (Windows: `winget install Gyan.FFmpeg`), reopen the terminal and try again.")
    if not has_x264():
        raise MuxError("This ffmpeg has no H.264 encoder (libx264). Install the full build: `winget install Gyan.FFmpeg`.")


def write_concat(frames: list[tuple[Path, float]], list_path: Path) -> None:
    """All frames sit in one folder; names only (no drive letters or spaces to escape)."""
    lines = []
    for p, d in frames:
        lines.append(f"file '{p.name}'")
        lines.append(f"duration {max(d, 0.001):.5f}")
    lines.append(f"file '{frames[-1][0].name}'")  # the concat demuxer ignores the last duration unless the file is repeated
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_mp4(frames: list[tuple[Path, float]], audio_wav: Path, out_mp4: Path, *, fps: int, duration: float, frames_dir: Path) -> None:
    """Encode the still frames and the episode audio into an MP4 with ffmpeg: H.264 video, AAC audio at the tuned
    loudness, cut to `duration`. It writes to a temporary file first and renames it when done.
    """
    require_tools()
    ff = audio.ffmpeg_path()
    if not frames:
        raise MuxError("No frames to encode.")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_mp4.with_name(out_mp4.stem + ".part.mp4")
    list_path = frames_dir / f"list_{os.getpid()}_{out_mp4.stem}.txt"
    write_concat(frames, list_path)
    lufs = tuning.current().final_loudness_lufs
    cmd = [ff, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_path.name, "-i", str(audio_wav.resolve()),
           "-map", "0:v:0", "-map", "1:a:0",
           "-vf", f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-tune", "stillimage", "-pix_fmt", "yuv420p",
           "-af", f"loudnorm=I={lufs:g}:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
           "-t", f"{duration:.3f}", "-movflags", "+faststart", str(tmp_out.resolve())]
    try:
        p = subprocess.run(cmd, capture_output=True, cwd=str(frames_dir))
    finally:
        list_path.unlink(missing_ok=True)
    if p.returncode != 0 or not tmp_out.exists():
        tmp_out.unlink(missing_ok=True)
        msg = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise MuxError("ffmpeg could not build the video: " + (" | ".join(msg[-3:])[:400] or f"exit code {p.returncode}"))
    os.replace(tmp_out, out_mp4)


def probe(path: Path) -> dict:
    """{'video': bool, 'audio': bool, 'duration': float, 'width': int, 'height': int, 'fps': float, 'vcodec': str, 'acodec': str}.
    Uses ffprobe when present, otherwise parses `ffmpeg -i`."""
    info = {"video": False, "audio": False, "duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "vcodec": "", "acodec": ""}
    fp = ffprobe_path()
    if fp:
        import json
        p = subprocess.run([fp, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True)
        if p.returncode == 0:
            d = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
            for s in d.get("streams", []):
                if s.get("codec_type") == "video" and not info["video"]:
                    info.update(video=True, width=int(s.get("width", 0)), height=int(s.get("height", 0)), vcodec=s.get("codec_name", ""))
                    num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
                    try:
                        info["fps"] = float(num) / float(den or 1)
                    except (ValueError, ZeroDivisionError):
                        pass
                elif s.get("codec_type") == "audio" and not info["audio"]:
                    info.update(audio=True, acodec=s.get("codec_name", ""))
            try:
                info["duration"] = float(d.get("format", {}).get("duration", 0))
            except (TypeError, ValueError):
                pass
            return info
    ff = audio.ffmpeg_path()
    if not ff:
        return info
    txt = subprocess.run([ff, "-hide_banner", "-i", str(path)], capture_output=True).stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", txt)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Video:\s*(\w+).*?,\s*(\d{2,5})x(\d{2,5})", txt)
    if m:
        info.update(video=True, vcodec=m[1], width=int(m[2]), height=int(m[3]))
    m = re.search(r"([\d.]+)\s*fps", txt)
    if m:
        info["fps"] = float(m[1])
    m = re.search(r"Audio:\s*(\w+)", txt)
    if m:
        info.update(audio=True, acodec=m[1])
    return info
