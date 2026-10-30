"""Printable pages: the study sheet (takeaways, glossary, quiz + answer key) and the quality evidence report.
Plain self-contained HTML (no scripts, no external files) so the student can print either one to PDF."""
from __future__ import annotations

from html import escape as e
from typing import Optional

_FONT = '"Segoe UI","Noto Sans","Noto Sans Devanagari","Noto Sans Tamil","Noto Sans Telugu","Nirmala UI",system-ui,sans-serif'

_BASE = """
:root{--ink:#14213d;--mut:#5b6785;--line:#dfe4ee;--bg:#fff;--soft:#f4f6fb;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--a:#0f766e;--b:#be185d;--brand:#14213d}
*{box-sizing:border-box}body{margin:0;background:#eef1f7;color:var(--ink);font:15px/1.55 %s}
.page{max-width:860px;margin:24px auto;background:var(--bg);padding:40px 48px;border-radius:10px;box-shadow:0 2px 14px #14213d1a}
h1{font-size:26px;line-height:1.2;margin:0 0 4px}h2{font-size:17px;margin:30px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
h3{font-size:14px;margin:18px 0 6px}p{margin:6px 0}.mut{color:var(--mut)}.small{font-size:12.5px}
.chips span{display:inline-block;margin:0 6px 6px 0;padding:2px 10px;border-radius:99px;background:var(--soft);font-size:12.5px}
table{border-collapse:collapse;width:100%%;font-size:13.5px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.banner{background:#fff7e6;border:1px solid #f3d38a;padding:8px 12px;border-radius:8px;margin:12px 0;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.card{border:1px solid var(--line);border-radius:10px;padding:10px 12px}.card b{display:block;font-size:24px;line-height:1.1}
.card span{font-size:12px;color:var(--mut)}
.bar{height:10px;border-radius:6px;background:var(--soft);overflow:hidden;display:flex}.bar i{display:block;height:100%%}
.st{display:inline-block;min-width:44px;text-align:center;padding:0 8px;border-radius:99px;font-size:11.5px;font-weight:700;color:#fff}
.st.pass{background:var(--ok)}.st.warn{background:var(--warn)}.st.fail{background:var(--bad)}.st.none{background:#94a3b8}
.tiles{display:flex;flex-wrap:wrap;gap:3px}.tile{width:16px;height:16px;border-radius:3px;background:#cbd5e1}
.tile.covered,.tile.planned{background:#16a34a}.tile.thin{background:#f59e0b}.tile.missing{background:#dc2626}.tile.heading{background:#e2e8f0;height:16px;width:6px}
.legend{font-size:12px;color:var(--mut);margin-top:6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px;vertical-align:-1px}
ol.q{padding-left:22px}ol.q li{margin:0 0 14px;break-inside:avoid}.opt{margin:2px 0 2px 4px}
dl.g{display:grid;grid-template-columns:minmax(120px,28%%) 1fr;gap:6px 16px}dl.g dt{font-weight:700}dl.g dd{margin:0}
.key{columns:2;font-size:13.5px}
@media print{body{background:#fff}.page{box-shadow:none;margin:0;max-width:none;padding:0}h2{break-after:avoid}}
@page{size:A4;margin:16mm}
""" % _FONT


def _doc(title: str, lang: str, body: str) -> str:
    return (f'<!doctype html><html lang="{e(lang)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(title)}</title><style>{_BASE}</style></head><body><div class=\"page\">{body}</div></body></html>")


# --------------------------------------------------------------------------- study sheet
def sheet_html(title: str, lang: str, pack: dict, show: str = "") -> str:
    """A printable study sheet (takeaways, glossary, quiz and answer key) as a self-contained HTML page."""
    parts = [f"<h1>{e(title)}</h1><p class='mut'>Study sheet" + (f" from <b>{e(show)}</b>" if show else "") + "</p>"]
    if pack.get("takeaways"):
        parts.append("<h2>Key takeaways</h2><ul>" + "".join(f"<li>{e(t['text'])}</li>" for t in pack["takeaways"]) + "</ul>")
    if pack.get("glossary"):
        parts.append("<h2>Glossary</h2><dl class='g'>" + "".join(f"<dt>{e(g['term'])}</dt><dd>{e(g['definition'])}</dd>" for g in pack["glossary"]) + "</dl>")
    if pack.get("quiz"):
        parts.append("<h2>Quiz</h2><ol class='q'>")
        for q in pack["quiz"]:
            parts.append(f"<li><div>{e(q['question'])}</div>" + "".join(f"<div class='opt'>{'ABCD'[i]}. {e(o)}</div>" for i, o in enumerate(q["options"])) + "</li>")
        parts.append("</ol><h2>Answer key</h2><div class='key'>")
        for n, q in enumerate(pack["quiz"], 1):
            parts.append(f"<p><b>{n}. {'ABCD'[q['answer']]}</b> {e(q.get('explanation', ''))}</p>")
        parts.append("</div>")
    meta = pack.get("meta") or {}
    if meta.get("offline"):
        parts.append("<p class='banner'>Made in demo mode from the chapter's own sentences (no AI model).</p>")
    return _doc(f"{title} - study sheet", lang, "".join(parts))


# --------------------------------------------------------------------------- evidence report
def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _mmss(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def report_html(ins: dict) -> str:
    """The printable Quality evidence report as a self-contained HTML page, with a clear banner when the episode was made
    in demo mode.
    """
    v = ins.get("validation") or {}
    loop, spk, cov, gr, cost = ins["loop"], ins["speaking"], ins.get("coverage"), ins.get("grounding"), ins["cost"]
    b: list[str] = []
    b.append(f"<h1>{e(ins['title'])}</h1>")
    b.append(f"<p class='mut'>Quality evidence report · {e(ins['show_name'])} · {e(ins['language_name'])}"
             f"{' (written in English, then translated)' if ins['writing_mode'] == 'translate' and ins['language'] != 'en' else ''}"
             f" · {_mmss(ins['duration_s'])} long · hosts {e(spk['A']['name'])} and {e(spk['B']['name'])}</p>")
    if ins.get("demo_mode"):
        b.append("<div class='banner'><b>Demo mode.</b> This episode was made without real AI models (offline stand-ins), so the numbers show that the "
                 "machinery works, not how good real model output is. Generate the episode with your API keys for evidence you can cite.</div>")
    cards = [
        (f"{v.get('score', '–')}" + ("/100" if v else ""), "overall validation score"),
        (f"{loop['passed_first_try'] + loop['repaired']}/{loop['segments']}", "segments pass the fact-check" + (" (checker off)" if not loop["fact_check_on"] else "")),
        (_pct(gr["grounded_share"]) if gr else "n/a", "lines with every number found in the chapter"),
        (_pct(cov["covered_share"]) if cov else "n/a", "of chapter paragraphs covered"),
        (f"${cost['usd']:.2f}", f"model cost · {cost['llm_calls']} calls"),
    ]
    b.append("<div class='cards'>" + "".join(f"<div class='card'><b>{e(a)}</b><span>{e(c)}</span></div>" for a, c in cards) + "</div>")

    b.append("<h2>What was checked, and how</h2><ul class='small'>"
             "<li><b>Planned from the chapter.</b> The plan ties every segment to the chapter paragraphs it may use and lists the key points it must make.</li>"
             "<li><b>Written only from those paragraphs</b>, then checked by rules (numbers that are not in the chapter, over-long lines, Latin letters in native-script text) "
             "and by a second model acting as fact-checker.</li>"
             f"<li><b>Repaired automatically</b> (up to {loop['max_revisions']} rounds). A segment that still fails is flagged <i>needs review</i> and never hidden.</li>"
             "<li><b>Voiced and mixed</b>, then validated again on the finished audio: length, clipping, silence, levels, subtitles and chapter marks.</li></ul>")

    b.append("<h2>Fact-check loop</h2>")
    seg_n = max(1, loop["segments"])
    parts = [(loop["passed_first_try"], "#16a34a", "passed first time"), (loop["repaired"], "#0ea5e9", "fixed by the repair loop"),
             (loop["needs_review"], "#dc2626", "flagged for review")]
    b.append("<div class='bar'>" + "".join(f"<i style='width:{100 * n / seg_n:.1f}%;background:{c}'></i>" for n, c, _ in parts) + "</div>")
    b.append("<p class='small mut'>" + " · ".join(f"{n} {t}" for n, _, t in parts) + f" · {loop['repair_rounds']} repair round(s) in total</p>")

    b.append("<h2>Segments</h2><table><tr><th>#</th><th>Segment</th><th>Words</th><th>Time</th><th>Check</th><th>Drafts</th><th>Open issues</th></tr>")
    for r in ins["segments"]:
        st = "fail" if r["needs_review"] else ("pass" if r["verdict"] == "pass" else ("warn" if r["verdict"] == "revise" else "none"))
        label = {"pass": "pass", "warn": "revise", "fail": "review", "none": "–"}[st]
        b.append(f"<tr><td>{r['index']}</td><td>{e(r['title'])} <span class='mut small'>({e(r['kind'])})</span></td><td>{r['words']}</td>"
                 f"<td>{_mmss(r['seconds'])}</td><td><span class='st {st}'>{label}</span></td><td>{r['attempts'] or '–'}</td>"
                 f"<td>{r['major']} major, {r['minor']} minor{', ' + str(r['missing']) + ' missing point(s)' if r['missing'] else ''}</td></tr>")
    b.append("</table>")

    if v:
        b.append(f"<h2>Validation ({e(v['verdict'])}: {v['counts'].get('pass', 0)} passed, {v['counts'].get('warn', 0)} warnings, {v['counts'].get('fail', 0)} failed)</h2><table>")
        area = None
        for c in v["checks"]:
            if c["area"] != area:
                area = c["area"]
                b.append(f"<tr><th colspan='3'>{e(area)}</th></tr>")
            b.append(f"<tr><td><span class='st {c['status']}'>{c['status']}</span></td><td>{e(c['name'])}</td><td class='small'>{e(c['detail'])}</td></tr>")
        b.append("</table>")

    b.append("<h2>Conversation balance</h2><table><tr><th>Host</th><th>Speaking time</th><th>Words</th><th>Turns</th><th>Questions</th><th>Reactions</th></tr>")
    for k, col in (("A", "var(--a)"), ("B", "var(--b)")):
        h = spk[k]
        b.append(f"<tr><td><b style='color:{col}'>{e(h['name'])}</b></td><td>{_mmss(h['seconds'])} ({round(h['share'] * 100)}%)</td><td>{h['words']}</td>"
                 f"<td>{h['turns']}</td><td>{h['questions']}</td><td>{h['reactions']}</td></tr>")
    b.append("</table>")
    sm = ins.get("script") or {}
    if sm:
        b.append(f"<p class='small mut'>Average {sm['avg_words_per_turn']} words per turn · speaker changes on {round(sm['speaker_switch_rate'] * 100)}% of turns · "
                 f"{round(sm['question_share'] * 100)}% questions · {round(sm['reaction_share'] * 100)}% short reactions · longest monologue {sm['longest_monologue']} words · "
                 f"{round(sm['repeated_4gram_share'] * 1000) / 10}% repeated phrases</p>")

    if cov:
        b.append("<h2>Chapter coverage</h2><div class='tiles'>" + "".join(
            f"<span class='tile {c['status']}' title='{e(c['section'] or 'paragraph ' + str(c['id']))} · {e(c['status'])}'></span>" for c in cov["cells"]) + "</div>")
        b.append("<div class='legend'><i style='background:#16a34a'></i>covered<i style='background:#f59e0b'></i>thin<i style='background:#dc2626'></i>not planned into any segment</div>")
        if not cov["comparable"]:
            b.append("<p class='small mut'>The chapter and the podcast use different scripts, so coverage shows which paragraphs were planned into a segment, not word overlap.</p>")
        if gr and gr["examples"]:
            b.append("<p class='small mut'>Unmatched numbers: " + e("; ".join(gr["examples"])) + "</p>")

    b.append("<h2>Cost and usage</h2><table>"
             f"<tr><td>Language-model calls</td><td>{cost['llm_calls']} ({cost['tokens']:,} tokens, ${cost['usd']:.4f})</td></tr>"
             f"<tr><td>Voice requests</td><td>{cost['tts_calls']} ({cost['tts_chars']:,} characters, {cost['tts_cache_hits']} reused from cache)</td></tr>"
             + (f"<tr><td>Translation requests</td><td>{cost['mt_calls']} ({cost['mt_chars']:,} characters)</td></tr>" if cost["mt_calls"] else "")
             + (f"<tr><td>Model cost per audio minute</td><td>${cost['usd_per_audio_minute']:.3f}</td></tr>" if cost["usd_per_audio_minute"] else "") + "</table>")

    lrn, ask = ins.get("learn"), ins["ask"]
    if lrn or ask["questions"]:
        b.append("<h2>Study pack and listener questions</h2><ul class='small'>")
        if lrn and lrn.get("kept"):
            k = lrn["kept"]
            b.append(f"<li>Study pack: {k['takeaways']} takeaways, {k['glossary']} glossary terms, {k['quiz']} quiz questions kept; {lrn['dropped']} dropped by the chapter check"
                     f"{' (verified by a second model)' if lrn.get('verified_by_model') else ''}.</li>")
        if ask["questions"]:
            b.append(f"<li>Ask the hosts: {ask['questions']} question(s): {ask['grounded']} answered from the chapter, {ask['declined']} honestly declined as outside the chapter"
                     f"{', ' + str(ask['needs_review']) + ' flagged for review' if ask['needs_review'] else ''}.</li>")
        b.append("</ul>")
    b.append(f"<p class='small mut'>Source: {e(ins['source']['filename'])}"
             f"{' · ' + str(ins['source']['pages']) + ' pages' if ins['source']['pages'] else ''} · {ins['source']['words']:,} words · {ins['source']['units']} paragraphs. "
             f"Episode {e(ins['id'])}.</p>")
    return _doc(f"{ins['title']} - quality report", ins["language"] if ins["language"] != "en" else "en", "".join(b))
