"""Builds a small chapter PDF (headings, paragraphs, one captioned figure) for tests and demos."""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def make_figure_png(path: Path) -> Path:
    im = Image.new("RGB", (480, 300), (150, 200, 240))
    d = ImageDraw.Draw(im)
    d.ellipse((190, 70, 290, 170), fill=(250, 220, 60))
    d.rectangle((0, 220, 480, 300), fill=(60, 120, 200))
    for x in range(20, 480, 60):
        d.arc((x, 200, x + 50, 240), 0, 180, fill=(255, 255, 255), width=3)
    im.save(path)
    return path


def make_chapter_pdf(text: str, out: Path, *, caption: str = "Fig. 2.1 Evaporation of water from a puddle",
                     figure_after: str = "Evaporation") -> Path:
    """Lines of `text` become PDF text; the figure is placed right after the heading that contains `figure_after`."""
    png = make_figure_png(out.with_suffix(".fig.png"))
    W, H = A4
    c = canvas.Canvas(str(out), pagesize=A4)
    y = H - 60

    def newpage():
        nonlocal y
        c.showPage()
        y = H - 60

    for block in re.split(r"\n\s*\n", text.strip()):
        block = block.strip()
        heading = block.startswith("#")
        txt = block.lstrip("# ").strip()
        c.setFont("Helvetica-Bold" if heading else "Helvetica", 15 if heading else 11)
        lines = textwrap.wrap(txt, 80 if not heading else 60)
        need = len(lines) * 15 + 14
        if y - need < 70:
            newpage()
            c.setFont("Helvetica-Bold" if heading else "Helvetica", 15 if heading else 11)
        for ln in lines:
            c.drawString(60, y, ln)
            y -= 15
        y -= 10
        if heading and figure_after.lower() in txt.lower():
            if y - 260 < 70:
                newpage()
            c.drawImage(str(png), 130, y - 215, width=330, height=206)
            c.setFont("Helvetica-Oblique", 10)
            c.drawString(130, y - 232, caption)
            y -= 255
    c.showPage()
    c.save()
    return out
