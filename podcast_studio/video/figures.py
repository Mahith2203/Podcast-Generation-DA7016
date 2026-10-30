"""Find the pictures in a chapter PDF so the video can show the textbook's own figures with their original captions."""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Optional

from ..models import Figure, SourceUnit

MAX_FIGURES = 24
MIN_W, MIN_H = 120, 90          # pixels
CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?s?\.?\s*[\dIVX]+[\.\-–]?\s*[\dA-Za-z]*)", re.I)
_TOKEN = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text)}


def _best_unit(text: str, units: list[SourceUnit]) -> Optional[int]:
    a = _tokens(text)
    if len(a) < 2:
        return None
    best, best_score = None, 0.0
    for u in units:
        if u.kind == "heading":
            continue
        b = _tokens(u.text)
        if not b:
            continue
        score = len(a & b) / len(a | b)
        if score > best_score:
            best, best_score = u.id, score
    return best if best_score >= 0.08 else None


def extract_figures(pdf_path: Path, out_dir: Path, units: list[SourceUnit], *, project_dir: Path) -> list[Figure]:
    """Save chapter pictures as PNG (files are written under out_dir, paths are returned relative to project_dir)."""
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore
        from PIL import Image
    except ImportError:  # pragma: no cover - both are required packages
        return []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    found: list[tuple[int, tuple, bytes, str, str]] = []   # page, bbox, bytes, caption, near_text
    seen_hash: dict[str, int] = {}
    n_pages = doc.page_count
    for pno in range(n_pages):
        page = doc[pno]
        pw, ph = float(page.rect.width), float(page.rect.height)
        try:
            d = page.get_text("dict", sort=True)
        except Exception:  # noqa: BLE001
            continue
        text_blocks = []
        for b in d.get("blocks", []):
            if b.get("type") == 0:
                txt = " ".join(sp["text"] for ln in b.get("lines", []) for sp in ln.get("spans", [])).strip()
                if txt:
                    text_blocks.append((b["bbox"], txt))
        for b in d.get("blocks", []):
            if b.get("type") != 1 or not b.get("image"):
                continue
            x0, y0, x1, y1 = b["bbox"]
            area = max(0.0, (x1 - x0) * (y1 - y0))
            if b.get("width", 0) < MIN_W or b.get("height", 0) < MIN_H or area < 0.02 * pw * ph or area > 0.85 * pw * ph:
                continue
            h = hashlib.sha1(b["image"]).hexdigest()
            seen_hash[h] = seen_hash.get(h, 0) + 1
            caption = ""
            below = sorted((tb for tb in text_blocks if tb[0][1] >= y1 - 2 and tb[0][1] - y1 < 70 and tb[0][0] < x1 and tb[0][2] > x0), key=lambda t: t[0][1])
            above = sorted((tb for tb in text_blocks if tb[0][3] <= y0 + 2 and y0 - tb[0][3] < 50 and tb[0][0] < x1 and tb[0][2] > x0), key=lambda t: -t[0][3])
            for _, txt in below[:2] + above[:1]:
                if CAPTION_RE.match(txt):
                    caption = re.sub(r"\s+", " ", txt)[:220]
                    break
            before = [txt for bb, txt in text_blocks if bb[3] <= y0 + 2 and len(txt.split()) >= 6 and not CAPTION_RE.match(txt)]
            near = re.sub(r"\s+", " ", before[-1])[:260] if before else ""
            found.append((pno, (x0, y0, x1, y1), b["image"], caption, near))
    doc.close()
    figs: list[Figure] = []
    emitted: set[str] = set()
    for pno, _bbox, raw, caption, near in found:
        h = hashlib.sha1(raw).hexdigest()
        if h in emitted or (n_pages >= 3 and seen_hash[h] > max(2, 0.3 * n_pages)):  # repeated logo / ornament
            continue
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            if im.mode == "RGBA":
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            if max(im.size) > 1600:
                im.thumbnail((1600, 1600))
        except Exception:  # noqa: BLE001
            continue
        emitted.add(h)
        fid = f"fig{len(figs) + 1:02d}"
        path = out_dir / f"{fid}.png"
        im.save(path, "PNG")
        figs.append(Figure(id=fid, page=pno + 1, file=path.relative_to(project_dir).as_posix(), width=im.size[0], height=im.size[1],
                           caption=caption, near_text=near, unit_id=_best_unit(f"{caption} {near}", units)))
        if len(figs) >= MAX_FIGURES:
            break
    return figs
