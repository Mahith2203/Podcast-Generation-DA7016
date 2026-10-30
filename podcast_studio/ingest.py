"""Turn an uploaded chapter (PDF / text / markdown) into clean, numbered 'source units'.

The numbered units are the backbone of faithfulness: the planner assigns units to segments, the writer sees the
units for its segment, and the fact-checker verifies the script against them.
"""
from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .models import SourceInfo, SourceUnit
from .text import ends_with_terminator, guess_language
from .util import slugify

try:  # PyMuPDF >= 1.24 exposes `pymupdf`; older versions only `fitz`
    import pymupdf  # type: ignore
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore


class IngestError(ValueError):
    """Raised with a message that is safe and useful to show to the user."""


@dataclass
class Block:
    """One piece of text found on a PDF page (paragraph, heading, list or table) with its font size and position."""
    kind: str  # para | heading | list | table
    text: str
    page: int
    size: float = 0.0
    bold: float = 0.0
    y0: float = 0.0
    y1: float = 0.0
    page_h: float = 0.0
    x0: float = 0.0


@dataclass
class SourceDoc:
    """A cleaned chapter: the full text, the numbered source units, a summary (`SourceInfo`) and the text of each page."""
    text: str
    units: list[SourceUnit]
    info: SourceInfo
    pages_text: list[str] = field(default_factory=list)


_BOILERPLATE = re.compile(r"^\s*(reprint|rationali[sz]ed|re-?print)\b.*\d{4}\s*[-–]\s*\d{2,4}\s*$", re.I)
_PAGE_NUM = re.compile(r"^\s*(?:page\s*)?(\d{1,4}|[ivxlcdm]{1,6})\s*$", re.I)
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+){0,3})[.)]?\s+\S")
_LIST_MARK = re.compile(r"^\s*(?:[•▪●◦\-–*]|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)]|\(?[ivx]{1,4}[.)])\s+")
_MAX_UNIT_CHARS = 1200


# =========================================================================== public API


def ingest_text(text: str, filename: str = "pasted text", *, max_chars: int = 400_000) -> SourceDoc:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise IngestError("The text is empty.")
    items = _plain_items(text)
    return _finish(items, filename, pages=1, max_chars=max_chars, ocr_used=False, warnings=[])


def ingest_file(path: Path, filename: str, *, ocr: Optional[Callable[[bytes], str]] = None, max_chars: int = 400_000,
                progress: Optional[Callable[[str], None]] = None) -> SourceDoc:
    """Read a PDF or text file into a `SourceDoc`. Scanned pages fall back to the `ocr` callback when one is given."""
    ext = Path(filename).suffix.lower()
    if ext in {".txt", ".md", ".markdown", ".text"}:
        raw = Path(path).read_bytes()
        for enc in ("utf-8-sig", "utf-16", "cp1252"):
            try:
                return ingest_text(raw.decode(enc), filename, max_chars=max_chars)
            except UnicodeDecodeError:
                continue
        raise IngestError("Could not read the text file (unknown encoding). Save it as UTF-8.")
    if ext == ".pdf":
        return _ingest_pdf(Path(path), filename, ocr=ocr, max_chars=max_chars, progress=progress)
    raise IngestError(f"Unsupported file type '{ext or 'unknown'}'. Upload a PDF, .txt or .md file, or paste the text.")


# =========================================================================== PDF


def _ingest_pdf(path: Path, filename: str, *, ocr, max_chars: int, progress) -> SourceDoc:
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:  # noqa: BLE001
        raise IngestError(f"Could not open the PDF: {e}") from e
    if doc.needs_pass:
        raise IngestError("This PDF is password-protected. Remove the password and upload it again.")
    n_pages = doc.page_count
    if n_pages == 0:
        raise IngestError("The PDF has no pages.")
    warnings: list[str] = []
    if n_pages > 80:
        warnings.append(f"The PDF has {n_pages} pages - a single chapter or section works best.")

    pages: list[list[Block]] = []
    empty_pages: list[int] = []
    for pno in range(n_pages):
        blocks = _page_blocks(doc[pno], pno)
        pages.append(blocks)
        if sum(len(b.text) for b in blocks) < 40:
            empty_pages.append(pno)

    ocr_used = False
    if empty_pages:
        if ocr is None:
            if len(empty_pages) == n_pages:
                raise IngestError("This PDF looks scanned (no text layer) and OCR is not available. "
                                  "Upload a text PDF or paste the text.")
            warnings.append(f"{len(empty_pages)} page(s) have no text layer and were skipped (OCR unavailable).")
        else:
            if progress:
                progress(f"Running OCR on {len(empty_pages)} scanned page(s)...")

            def _do(pno: int):
                png = doc[pno].get_pixmap(dpi=150).tobytes("png")
                return pno, ocr(png)

            with ThreadPoolExecutor(max_workers=3) as ex:
                for pno, txt in ex.map(_do, empty_pages):
                    pages[pno] = [Block(kind=k, text=t, page=pno) for k, t in _plain_items(txt)]
            ocr_used = True
            warnings.append(f"OCR was used for {len(empty_pages)} page(s); check the extracted text if numbers look odd.")

    items = _assemble_blocks(pages)
    return _finish(items, filename, pages=n_pages, max_chars=max_chars, ocr_used=ocr_used, warnings=warnings)


def _page_blocks(page, pno: int) -> list[Block]:
    H = float(page.rect.height)
    out: list[Block] = []
    try:
        d = page.get_text("dict", sort=True)
    except Exception:  # noqa: BLE001
        return out
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines, chars, size_sum, bold_chars = [], 0, 0.0, 0
        for ln in b.get("lines", []):
            s = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            lines.append(s)
            for sp in ln.get("spans", []):
                n = len(sp.get("text", "").strip())
                chars += n
                size_sum += float(sp.get("size", 0)) * n
                if (sp.get("flags", 0) & 16) or "bold" in str(sp.get("font", "")).lower():
                    bold_chars += n
        text = "\n".join(lines).strip()
        if not text or chars == 0:
            continue
        x0, y0, x1, y1 = b["bbox"]
        out.append(Block("para", text, pno, size=size_sum / chars, bold=bold_chars / chars, y0=y0, y1=y1, page_h=H, x0=x0))
    # tables (PyMuPDF >= 1.23). Replace overlapping text blocks with one table block.
    try:
        tabs = page.find_tables()
        for t in getattr(tabs, "tables", []):
            rows = t.extract()
            if not rows or len(rows) < 2:
                continue
            bx0, by0, bx1, by1 = t.bbox
            body = "\n".join(" | ".join((c or "").replace("\n", " ").strip() for c in r) for r in rows).strip()
            if not body.replace("|", "").strip():
                continue
            out = [b for b in out if not (bx0 - 2 <= (b.x0) <= bx1 + 2 and by0 - 2 <= (b.y0 + b.y1) / 2 <= by1 + 2)]
            out.append(Block("table", body, pno, y0=by0, y1=by1, page_h=H, x0=bx0))
    except Exception:  # noqa: BLE001
        pass
    out.sort(key=lambda b: (round(b.y0, 1), b.x0))
    return out


def _norm_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip()


def _assemble_blocks(pages: list[list[Block]]) -> list[tuple[str, str]]:
    n_pages = len(pages)
    allb = [b for p in pages for b in p]
    if not allb:
        raise IngestError("No text could be extracted from the PDF.")
    # --- repeated headers/footers and page numbers
    margin_counts: Counter[str] = Counter()
    seen_on: dict[str, set[int]] = {}
    for b in allb:
        if b.kind == "table" or b.page_h <= 0:
            continue
        if b.y1 < 0.10 * b.page_h or b.y0 > 0.90 * b.page_h:
            k = _norm_key(b.text)
            seen_on.setdefault(k, set()).add(b.page)
    for k, ps in seen_on.items():
        margin_counts[k] = len(ps)
    min_repeat = max(2, int(0.3 * n_pages + 0.5)) if n_pages >= 3 else 99

    kept: list[Block] = []
    for b in allb:
        t = b.text.strip()
        if b.kind != "table" and b.page_h > 0:
            in_margin = b.y1 < 0.10 * b.page_h or b.y0 > 0.90 * b.page_h
            if in_margin and (_PAGE_NUM.match(t) or margin_counts[_norm_key(t)] >= min_repeat):
                continue
        if _BOILERPLATE.match(t):
            continue
        kept.append(b)

    # --- classify
    size_cnt: Counter[int] = Counter()
    for b in kept:
        if b.kind == "para":
            size_cnt[round(b.size)] += len(b.text)
    body_size = size_cnt.most_common(1)[0][0] if size_cnt else 0
    for b in kept:
        if b.kind != "para":
            continue
        lines = [ln for ln in b.text.split("\n") if ln.strip()]
        if sum(1 for ln in lines if _LIST_MARK.match(ln)) >= 2 and len(lines) >= 2:
            b.kind = "list"
            b.text = "\n".join(re.sub(r"\s+", " ", ln).strip() for ln in lines)
            continue
        words = len(b.text.split())
        flat = re.sub(r"\s+", " ", b.text).strip()
        short = words <= 14 and len(lines) <= 2 and not flat.endswith((".", ",", ";", "।"))
        bigger = body_size and b.size >= body_size * 1.12
        numbered = bool(_NUMBERED_HEADING.match(flat)) and words <= 10
        if short and not _PAGE_NUM.match(flat) and (bigger or b.bold >= 0.85 or numbered):
            b.kind = "heading"
            b.text = flat
        else:
            b.text = _reflow(b.text)

    # --- merge a paragraph that continues across a page break
    merged: list[Block] = []
    for b in kept:
        if (merged and b.kind == "para" and merged[-1].kind == "para" and b.page == merged[-1].page + 1
                and not ends_with_terminator(merged[-1].text) and (b.text[:1].islower() or merged[-1].text.endswith(","))):
            merged[-1].text = (merged[-1].text + " " + b.text).strip()
            merged[-1].page = b.page
            continue
        merged.append(b)
    return [(b.kind, b.text) for b in merged if b.text.strip()]


def _reflow(text: str) -> str:
    t = re.sub(r"(\w)-\n([a-zऀ-෿])", r"\1\2", text)  # re-join words hyphenated at a line end
    t = t.replace("\n", " ")
    return re.sub(r"\s+", " ", t).strip()


# =========================================================================== plain text


def _plain_items(text: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for para in re.split(r"\n\s*\n+", text.strip()):
        lines = [ln.rstrip() for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if lines[0].lstrip().startswith("#"):
            head = re.sub(r"^#+\s*", "", lines[0]).strip()
            items.append(("heading", head))
            rest = " ".join(lines[1:]).strip()
            if rest:
                items.append(("para", re.sub(r"\s+", " ", rest)))
            continue
        if sum(1 for ln in lines if _LIST_MARK.match(ln)) >= 2 and len(lines) >= 2:
            items.append(("list", "\n".join(re.sub(r"\s+", " ", ln).strip() for ln in lines)))
            continue
        if sum(1 for ln in lines if "|" in ln) >= 2 and len(lines) >= 2:
            items.append(("table", "\n".join(lines)))
            continue
        words = len(flat.split())
        if len(lines) <= 2 and words <= 12 and not flat.endswith((".", ",", ";", "।", "?")) and not _PAGE_NUM.match(flat):
            items.append(("heading", flat))
        else:
            items.append(("para", _reflow("\n".join(lines))))
    return items


# =========================================================================== units


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    from .text import split_sentences

    out, cur = [], ""
    for s in split_sentences(text):
        if cur and len(cur) + 1 + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    return out


def _finish(items: list[tuple[str, str]], filename: str, *, pages: int, max_chars: int, ocr_used: bool,
            warnings: list[str]) -> SourceDoc:
    items = [(k, t) for k, t in items if t.strip()]
    if not items:
        raise IngestError("No readable text was found.")
    units: list[SourceUnit] = []
    section = ""
    uid = 1
    for kind, text in items:
        if kind == "heading":
            section = text
            units.append(SourceUnit(id=uid, kind="heading", text=text, section=text))
            uid += 1
            continue
        for piece in _split_long(text, _MAX_UNIT_CHARS):
            units.append(SourceUnit(id=uid, kind=kind if kind in {"list", "table"} else "para", text=piece, section=section))
            uid += 1
    total_chars = sum(len(u.text) for u in units)
    if total_chars > max_chars:
        raise IngestError(f"The source is very long ({total_chars:,} characters). Upload a single chapter or section "
                          f"(limit {max_chars:,} characters).")
    if not any(u.kind != "heading" for u in units):
        raise IngestError("Only headings were found - there is no body text to turn into a podcast.")
    full = "\n\n".join(("## " + u.text) if u.kind == "heading" else u.text for u in units)
    words = sum(len(u.text.split()) for u in units if u.kind != "heading")
    if words < 40:
        warnings.append("The source is very short; the podcast will be brief.")
    lang, script = guess_language(full)
    title = next((u.text for u in units if u.kind == "heading"), "") or Path(filename).stem.replace("_", " ").replace("-", " ")
    info = SourceInfo(filename=filename, pages=pages, chars=total_chars, words=words, unit_count=len(units),
                      ocr_used=ocr_used, script_guess=script, title_guess=title[:120].strip(), warnings=warnings)
    return SourceDoc(text=full, units=units, info=info)


def default_title(info: SourceInfo, filename: str) -> str:
    return info.title_guess or slugify(Path(filename).stem)
