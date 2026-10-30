"""Audio helpers. Everything is float32 mono at 24 kHz (the Bodhan TTS output format: PCM16, 24 kHz, mono).

Only numpy + the stdlib `wave` module are required. ffmpeg is used when present for (a) MP3 export with
loudness normalisation and (b) decoding unusual WAV flavours the stdlib cannot read.
"""
from __future__ import annotations

import io
import math
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 24000


class AudioError(RuntimeError):
    """Raised when audio cannot be decoded, encoded or processed."""
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


# --------------------------------------------------------------------------- decode / encode


def _resample(x: np.ndarray, sr_in: int, sr_out: int = SR) -> np.ndarray:
    if sr_in == sr_out or len(x) == 0:
        return x
    n_out = int(round(len(x) * sr_out / sr_in))
    t_in = np.linspace(0.0, 1.0, num=len(x), endpoint=False)
    t_out = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(t_out, t_in, x).astype(np.float32)


def wav_bytes_to_array(data: bytes) -> np.ndarray:
    """Decode WAV bytes to float32 mono @ SR. Raises AudioError on garbage."""
    if not data or data[:4] != b"RIFF":
        raise AudioError("Response is not a WAV file (missing RIFF header).")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
        if sw == 2:
            x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif sw == 1:
            x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sw == 4:
            x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        elif sw == 3:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
            v = np.where(v >= 1 << 23, v - (1 << 24), v)
            x = v.astype(np.float32) / 8388608.0
        else:
            raise AudioError(f"Unsupported sample width {sw}")
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        return _resample(x.astype(np.float32), sr)
    except (wave.Error, EOFError) as e:
        return _decode_with_ffmpeg(data, e)


def _decode_with_ffmpeg(data: bytes, original: Exception) -> np.ndarray:
    ff = ffmpeg_path()
    if not ff:
        raise AudioError(f"Could not decode WAV ({original}) and ffmpeg is not installed to try another decoder.")
    p = subprocess.run(
        [ff, "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(SR), "pipe:1"],
        input=data, capture_output=True,
    )
    if p.returncode != 0 or not p.stdout:
        raise AudioError(f"Could not decode audio: {p.stderr.decode('utf-8', 'replace')[:200]}")
    return np.frombuffer(p.stdout, dtype="<i2").astype(np.float32) / 32768.0


def array_to_wav_bytes(x: np.ndarray) -> bytes:
    """Encode a mono float array (values -1..1) as 16-bit PCM WAV bytes at the project sample rate `SR`."""
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def write_wav(path: Path, x: np.ndarray) -> None:
    from .util import atomic_write_bytes

    atomic_write_bytes(Path(path), array_to_wav_bytes(x))


def read_wav(path: Path) -> np.ndarray:
    return wav_bytes_to_array(Path(path).read_bytes())


def duration(x: np.ndarray) -> float:
    return len(x) / SR


def encode_mp3(wav_path: Path, mp3_path: Path, loudnorm: bool = True) -> bool:
    """MP3 via ffmpeg (EBU R128 loudness ~ -16 LUFS, the usual podcast target). Returns False if unavailable."""
    ff = ffmpeg_path()
    if not ff:
        return False
    cmd = [ff, "-v", "error", "-y", "-i", str(wav_path)]
    if loudnorm:
        from . import tuning
        cmd += ["-af", f"loudnorm=I={tuning.current().final_loudness_lufs:g}:TP=-1.5:LRA=11"]
    cmd += ["-ar", "44100", "-ac", "1", "-codec:a", "libmp3lame", "-q:a", "3", str(mp3_path)]
    p = subprocess.run(cmd, capture_output=True)
    return p.returncode == 0 and Path(mp3_path).exists()


# --------------------------------------------------------------------------- shaping


def silence(ms: float) -> np.ndarray:
    return np.zeros(int(SR * ms / 1000.0), dtype=np.float32)


def _frame_db(x: np.ndarray, frame: int) -> np.ndarray:
    n = len(x) // frame
    if n == 0:
        return np.array([-120.0])
    f = x[: n * frame].reshape(n, frame)
    rms = np.sqrt((f ** 2).mean(axis=1) + 1e-12)
    return 20.0 * np.log10(rms + 1e-12)


def trim_silence(x: np.ndarray, thresh_db: float = -45.0, pad_ms: float = 40.0) -> np.ndarray:
    """Remove leading/trailing near-silence (TTS engines pad clips), keeping a short natural pad."""
    if len(x) == 0:
        return x
    frame = int(SR * 0.01)
    db = _frame_db(x, frame)
    idx = np.where(db > thresh_db)[0]
    if len(idx) == 0:
        return x[: int(SR * 0.05)]
    pad = int(SR * pad_ms / 1000.0)
    start = max(0, idx[0] * frame - pad)
    end = min(len(x), (idx[-1] + 1) * frame + pad)
    return x[start:end]


def fade(x: np.ndarray, ms: float = 8.0) -> np.ndarray:
    """Apply a short linear fade-in and fade-out (8 ms by default) so clips can be joined without clicks."""
    n = min(int(SR * ms / 1000.0), len(x) // 2)
    if n <= 0:
        return x
    y = x.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def normalize_rms(x: np.ndarray, target_db: float = -18.0, peak_db: float = -1.5) -> np.ndarray:
    """Scale so the *speech* (frames above a floor) sits at target RMS, never exceeding the peak ceiling."""
    if len(x) == 0:
        return x
    frame = int(SR * 0.02)
    n = len(x) // frame
    if n == 0:
        return x
    f = x[: n * frame].reshape(n, frame)
    rms = np.sqrt((f ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    active = rms[db > -50.0]
    if len(active) == 0:
        return x
    speech_rms = float(np.sqrt((active ** 2).mean()))
    gain = (10 ** (target_db / 20.0)) / max(speech_rms, 1e-6)
    peak = float(np.max(np.abs(x))) * gain
    ceiling = 10 ** (peak_db / 20.0)
    if peak > ceiling:
        gain *= ceiling / peak
    return (x * gain).astype(np.float32)


def soft_limit(x: np.ndarray, ceiling: float = 0.95) -> np.ndarray:
    return (np.tanh(x / ceiling) * ceiling).astype(np.float32) if len(x) else x


def process_clip(x: np.ndarray) -> np.ndarray:
    """Standard treatment for every raw TTS clip: trim -> level -> tiny fades."""
    from . import tuning
    return fade(normalize_rms(trim_silence(x), target_db=tuning.current().speech_level_db), 6.0)


def concat(parts: list[np.ndarray]) -> np.ndarray:
    parts = [p for p in parts if len(p)]
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, dtype=np.float32)


def overlay(base: np.ndarray, clip: np.ndarray, start: int, gain: float = 1.0) -> np.ndarray:
    """Mix `clip` into `base` starting at sample `start` (extends base if needed)."""
    end = start + len(clip)
    if end > len(base):
        base = np.concatenate([base, np.zeros(end - len(base), dtype=np.float32)])
    base[start:end] += clip * gain
    return base


# --------------------------------------------------------------------------- jingle


def make_stinger(kind: str = "in", seconds: float = 2.4) -> np.ndarray:
    """A soft synthesised chime so episodes open/close like a produced show. No external assets needed."""
    n = int(SR * seconds)
    t = np.arange(n) / SR
    # gentle major-ish arpeggio (C5 E5 G5 C6), reversed for the outro
    notes = [523.25, 659.25, 783.99, 1046.50]
    if kind == "out":
        notes = notes[::-1]
    out = np.zeros(n, dtype=np.float32)
    step = 0.17
    for i, f in enumerate(notes):
        s = int(i * step * SR)
        tt = t[: n - s]
        env = np.exp(-tt * 2.6) * np.minimum(1.0, tt * 80.0)
        tone = (np.sin(2 * math.pi * f * tt) + 0.35 * np.sin(2 * math.pi * 2 * f * tt) + 0.12 * np.sin(2 * math.pi * 3 * f * tt))
        out[s:] += (tone * env * 0.22).astype(np.float32)
    # soft pad underneath
    pad_env = np.minimum(1.0, t * 3.0) * np.exp(-np.maximum(0, t - 0.6) * 1.8)
    pad = (np.sin(2 * math.pi * 261.63 * t) + np.sin(2 * math.pi * 392.0 * t)) * pad_env * 0.06
    out += pad.astype(np.float32)
    return fade(soft_limit(out * 0.9), 15.0)


# --------------------------------------------------------------------------- synthetic voices (mock mode / tests)


def synth_babble(text: str, voice: str, style: str | None = None) -> np.ndarray:
    """Deterministic 'speech-like' audio for offline demo/tests: one syllable burst per ~3 letters.
    Pitch depends on the voice name (and a bit on style) so different hosts are audibly different."""
    seed = sum(ord(c) * (i + 1) for i, c in enumerate(voice)) % 997
    base = 110 + (seed % 140)
    if style in {"happy", "surprise"}:
        base *= 1.12
    out = []
    rng = np.random.RandomState(seed)
    for word in text.split():
        syl = max(1, len(word) // 3)
        for _ in range(syl):
            dur = 0.11 + 0.03 * rng.rand()
            t = np.arange(int(SR * dur)) / SR
            f0 = base * (1 + 0.15 * math.sin(len(out) * 0.7)) * (1 + 0.1 * rng.rand())
            sig = np.sin(2 * math.pi * f0 * t) + 0.5 * np.sin(2 * math.pi * 2 * f0 * t) + 0.25 * np.sin(2 * math.pi * 3 * f0 * t)
            env = np.sin(math.pi * t / dur) ** 1.5
            out.append((sig * env * 0.25).astype(np.float32))
        out.append(np.zeros(int(SR * 0.045), dtype=np.float32))
        if word[-1:] in ",;:":
            out.append(np.zeros(int(SR * 0.12), dtype=np.float32))
        if word[-1:] in ".!?।":
            out.append(np.zeros(int(SR * 0.22), dtype=np.float32))
    body = np.concatenate(out) if out else np.zeros(int(SR * 0.2), dtype=np.float32)
    # engines pad clips with a little silence; mimic it so trimming is exercised
    return np.concatenate([np.zeros(int(SR * 0.12), dtype=np.float32), body, np.zeros(int(SR * 0.18), dtype=np.float32)])
