from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from podcast_studio.ingest import IngestError, ingest_file, ingest_text


def _make_pdf(path: Path, pages: int = 3, scanned_pages: set[int] | None = None) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    para = "Evaporation is the change of liquid water into water vapour when it is heated by the Sun. " * 3
    for pg in range(1, pages + 1):
        if scanned_pages and pg in scanned_pages:
            c.showPage()
            continue
        c.setFont("Helvetica", 9)
        c.drawString(50, H - 30, "Science - Chapter 2")
        c.drawString(W / 2, 25, str(pg))
        c.drawString(50, 12, "Reprint 2025-26")
        y = H - 90
        c.setFont("Helvetica-Bold", 16)
        c.drawString(50, y, f"{pg}.1 Heading number {pg}")
        y -= 32
        c.setFont("Helvetica", 11)
        for i in range(5):
            words = (para + f"Closing sentence {pg}-{i}.").split()
            line = ""
            for w in words:
                if c.stringWidth(line + " " + w, "Helvetica", 11) > W - 110:
                    c.drawString(50, y, line)
                    y -= 14
                    line = w
                else:
                    line = (line + " " + w).strip()
            if pg == 1 and i == 4:
                c.drawString(50, y, "and this sentence keeps going onto the next page because the")
            else:
                c.drawString(50, y, line)
            y -= 26
        if pg == 2:
            # the continuation of page 1's last paragraph starts page 2 in lowercase
            pass
        c.showPage()
    c.save()


def test_text_ingest_units_and_sections(sample_text):
    d = ingest_text(sample_text, "water_cycle.txt")
    assert d.info.title_guess == "The Water Cycle"
    heads = [u.text for u in d.units if u.kind == "heading"]
    assert "Evaporation" in heads and "Precipitation" in heads
    paras = [u for u in d.units if u.kind == "para"]
    assert all(u.section for u in paras)
    assert [u.id for u in d.units] == list(range(1, len(d.units) + 1))
    assert d.info.words > 400


def test_pdf_strips_headers_footers_and_page_numbers(tmp_path):
    p = tmp_path / "ch.pdf"
    _make_pdf(p)
    d = ingest_file(p, "ch.pdf")
    assert "Science - Chapter 2" not in d.text
    assert "Reprint 2025-26" not in d.text
    assert not any(u.text.strip() in {"1", "2", "3"} for u in d.units)
    heads = [u.text for u in d.units if u.kind == "heading"]
    assert heads == ["1.1 Heading number 1", "2.1 Heading number 2", "3.1 Heading number 3"]
    assert d.info.pages == 3


def test_pdf_scanned_pages_use_ocr_callback(tmp_path):
    p = tmp_path / "scan.pdf"
    _make_pdf(p, pages=3, scanned_pages={2})
    calls = []

    def ocr(png: bytes) -> str:
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        calls.append(1)
        return "Recognised heading\n\nThis is the recognised paragraph from the scanned page."

    d = ingest_file(p, "scan.pdf", ocr=ocr)
    assert len(calls) == 1 and d.info.ocr_used
    assert "recognised paragraph" in d.text


def test_pdf_fully_scanned_without_ocr_is_a_clear_error(tmp_path):
    p = tmp_path / "scan.pdf"
    _make_pdf(p, pages=2, scanned_pages={1, 2})
    with pytest.raises(IngestError, match="scanned"):
        ingest_file(p, "scan.pdf", ocr=None)


def test_unsupported_and_broken_inputs(tmp_path):
    (tmp_path / "x.docx").write_bytes(b"PK")
    with pytest.raises(IngestError, match="Unsupported"):
        ingest_file(tmp_path / "x.docx", "x.docx")
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf at all")
    with pytest.raises(IngestError):
        ingest_file(tmp_path / "bad.pdf", "bad.pdf")
    with pytest.raises(IngestError):
        ingest_text("   \n\n  ")
    with pytest.raises(IngestError, match="very long"):
        ingest_text("word " * 100, max_chars=50)


def test_hindi_text_ingest_roundtrip(tmp_path):
    text = "# जल चक्र\n\nपानी समुद्र से भाप बनकर ऊपर उठता है। फिर बादल बनते हैं।\n\n## वाष्पीकरण\n\nसूर्य की गर्मी से पानी भाप बनता है।"
    f = tmp_path / "hi.txt"
    f.write_bytes(text.encode("utf-8"))
    d = ingest_file(f, "hi.txt")
    assert d.info.script_guess == "Devanagari" and d.info.title_guess == "जल चक्र"
    assert any("वाष्पीकरण" == u.text for u in d.units)


def test_long_paragraph_is_split_into_bounded_units():
    big = ". ".join(f"Sentence number {i} says something useful" for i in range(200)) + "."
    d = ingest_text(big, "big.txt")
    assert len(d.units) > 3 and all(len(u.text) <= 1300 for u in d.units)
