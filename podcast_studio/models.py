"""Domain models. Everything a project needs is stored as JSON on disk, so a run can be resumed."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .util import now_iso
from .voices import DEFAULT_ALLOWED_STYLES, LANGS, STYLES, VOICE_GENDER, VOICE_NAMES, default_hosts


class HostCfg(BaseModel):
    """One host: display name, voice and the grammatical gender implied by the voice."""
    name: str = Field(min_length=1, max_length=40)
    voice: str
    gender: Literal["female", "male"] = "female"

    @field_validator("voice")
    @classmethod
    def _voice_ok(cls, v: str) -> str:
        if v not in VOICE_NAMES:
            raise ValueError(f"Unknown voice '{v}'. Choose one of: {', '.join(VOICE_NAMES)}")
        return v

    @model_validator(mode="after")
    def _gender_from_voice(self) -> "HostCfg":
        # The voice decides the grammatical gender the script must use (matters in Hindi, Marathi, ...).
        self.gender = VOICE_GENDER.get(self.voice, self.gender)  # type: ignore[assignment]
        return self


def _default_host_a() -> HostCfg:
    return HostCfg(**default_hosts("en")[0])


def _default_host_b() -> HostCfg:
    return HostCfg(**default_hosts("en")[1])


class ProjectSettings(BaseModel):
    """Everything chosen when an episode is created: language, writing mode, length, hosts, fact-check on or off,
    delivery styles, and optional instructions. Out-of-range values are clamped.
    """
    language: str = "en"
    writing_mode: Literal["direct", "translate"] = "direct"
    polish_translation: bool = False
    audience: str = "school students (Classes 6-12)"
    minutes: Optional[float] = None  # None = automatic from chapter length
    show_name: str = "Chapter Chat"
    host_a: HostCfg = Field(default_factory=_default_host_a)
    host_b: HostCfg = Field(default_factory=_default_host_b)
    fact_check: bool = True
    max_revisions: int = 2
    allowed_styles: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_STYLES))
    jingle: bool = True
    auto_voice: bool = True
    make_video: bool = False  # also make the narrated video as soon as the audio is ready
    extra_instructions: str = ""
    writer_model: Optional[str] = None
    checker_model: Optional[str] = None
    tts_max_words: int = 40

    @field_validator("language")
    @classmethod
    def _lang_ok(cls, v: str) -> str:
        if v not in LANGS:
            raise ValueError(f"Unsupported language '{v}'.")
        return v

    @field_validator("minutes")
    @classmethod
    def _minutes_ok(cls, v):
        if v is None:
            return v
        return max(2.0, min(60.0, float(v)))

    @field_validator("max_revisions")
    @classmethod
    def _rev_ok(cls, v: int) -> int:
        return max(0, min(4, int(v)))

    @field_validator("allowed_styles")
    @classmethod
    def _styles_ok(cls, v: list[str]) -> list[str]:
        return [s for s in v if s in STYLES]

    @field_validator("tts_max_words")
    @classmethod
    def _words_ok(cls, v: int) -> int:
        return max(12, min(70, int(v)))

    @field_validator("extra_instructions")
    @classmethod
    def _extra_len(cls, v: str) -> str:
        return (v or "")[:2000]

    @field_validator("show_name")
    @classmethod
    def _show_ok(cls, v: str) -> str:
        return (v or "Chapter Chat").strip()[:60] or "Chapter Chat"


# --------------------------------------------------------------------------- script


class Turn(BaseModel):
    """One spoken line: who speaks, the text, an optional English master (translate mode), a delivery style and whether
    it is a short reaction.
    """
    speaker: Literal["A", "B"]
    text: str  # what is spoken, in the podcast language
    text_en: Optional[str] = None  # English master (translate mode only)
    style: Optional[str] = None
    reaction: bool = False  # short interjection; may overlap the previous speaker slightly
    take: int = 0  # bump to force a fresh TTS call for identical text
    edited: bool = False


class Issue(BaseModel):
    """One problem found by a check, with its severity, the line it concerns and a suggested fix."""
    turn: Optional[int] = None
    severity: Literal["major", "minor"] = "minor"
    type: str = "other"
    detail: str = ""
    evidence_unit: Optional[int] = None
    suggested_fix: str = ""
    source: Literal["llm", "rule"] = "llm"


class CheckReport(BaseModel):
    """Result of checking one segment: verdict, issues, missing key points and whether it still needs a human look after
    the repair attempts.
    """
    verdict: Literal["pass", "revise", "unchecked"] = "unchecked"
    issues: list[Issue] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    notes: str = ""
    attempts: int = 1
    needs_review: bool = False

    @property
    def has_blockers(self) -> bool:
        return any(i.severity == "major" for i in self.issues) or bool(self.missing_points)


class SegmentVersion(BaseModel):
    """One saved version of a segment (generated, rewritten, edited or re-voiced) with its lines, check result and audio."""
    version: int
    created_at: str = Field(default_factory=now_iso)
    origin: Literal["generated", "rewrite", "edited", "revoiced"] = "generated"
    instruction: Optional[str] = None
    turns: list[Turn] = Field(default_factory=list)
    check: Optional[CheckReport] = None
    audio_file: Optional[str] = None  # relative to the project dir
    duration: Optional[float] = None
    turn_times: list[list[float]] = Field(default_factory=list)  # [[start, end], ...] within the segment audio

    @property
    def words(self) -> int:
        return sum(len(t.text.split()) for t in self.turns)


class Segment(BaseModel):
    """One part of the episode (intro, body, recap or outro): its plan, the chapter paragraphs it covers, and every saved
    version.
    """
    index: int
    kind: Literal["intro", "body", "recap", "outro"] = "body"
    title: str
    goal: str = ""
    key_points: list[str] = Field(default_factory=list)
    unit_ids: list[int] = Field(default_factory=list)
    target_words: int = 150
    status: Literal["pending", "writing", "written", "voicing", "ready", "error"] = "pending"
    error: Optional[str] = None
    versions: list[SegmentVersion] = Field(default_factory=list)
    current: int = 0  # version number in use (0 = none yet)

    def version(self, n: Optional[int] = None) -> Optional[SegmentVersion]:
        n = self.current if n is None else n
        for v in self.versions:
            if v.version == n:
                return v
        return None

    def next_version_number(self) -> int:
        return max((v.version for v in self.versions), default=0) + 1


# --------------------------------------------------------------------------- project


class SourceInfo(BaseModel):
    """Facts about the uploaded chapter: size, pages, whether OCR was needed, detected script and any warnings."""
    filename: str = ""
    pages: int = 0
    chars: int = 0
    words: int = 0
    unit_count: int = 0
    ocr_used: bool = False
    script_guess: str = ""
    title_guess: str = ""
    warnings: list[str] = Field(default_factory=list)


class PlanInfo(BaseModel):
    """The episode plan: title, opening hook and target length."""
    episode_title: str = ""
    hook: str = ""
    target_minutes: float = 0.0
    total_words: int = 0


class ChapterMark(BaseModel):
    """A chapter marker in the final audio: segment title and start time in seconds."""
    index: int
    title: str
    start: float


class FinalAudio(BaseModel):
    """The finished episode: WAV, MP3, transcripts, subtitles, duration, chapter marks and whether it is out of date."""
    wav: Optional[str] = None
    mp3: Optional[str] = None
    transcript_md: Optional[str] = None
    srt: Optional[str] = None
    vtt: Optional[str] = None
    duration: float = 0.0
    chapters: list[ChapterMark] = Field(default_factory=list)
    built_at: str = ""
    stale: bool = False  # true when a segment changed but the episode was not rebuilt yet


class UsageTotals(BaseModel):
    """Counts of what an episode used: model calls, tokens, cost, voice requests and characters, cache hits, translation
    calls.
    """
    llm_calls: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_cost_usd: float = 0.0
    tts_calls: int = 0
    tts_chars: int = 0
    tts_cache_hits: int = 0
    mt_calls: int = 0
    mt_chars: int = 0



# --------------------------------------------------------------------------- video

SceneLayout = Literal["title", "bullets", "steps", "cycle", "definition", "compare", "figure", "keyfact", "recap"]


class Figure(BaseModel):
    """A picture found in the chapter PDF (shown in the video with its original caption, never re-captioned)."""
    id: str
    page: int
    file: str  # relative to the project dir
    width: int = 0
    height: int = 0
    caption: str = ""
    near_text: str = ""
    unit_id: Optional[int] = None  # best-matching chapter paragraph


class Scene(BaseModel):
    """One slide of the video: a layout, its text, which script lines it is on screen for, and when each item appears."""
    layout: SceneLayout = "bullets"
    title: str = ""
    items: list[str] = Field(default_factory=list)      # bullets / steps / cycle / recap points / compare row labels
    term: str = ""                                       # definition term, or keyfact value
    text: str = ""                                       # definition body, or keyfact label, or subtitle
    columns: list[str] = Field(default_factory=list)     # compare: two column headings
    rows: list[list[str]] = Field(default_factory=list)  # compare: rows of two cells
    figure: Optional[str] = None                         # Figure.id
    from_turn: int = 0                                   # first/last script line (inclusive) this scene is on screen for
    to_turn: int = 0
    reveal: list[int] = Field(default_factory=list)      # script line at which each item/row appears

    def visible_text(self) -> str:
        return " ".join([self.title, self.term, self.text, *self.items, *self.columns, *[c for r in self.rows for c in r]]).strip()


class SegmentStoryboard(BaseModel):
    """The slides planned for one segment, and any problems the checks found in them."""
    index: int
    version: int                                         # script version this storyboard was made for
    scenes: list[Scene] = Field(default_factory=list)
    source: Literal["llm", "fallback", "mock"] = "llm"
    instruction: Optional[str] = None
    issues: list[str] = Field(default_factory=list)     # problems the checks found (empty = clean)
    created_at: str = Field(default_factory=now_iso)


class VideoCfg(BaseModel):
    """Video options: resolution, frame rate, theme, captions, speaker names, chapter figures and cross-fade."""
    resolution: Literal["480p", "720p", "1080p"] = "720p"
    fps: int = 25
    captions: bool = True
    speakers: bool = True       # name chip next to the caption, and a speaker strip
    theme: Literal["classic", "dark", "warm"] = "classic"
    use_figures: bool = True    # use pictures from the chapter PDF where they fit
    crossfade: float = 0.35

    @field_validator("fps")
    @classmethod
    def _fps_ok(cls, v: int) -> int:
        return max(10, min(30, int(v)))

    @field_validator("crossfade")
    @classmethod
    def _cf_ok(cls, v: float) -> float:
        return max(0.0, min(1.2, float(v)))


class FinalVideo(BaseModel):
    """The finished video: MP4, poster image, size, duration and whether it is out of date."""
    mp4: Optional[str] = None
    poster: Optional[str] = None
    duration: float = 0.0
    width: int = 0
    height: int = 0
    frames: int = 0
    built_at: str = ""
    audio_built_at: str = ""    # which build of the episode audio this video belongs to
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class VideoState(BaseModel):
    """Everything about an episode's video: options, status, storyboards, figures from the PDF and the final file."""
    cfg: VideoCfg = Field(default_factory=VideoCfg)
    status: Literal["none", "directing", "rendering", "ready", "error"] = "none"
    message: str = ""
    storyboards: list[SegmentStoryboard] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    figures_done: bool = False
    final: Optional[FinalVideo] = None


ProjectStatus = Literal[
    "created", "ingesting", "planning", "writing", "script_ready", "voicing",
    "assembling", "ready", "error", "interrupted",
]


class Project(BaseModel):
    """One episode on disk: settings, source info, plan, segments, final audio, video, usage and a log."""
    id: str
    title: str
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    status: ProjectStatus = "created"
    message: str = ""
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    source: SourceInfo = Field(default_factory=SourceInfo)
    plan: PlanInfo = Field(default_factory=PlanInfo)
    segments: list[Segment] = Field(default_factory=list)
    final: Optional[FinalAudio] = None
    video: VideoState = Field(default_factory=VideoState)
    usage: UsageTotals = Field(default_factory=UsageTotals)
    log: list[str] = Field(default_factory=list)

    def host(self, speaker: str) -> HostCfg:
        return self.settings.host_a if speaker == "A" else self.settings.host_b


class SourceUnit(BaseModel):
    """One numbered piece of the chapter (heading, paragraph, list or table). Every claim in the episode must trace back
    to these ids.
    """
    id: int
    kind: Literal["heading", "para", "list", "table"] = "para"
    text: str
    section: str = ""
