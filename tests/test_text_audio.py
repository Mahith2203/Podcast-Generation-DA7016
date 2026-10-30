from __future__ import annotations

import numpy as np
import pytest

from podcast_studio import audio
from podcast_studio.text import (
    chunk_for_tts,
    clean_spoken,
    dominant_script,
    extract_numbers,
    guess_language,
    split_sentences,
    unsupported_numbers,
)

# ------------------------------------------------------------------ text


def test_split_sentences_english_decimals_and_quotes():
    s = split_sentences('Plants make food. It is 3.5 percent! Is that right? He said "yes." Done.')
    assert s == ["Plants make food.", "It is 3.5 percent!", "Is that right?", 'He said "yes."', "Done."]


def test_split_sentences_hindi_danda_and_urdu():
    assert split_sentences("पौधे भोजन बनाते हैं। यह कैसे होता है? चलिए देखते हैं।") == [
        "पौधे भोजन बनाते हैं।", "यह कैसे होता है?", "चलिए देखते हैं।"]
    assert len(split_sentences("یہ پہلا جملہ ہے۔ یہ دوسرا ہے۔")) == 2


def test_clean_spoken_removes_markup_and_labels():
    t = clean_spoken("**Meera:** Wow [laughs] that's e.g. big — really (very big) 😀 https://x.com", labels=("Meera",))
    assert "Meera" not in t and "[" not in t and "http" not in t and "😀" not in t and "**" not in t
    assert "for example" in t and t.endswith(".")


def test_clean_spoken_adds_danda_for_devanagari():
    assert clean_spoken("पौधे भोजन बनाते हैं", "hi").endswith("।")
    assert clean_spoken("Plants make food", "en").endswith(".")


def test_chunking_respects_limits_and_loses_nothing():
    long = ("Photosynthesis is the process by which green plants make their own food using sunlight, water, and carbon dioxide, "
            "and it happens mainly in the leaves, inside tiny structures called chloroplasts, which contain a green pigment "
            "named chlorophyll that captures light energy. Then oxygen is released. Plants also take in water through roots.")
    chunks = chunk_for_tts(long, max_words=20)
    assert all(len(c.split()) <= 20 for c in chunks)
    assert " ".join(chunks).split() == long.split()


def test_chunking_packs_at_most_two_sentences():
    chunks = chunk_for_tts("One. Two. Three. Four. Five.", max_words=40)
    assert chunks == ["One. Two.", "Three. Four.", "Five."]


def test_chunking_unbreakable_long_sentence_is_split_by_words():
    s = " ".join(["word"] * 100) + "."
    chunks = chunk_for_tts(s, max_words=30)
    assert len(chunks) >= 4 and all(len(c.split()) <= 30 for c in chunks)


def test_number_checks_handle_indic_digits_and_small_numbers():
    assert extract_numbers("१२३ and 4,500.50") >= {"123", "4500.50"}
    assert unsupported_numbers("In 1987 about 71% and 3 types", "Earth is 71% water") == ["1987"]
    assert unsupported_numbers("about ७१ percent", "Earth is 71 percent") == []


def test_script_detection():
    assert dominant_script("நீர் சுழற்சி") == "Tamil"
    assert guess_language("पानी का चक्र")[1] == "Devanagari"
    assert guess_language("Water cycle") == ("en", "Latin")


# ------------------------------------------------------------------ audio


def test_wav_roundtrip_and_validation():
    x = audio.synth_babble("hello there", "Amit")
    y = audio.wav_bytes_to_array(audio.array_to_wav_bytes(x))
    assert abs(len(y) - len(x)) <= 1 and np.max(np.abs(y - x)) < 2e-4
    with pytest.raises(audio.AudioError):
        audio.wav_bytes_to_array(b"not a wav")


def test_resample_and_stereo_are_normalised():
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(12000)
        w.writeframes((np.sin(np.arange(12000) / 10) * 8000).astype("<i2").repeat(2).tobytes())
    y = audio.wav_bytes_to_array(buf.getvalue())
    assert abs(len(y) - audio.SR) < 5  # 1 second at 12 kHz -> ~1 second at 24 kHz


def test_trim_silence_and_normalise():
    x = audio.synth_babble("one two three four", "Kavya")
    t = audio.trim_silence(x)
    assert len(t) < len(x)
    n = audio.normalize_rms(t * 0.05)
    assert np.max(np.abs(n)) <= 10 ** (-1.5 / 20) + 1e-3
    frames = n[: len(n) // 480 * 480].reshape(-1, 480)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    active = rms[rms > 10 ** (-50 / 20)]
    assert 10 ** (-20 / 20) < np.sqrt((active ** 2).mean()) < 10 ** (-14 / 20)


def test_silence_trim_never_returns_empty_for_silence():
    assert len(audio.trim_silence(np.zeros(audio.SR, dtype=np.float32))) > 0


def test_overlay_extends_buffer():
    base = np.zeros(10, dtype=np.float32)
    out = audio.overlay(base, np.ones(10, dtype=np.float32), 5)
    assert len(out) == 15 and out[4] == 0 and out[14] == 1


def test_stinger_is_audible_and_bounded():
    s = audio.make_stinger("in")
    assert 1.5 < audio.duration(s) < 4 and 0.05 < np.max(np.abs(s)) <= 1.0
