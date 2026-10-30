"""Languages, voices and speaking styles, transcribed from the Bodhan indic-speak API reference.

Notes taken from the docs:
  * 45 voices, grouped by the language each was recorded in, but ANY voice can read ANY language.
  * English has no dedicated voice; any voice can read it (with that voice's accent).
  * `instructions` is a JSON string: {"lang": "<code>", "style": "<optional style>"}.
"""
from __future__ import annotations

# code -> metadata. `tier` = how reliably general LLMs write natural conversational text in the language.
LANGS: dict[str, dict] = {
    "en": {"name": "English", "native": "English", "script": "Latin", "tier": "strong"},
    "hi": {"name": "Hindi", "native": "हिन्दी", "script": "Devanagari", "tier": "strong"},
    "bn": {"name": "Bengali", "native": "বাংলা", "script": "Bengali", "tier": "strong"},
    "gu": {"name": "Gujarati", "native": "ગુજરાતી", "script": "Gujarati", "tier": "strong"},
    "kn": {"name": "Kannada", "native": "ಕನ್ನಡ", "script": "Kannada", "tier": "strong"},
    "ml": {"name": "Malayalam", "native": "മലയാളം", "script": "Malayalam", "tier": "strong"},
    "mr": {"name": "Marathi", "native": "मराठी", "script": "Devanagari", "tier": "strong"},
    "pa": {"name": "Punjabi", "native": "ਪੰਜਾਬੀ", "script": "Gurmukhi", "tier": "strong"},
    "ta": {"name": "Tamil", "native": "தமிழ்", "script": "Tamil", "tier": "strong"},
    "te": {"name": "Telugu", "native": "తెలుగు", "script": "Telugu", "tier": "strong"},
    "ur": {"name": "Urdu", "native": "اردو", "script": "Perso-Arabic (Nastaliq)", "tier": "strong"},
    "ne": {"name": "Nepali", "native": "नेपाली", "script": "Devanagari", "tier": "moderate"},
    "or": {"name": "Odia", "native": "ଓଡ଼ିଆ", "script": "Odia", "tier": "moderate"},
    "as": {"name": "Assamese", "native": "অসমীয়া", "script": "Bengali-Assamese", "tier": "moderate"},
    "sa": {"name": "Sanskrit", "native": "संस्कृतम्", "script": "Devanagari", "tier": "moderate"},
    "mai": {"name": "Maithili", "native": "मैथिली", "script": "Devanagari", "tier": "moderate"},
    "kok": {"name": "Konkani", "native": "कोंकणी", "script": "Devanagari", "tier": "moderate"},
    "doi": {"name": "Dogri", "native": "डोगरी", "script": "Devanagari", "tier": "limited"},
    "brx": {"name": "Bodo", "native": "बड़ो", "script": "Devanagari", "tier": "limited"},
    "ks": {"name": "Kashmiri", "native": "کٲشُر", "script": "Perso-Arabic", "tier": "limited"},
    "mni": {"name": "Manipuri (Meitei)", "native": "ꯃꯩꯇꯩꯂꯣꯟ", "script": "Meetei Mayek", "tier": "limited"},
    "sat": {"name": "Santali", "native": "ᱥᱟᱱᱛᱟᱲᱤ", "script": "Ol Chiki", "tier": "limited"},
    "sd": {"name": "Sindhi", "native": "سنڌي", "script": "Perso-Arabic", "tier": "limited"},
}

# (voice name, gender, language it was recorded in)
VOICES: list[tuple[str, str, str]] = [
    ("Prastuti", "female", "as"), ("Ankur", "male", "as"),
    ("Ishita", "female", "bn"), ("Sourav", "male", "bn"),
    ("Gwrbw", "female", "brx"), ("Sansuma", "male", "brx"),
    ("Preeti", "female", "doi"), ("Sham", "male", "doi"),
    ("Dhara", "female", "gu"), ("Parth", "male", "gu"),
    ("Kavya", "female", "hi"), ("Suhani", "female", "hi"), ("Amit", "male", "hi"),
    ("Deepika", "female", "kn"), ("Adarsh", "male", "kn"),
    ("Anjali", "female", "kok"), ("Sandeep", "male", "kok"),
    ("Zoon", "female", "ks"), ("Ishfaq", "male", "ks"),
    ("Vaidehi", "female", "mai"), ("Madhukar", "male", "mai"),
    ("Lakshmi", "female", "ml"), ("Kiran", "male", "ml"),
    ("Thoibi", "female", "mni"), ("Chaoba", "male", "mni"),
    ("Anagha", "female", "mr"), ("Chinmay", "male", "mr"),
    ("Srijana", "female", "ne"), ("Sagar", "male", "ne"),
    ("Itishree", "female", "or"), ("Akash", "male", "or"),
    ("Kaur", "female", "pa"), ("Manpreet", "male", "pa"),
    ("Bharati", "female", "sa"), ("Aryaman", "male", "sa"),
    ("Phulmani", "female", "sat"), ("Sibu", "male", "sat"),
    ("Moomal", "female", "sd"), ("Rano", "male", "sd"),
    ("Anitha", "female", "ta"), ("Arun", "male", "ta"),
    ("Sravani", "female", "te"), ("Vamsi", "male", "te"),
    ("Saba", "female", "ur"), ("Zaid", "male", "ur"),
]

VOICE_GENDER = {name: g for name, g, _ in VOICES}
VOICE_NAMES = [name for name, _, _ in VOICES]

STYLES = [
    "AIR style news", "Customer Care", "TV style news", "advertisements", "anger",
    "children's stories", "disgust", "educational lecture", "fear", "happy", "news",
    "sad", "single person narration audiobook", "surprise",
]

# Styles the script writer may use by default. Neutral (no style) is the baseline; these add colour.
DEFAULT_ALLOWED_STYLES = ["happy", "surprise", "educational lecture"]

# A short greeting for the voice-preview button. Only languages whose text we are confident about.
SAMPLE_TEXT = {
    "en": "Hello! Welcome to the show. Today we are going to explore something really interesting.",
    "hi": "नमस्ते! आज हम एक दिलचस्प विषय के बारे में बात करेंगे।",
    "ta": "வணக்கம்! இன்று நாம் ஒரு சுவாரஸ்யமான தலைப்பைப் பற்றிப் பேசப் போகிறோம்.",
    "te": "నమస్కారం! ఈ రోజు మనం ఒక ఆసక్తికరమైన అంశం గురించి మాట్లాడుకుందాం.",
    "bn": "নমস্কার! আজ আমরা একটি মজার বিষয় নিয়ে কথা বলব।",
    "mr": "नमस्कार! आज आपण एका रंजक विषयावर गप्पा मारणार आहोत.",
    "gu": "નમસ્તે! આજે આપણે એક રસપ્રદ વિષય વિશે વાત કરીશું.",
    "kn": "ನಮಸ್ಕಾರ! ಇಂದು ನಾವು ಒಂದು ಆಸಕ್ತಿದಾಯಕ ವಿಷಯದ ಬಗ್ಗೆ ಮಾತನಾಡೋಣ.",
    "ml": "നമസ്കാരം! ഇന്ന് നമുക്ക് രസകരമായ ഒരു വിഷയത്തെക്കുറിച്ച് സംസാരിക്കാം.",
    "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ਅੱਜ ਅਸੀਂ ਇੱਕ ਦਿਲਚਸਪ ਵਿਸ਼ੇ ਬਾਰੇ ਗੱਲ ਕਰਾਂਗੇ।",
    "ur": "السلام علیکم! آج ہم ایک دلچسپ موضوع پر بات کریں گے۔",
}

# Rough speaking rate used to turn "minutes" into a word budget for the script writer.
WORDS_PER_MINUTE = {"en": 150, "hi": 140}
DEFAULT_WPM = 120


def wpm(lang: str) -> int:
    return WORDS_PER_MINUTE.get(lang, DEFAULT_WPM)


def lang_name(code: str) -> str:
    return LANGS.get(code, {}).get("name", code)


def default_hosts(lang: str) -> tuple[dict, dict]:
    """Pick a female + male voice for a language. English (no native voice) uses the Hindi pair."""
    home = lang if lang in LANGS and lang != "en" else "hi"
    fem = [v for v, g, vl in VOICES if vl == home and g == "female"]
    mal = [v for v, g, vl in VOICES if vl == home and g == "male"]
    fv = fem[0] if fem else "Kavya"
    mv = mal[0] if mal else "Amit"
    return (
        {"name": "Meera", "voice": fv, "gender": "female"},
        {"name": "Arjun", "voice": mv, "gender": "male"},
    )


def catalog() -> dict:
    """What the UI needs to render its pickers."""
    return {
        "languages": [{"code": c, **m} for c, m in LANGS.items()],
        "voices": [{"name": n, "gender": g, "lang": vl} for n, g, vl in VOICES],
        "styles": STYLES,
        "default_allowed_styles": DEFAULT_ALLOWED_STYLES,
        "sample_text": SAMPLE_TEXT,
    }
