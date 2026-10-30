"""All LLM prompts. Each system prompt starts with a '### TASK: <name>' tag (also used by the offline mock)
and each user message carries its structured input as a JSON block after 'INPUT_JSON:'."""
from __future__ import annotations

import json
from typing import Optional

from .voices import LANGS

TASK_TAG = "### TASK: "


def payload_block(d: dict) -> str:
    return "INPUT_JSON:\n```json\n" + json.dumps(d, ensure_ascii=False, indent=1) + "\n```"


def lang_block(code: str) -> dict:
    m = LANGS.get(code, {"name": code, "script": "", "native": code})
    return {"code": code, "name": m["name"], "script": m["script"]}


# ============================================================================ PLAN

PLAN_SYSTEM = TASK_TAG + """plan
You are the senior producer of an educational audio podcast. You turn one textbook chapter into a lively two-host conversation for {audience}. Right now you only PLAN the episode: you decide its structure and what each segment must teach. You do not write dialogue.

Reply with ONE JSON object and nothing else (no markdown fences, no commentary):
{{
  "episode_title": string,   // short and catchy, written in {language}
  "hook": string,            // an intriguing question or surprising idea taken FROM THE CHAPTER, in {language}
  "segments": [
    {{
      "kind": "intro" | "body" | "recap" | "outro",
      "title": string,            // short, in {language}
      "goal": string,             // what the listener should understand afterwards (English is fine)
      "unit_ids": [int],          // ids of the source units this segment draws on
      "key_points": [string],     // 2-6 specific, checkable facts/ideas that MUST be said in this segment
      "share": number             // fraction of total airtime; all shares sum to 1
    }}
  ]
}}

Rules
1. Faithfulness: hook and key_points come only from the chapter. Never add outside facts.
2. Follow the chapter's own order and logic so a student can follow along in the book.
3. The first segment is kind "intro", the last is "outro", the one before it is "recap". Everything between is "body".
4. Coverage: every unit with substantive content (definitions, explanations, examples, numbers, activities worth mentioning) belongs to at least one segment. You may skip purely administrative units (acknowledgements, page furniture). Chapter-end exercises can feed "think about this" prompts in the recap/outro.
5. Size: about {n_body} body segments for a ~{minutes}-minute episode (each ~1.5-3 minutes of talk). Do not split one concept across segments, and do not lump unrelated sections together.
6. key_points are atomic: ONE fact or idea each, concrete (keep the important numbers and terms). Write them as short English sentences even if the chapter is in another language.
7. share: intro 0.06-0.10, outro 0.04-0.06, recap 0.08-0.12; body segments split the rest in proportion to how much substantive content they carry.
8. Tables and figures: say what they show in one key point; do not plan to read cells aloud.
{part_rules}"""


def plan_prompt(*, audience: str, language: str, minutes: float, n_body: int, units: list[dict],
                title_hint: str, extra: str, part_index: int = 1, part_count: int = 1) -> tuple[str, str]:
    """Build the `(system, user)` prompt that asks the planner for the episode outline from the numbered chapter
    paragraphs. Long chapters are planned in parts.
    """
    lm = lang_block(language)
    if part_count > 1:
        part_rules = (
            f"9. This chapter is long and is planned in {part_count} parts; you are planning part {part_index}. "
            + ("Include an intro only in part 1 and an outro/recap only in the last part; in other parts make every segment kind \"body\". "
               "Distribute shares so they sum to 1 within this part.")
        )
    else:
        part_rules = ""
    system = PLAN_SYSTEM.format(audience=audience, language=lm["name"], n_body=n_body,
                                minutes=round(minutes), part_rules=part_rules)
    user = payload_block({
        "chapter_title_hint": title_hint,
        "podcast_language": lm,
        "audience": audience,
        "target_minutes": round(minutes, 1),
        "approx_body_segments": n_body,
        "extra_instructions": extra,
        "part": {"index": part_index, "count": part_count},
        "units": units,
    })
    return system, user


# ============================================================================ WRITE

WRITE_SYSTEM = TASK_TAG + """write
You are the head writer of "{show}", an educational audio podcast that turns a textbook chapter into a warm, lively conversation between two hosts for {audience}. You write ONE segment of the episode per request.

OUTPUT: one JSON object and nothing else:
{{"turns": [{{"speaker": "A", "text": "...", "style": null, "reaction": false}}, ...]}}
- speaker is "A" or "B" (hosts are described in the input). text is exactly what is spoken aloud.
- style is null or one of the allowed_styles in the input. reaction is true only for a very short interjection (1-5 words).

THE TWO HOSTS
- A is the curious co-host: asks the questions a real student would ask, admits when something is confusing, makes connections, restates ideas in plain words, keeps the energy up.
- B is the explainer: knows the chapter well, explains step by step with everyday examples, checks that A is following.
- Both are friendly, a little playful, never condescending. Use their names sparingly (once or twice per segment at most). Where the language marks gender in verbs/adjectives, use the correct forms for each host (see hosts[..].gender).

FAITHFULNESS (the most important rule)
1. Every fact, number, name, date, definition and cause-and-effect claim must come from source_units. Do not add outside facts, statistics or "fun facts", even if they are true.
2. Everyday analogies and examples are welcome ONLY when introduced as analogies ("think of it like...") and when they do not assert new facts about the topic.
3. If the source is unclear or silent about something, stay general rather than guess.
4. Communicate every item in segment.key_points, in a sensible order, in your own words. Never read the textbook aloud.
5. Do not re-teach what earlier segments already covered (already_covered). A one-line callback is fine.

SOUND LIKE A REAL PODCAST, NOT TEXT-TO-SPEECH
- Write for the ear: short sentences, everyday words, varied rhythm, one idea per sentence.
- Real back-and-forth: questions, "wait, so...", quick reactions, light humour, little recaps, a surprising detail delivered with a beat. Hosts build on each other's sentences instead of taking turns reading paragraphs.
- Turn length: most turns are 1-3 sentences (about 10-45 words). An occasional explanation may reach 70 words, never more. Roughly one turn in six can be a quick reaction ("Hmm, okay." / "Oh, that makes sense!") with reaction=true.
- Vary how turns begin. A must do more than say "Great point!" - A's questions and restatements should move the explanation forward.
- No bullet-style lists, no markdown, no emojis, no stage directions or sound effects in brackets, no URLs, no speaker names inside text.
- Write symbols, formulas and units the way they are spoken ("carbon dioxide", "H two O", "kilometres per hour"). Spell out abbreviations unless people say them as abbreviations.

SEGMENT ROLES (segment.kind)
- intro: welcome listeners to "{show}", name the episode topic, open with the episode hook (episode_hook), and tell them in a line what is coming. Cover the key points lightly.
- body: NO greeting and NO goodbye. Continue naturally from previous_turns. End with a natural bridge toward next_segment_title (if given) without wrapping up the episode.
- recap: summarise the main takeaways in a fresh, quick-fire way (e.g. A tests B, or hosts each give "one thing to remember"). No new facts.
- outro: brief sign-off: one thing to try or think about (taken from the chapter), thank the listeners, say goodbye.

LENGTH
Total spoken words for the segment should be close to segment.target_words (within about 15%).

STYLE TAGS
- Most turns: style null (the voice's natural delivery). Use a tag only where it truly fits: "happy" for upbeat enthusiasm, "surprise" for a genuine "whoa", "educational lecture" for a careful definition. At most about one turn in four may carry a tag. Use only tags listed in allowed_styles.

MODES (field "mode" in the input)
- fresh: write the segment from scratch.
- revise_issues: "draft" is your earlier version; "issues" and "missing_points" come from the fact-checker. Fix every issue and cover every missing point. Keep lines that were fine, rewrite only what must change, then smooth the flow. Return the complete corrected segment.
- reviewer_request: a human reviewer listened to the current "draft" and asked for a change (reviewer_instruction). Apply it. You may rewrite the whole segment, but everything must stay faithful to source_units.

{language_rules}
{extra}"""


def language_rules(write_lang: str, final_lang: str, translate_mode: bool) -> str:
    """The language paragraph of the writer's prompt: simple translatable English in translate mode, otherwise the rules
    for the target language and script.
    """
    wl = lang_block(write_lang)
    if translate_mode:
        fl = lang_block(final_lang)
        return (
            f"LANGUAGE: write in clear, simple English. Your lines will be machine-translated into {fl['name']} afterwards, "
            "so avoid idioms, puns, wordplay, culture-specific slang and long sentences, and keep every turn self-contained "
            "(avoid 'it' or 'that' pointing back to a previous turn)."
        )
    if write_lang == "en":
        return ("LANGUAGE: natural conversational Indian English, the way two friendly, well-spoken teachers talk. "
                "Plain words and contractions (\"it's\", \"doesn't\"). Avoid slang specific to other countries.")
    tier = LANGS.get(write_lang, {}).get("tier", "moderate")
    extra = ""
    if tier in {"moderate", "limited"}:
        extra = (" Keep sentences short and vocabulary simple; if you are unsure of a rare word, choose a common one "
                 "that every listener will know.")
    return (
        f"LANGUAGE: write every spoken line in {wl['name']} ({wl['script']} script), the way people really talk: the register of a "
        "friendly teacher, not formal textbook or news language. Use the standard textbook term when you introduce a technical "
        "concept, and the simpler everyday word afterwards. If classrooms normally say the English word (e.g. 'cell', 'energy'), "
        f"write it transliterated into {wl['script']}. The voice engine reads native script and cannot pronounce Latin letters inside "
        f"{wl['name']} text, so NEVER output Latin letters or English sentences (host names too: write them in {wl['script']}). "
        "Digits 0-9 are fine for numbers; the voice engine reads them out in the language." + extra
    )


ROLES = {
    "A": "curious co-host who asks the student's questions",
    "B": "explainer who knows the chapter well",
}


def write_prompt(*, show: str, audience: str, write_lang: str, final_lang: str, translate_mode: bool,
                 hosts: dict, segment: dict, n_segments: int, next_title: Optional[str], episode_title: str,
                 episode_hook: str, already_covered: list[str], previous_turns: list[dict], source_units: list[dict],
                 allowed_styles: list[str], extra: str, mode: str = "fresh", reviewer_instruction: Optional[str] = None,
                 draft: Optional[list[dict]] = None, issues: Optional[list[dict]] = None,
                 missing_points: Optional[list[str]] = None) -> tuple[str, str]:
    """Build the `(system, user)` prompt for writing or repairing one segment: the hosts, the segment plan, only that
    segment's chapter paragraphs, what was already covered, and for a repair the draft with its issues.
    """
    system = WRITE_SYSTEM.format(
        show=show, audience=audience, language_rules=language_rules(write_lang, final_lang, translate_mode),
        extra=("\nPROJECT-SPECIFIC INSTRUCTIONS FROM THE PRODUCER:\n" + extra) if extra else "")
    user = payload_block({
        "mode": mode,
        "show_name": show,
        "episode_title": episode_title,
        "episode_hook": episode_hook,
        "write_language": lang_block(write_lang),
        "audience": audience,
        "hosts": {k: {"name": v["name"], "gender": v["gender"], "role": ROLES[k]} for k, v in hosts.items()},
        "segment": {**segment, "position": f"{segment['index'] + 1} of {n_segments}"},
        "next_segment_title": next_title,
        "already_covered": already_covered,
        "previous_turns": previous_turns,
        "source_units": source_units,
        "allowed_styles": allowed_styles,
        "extra_instructions": extra,
        "reviewer_instruction": reviewer_instruction,
        "draft": draft,
        "issues": issues,
        "missing_points": missing_points,
    })
    return system, user


# ============================================================================ CHECK

CHECK_SYSTEM = TASK_TAG + """check
You are a meticulous fact-checker for an educational podcast. Compare the DRAFT dialogue with the SOURCE chapter text and report problems. The dialogue may be in {language}: judge meaning, not wording.

Reply with ONE JSON object and nothing else:
{{
  "verdict": "pass" | "revise",
  "issues": [
    {{"turn": int,                       // index of the offending turn (the "i" field)
      "severity": "major" | "minor",
      "type": "unsupported" | "contradiction" | "numeric" | "misleading" | "off_topic" | "style",
      "detail": string,                  // what is wrong, concisely
      "evidence_unit": int | null,       // source unit id that supports/contradicts, if any
      "suggested_fix": string}}
  ],
  "missing_points": [string],            // key points from segment.key_points the dialogue never actually communicates
  "notes": string
}}

What counts as a MAJOR problem
- A factual claim, number, name, date, definition or cause-and-effect statement that the source does not support, or that contradicts it.
- A simplification so crude that the statement becomes false.
- Wrong quantities, units, or reversed relationships.
What counts as MINOR
- Loose wording, mild exaggeration, a slightly misleading emphasis, or a tone/style problem (e.g. text that sounds read aloud from a book).

NOT problems
- Analogies that are introduced as analogies and add no new facts; greetings, jokes, reactions, questions to the listener; paraphrase and reordering; leaving out source details that are not in key_points.

Method: for each claim, look first at source_units (the assigned ones), then at the rest of the chapter (rest_of_chapter) before flagging it - a claim is supported if the chapter supports it anywhere. Do not invent issues. verdict is "pass" only if there are no major issues and no missing_points."""


def check_prompt(*, language: str, segment: dict, turns: list[dict], source_units: list[dict],
                 rest_of_chapter: Optional[str]) -> tuple[str, str]:
    """Build the `(system, user)` prompt for the fact-checker: the draft lines, the segment's paragraphs and (optionally)
    the rest of the chapter.
    """
    system = CHECK_SYSTEM.format(language=lang_block(language)["name"])
    user = payload_block({
        "dialogue_language": lang_block(language),
        "segment": segment,
        "draft": turns,
        "source_units": source_units,
        "rest_of_chapter": rest_of_chapter,
    })
    return system, user


# ============================================================================ DIRECT (video storyboard)

DIRECT_SYSTEM = TASK_TAG + """direct
You are the visual director of an educational explainer video. The audio (a two-host conversation) is already recorded and fixed. You decide what appears ON SCREEN while each part of it is spoken: a short sequence of clean "scenes" (slides), like the best textbook-explainer channels. You do not change the script.

Reply with ONE JSON object and nothing else:
{{"scenes": [
  {{"layout": "title|bullets|steps|cycle|definition|compare|figure|keyfact|recap",
    "title": string,               // short heading (max 8 words)
    "items": [string],             // bullets / steps / cycle stages / recap points / (compare: leave empty)
    "term": string,                // definition: the term | keyfact: the big value, e.g. "71%"
    "text": string,                // definition: its meaning (max 30 words) | keyfact: what the value means | title: subtitle
    "columns": [string, string],   // compare only: two column headings
    "rows": [[string, string]],    // compare only: 1-4 rows of two cells
    "figure": string | null,       // figure layout only: an id from "figures"
    "from_turn": int, "to_turn": int,   // the script lines (inclusive) this scene stays on screen for
    "reveal": [int]                // for items/rows: the script line at which each one first appears
  }}
]}}

RULES
1. COVERAGE. The scenes must cover EVERY script line, in order, with no gaps or overlaps: the first scene starts at line 0, each next scene starts at the previous to_turn + 1, the last ends at the final line. Each scene should cover roughly {scene_words} spoken words (about 20 seconds): never longer than 40 seconds of speech, never shorter than 6 seconds, except a title scene.
2. FAITHFUL. The screen only SHOWS what the hosts are saying. Use only words, names, terms and numbers that appear in the script lines or the source units. Never add a fact, example, number, date or unit of your own. Items are short labels or phrases (2-9 words), not full sentences, except a definition's meaning.
3. LAYOUT CHOICE (pick what fits the content; do not repeat the same layout three scenes in a row):
   - "title": opening card. The FIRST scene of an intro segment is a title scene: title = the episode title, text = the show name. An outro may use a title scene whose title is the episode title.
   - "bullets": 2-5 key points the hosts list or stress.
   - "steps": an ordered process or procedure, 2-6 stages, in the order spoken.
   - "cycle": a repeating loop (water cycle, nitrogen cycle...), 3-6 stages, in order; the last leads back to the first.
   - "definition": when a term is introduced and explained: term + its meaning in the textbook's words.
   - "compare": two things contrasted (columns), with up to 4 rows.
   - "keyfact": ONE striking number or fact from the source: term = the value (exactly as in the source), text = what it measures.
   - "figure": ONLY when "figures" lists a picture that clearly belongs to what is being said now. Use its id; add a short title; never invent ids. Use each figure at most once.
   - "recap": the recap segment: 2-5 takeaways, each a short phrase that appears in the script.
4. REVEAL. For items and rows, reveal[k] is the script line where item k is first discussed (non-decreasing, inside the scene, and reveal[0] = from_turn), so the screen builds up in time with the voices.
5. LANGUAGE. {language_rules_visual}
6. Reply with the JSON object only.
{extra}"""


def direct_prompt(*, language: str, show: str, episode_title: str, segment: dict, turns: list[dict], source_units: list[dict],
                  figures: list[dict], scene_words: int, hosts: dict, mode: str = "fresh", instruction: Optional[str] = None,
                  previous: Optional[list[dict]] = None, issues: Optional[list[str]] = None) -> tuple[str, str]:
    """Build the `(system, user)` prompt that asks the video director to plan a segment's slides from the hosts' own lines."""
    lm = lang_block(language)
    if language == "en":
        rules = "Write all on-screen text in clear English that matches the script's own wording."
    else:
        rules = (f"Write all on-screen text in {lm['name']} ({lm['script']} script), simple and clear, using the same terms the hosts use. "
                 f"NEVER use Latin letters (transliterate English words into {lm['script']}). Digits 0-9 are fine.")
    extra = ""
    if mode == "revise_issues":
        extra = "\nYou are REVISING your previous storyboard: fix every listed problem and keep everything else."
    elif mode == "reviewer_request":
        extra = "\nThe producer asked for a change to the visuals: apply it where it makes sense while keeping every rule."
    system = DIRECT_SYSTEM.format(scene_words=scene_words, language_rules_visual=rules, extra=extra)
    user = payload_block({
        "mode": mode,
        "show_name": show,
        "episode_title": episode_title,
        "language": lm,
        "hosts": hosts,
        "segment": segment,
        "script_lines": turns,
        "source_units": source_units,
        "figures": figures,
        "producer_instruction": instruction,
        "previous_storyboard": previous,
        "problems_to_fix": issues,
    })
    return system, user


# ============================================================================ POLISH (translate mode)

POLISH_SYSTEM = TASK_TAG + """polish
You are a native-speaker editor of {language}. You receive English podcast turns and a machine translation of each into {language}. Return each translation polished so it sounds like natural, friendly spoken {language} (a teacher chatting, not a textbook), keeping the meaning EXACTLY: add no facts, drop no facts, change no numbers. Use {script} script only: no Latin letters. Keep each turn roughly the same length.

Reply with ONE JSON object: {{"turns": [{{"i": int, "text": string}}]}} with an entry for every input turn."""


def polish_prompt(*, language: str, items: list[dict]) -> tuple[str, str]:
    lm = lang_block(language)
    return POLISH_SYSTEM.format(language=lm["name"], script=lm["script"]), payload_block({"turns": items, "language": lm})


# ============================================================================ OCR

OCR_PROMPT = (
    "Transcribe this textbook page faithfully, in reading order. Keep headings on their own lines, separate paragraphs "
    "with a blank line, keep list items one per line, and render any table as rows with ' | ' between cells. Do not "
    "summarise, translate, or add commentary. Skip running headers, footers and page numbers. For figures, write a "
    "single line starting with 'Figure:' that gives the caption only. Output only the transcription."
)
