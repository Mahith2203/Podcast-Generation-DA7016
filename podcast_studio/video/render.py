"""Draws the slides with a real browser engine (Chromium or Edge: correct shaping for every Indic script), caches every
distinct frame on disk, and blends the cross-fades with Pillow. ffmpeg then only has to string the frames together."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from ..models import Figure, Project, Scene, SegmentStoryboard, VideoCfg
from ..voices import LANGS
from . import templates
from .timeline import Shot

RES = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}

INSTALL_HELP = ("The video needs a browser engine to draw the slides. Run this once, then try again:\n"
                "    python -m playwright install chromium\n"
                "(Microsoft Edge or Google Chrome, if installed, are used automatically.)")


class RenderError(RuntimeError):
    """Raised when slides cannot be drawn."""
    pass


def _launch(pw):
    errs: list[str] = []
    for kw in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
        try:
            return pw.chromium.launch(args=["--force-color-profile=srgb", "--font-render-hinting=none", "--disable-gpu"], **kw)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{kw.get('channel', 'chromium')}: {str(e).splitlines()[0][:120] if str(e) else e.__class__.__name__}")
    raise RenderError(INSTALL_HELP + "\n(" + "; ".join(errs) + ")")


def browser_status() -> tuple[bool, str]:
    """For `doctor`: can we open a browser at all? Tiny page, a second or two."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "the 'playwright' package is not installed: run `pip install playwright`, then `python -m playwright install chromium`"
    try:
        with sync_playwright() as pw:
            b = _launch(pw)
            name = f"{b.browser_type.name} {b.version}"
            b.close()
        return True, name
    except RenderError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"{e.__class__.__name__}: {str(e)[:200]}"


def _h(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]


def is_rtl(lang: str) -> bool:
    return "Arabic" in LANGS.get(lang, {}).get("script", "")


def user_fonts(fonts_dir: Optional[Path]) -> tuple[str, str]:
    """(@font-face css, font-family list) for any .ttf/.otf/.woff2 the user drops into the `fonts` folder."""
    if not fonts_dir or not fonts_dir.is_dir():
        return "", ""
    css, fam = [], []
    for i, f in enumerate(sorted(p for p in fonts_dir.iterdir() if p.suffix.lower() in {".ttf", ".otf", ".woff2", ".woff"})):
        css.append(f'@font-face{{font-family:"UserFont{i}";src:url("{f.resolve().as_uri()}")}}')
        fam.append(f'"UserFont{i}"')
    return "".join(css), (",".join(fam) + ",") if fam else ""


class FrameRenderer:
    """One browser for the whole video. Use as a context manager."""

    def __init__(self, project: Project, cfg: VideoCfg, *, cache_dir: Path, project_dir: Path, fonts_dir: Optional[Path] = None):
        self.project, self.cfg = project, cfg
        self.w, self.h = RES.get(cfg.resolution, RES["720p"])
        self.frames_dir = cache_dir / "frames"
        self.pages_dir = cache_dir / "pages"
        self.project_dir = project_dir
        self.font_css, self.families = user_fonts(fonts_dir)
        self.warnings: list[str] = []
        self.browser_name = ""
        self.rendered = 0
        self.reused = 0
        self._pw = self._browser = self._page = None
        for d in (self.frames_dir, self.pages_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ browser lifecycle
    def __enter__(self) -> "FrameRenderer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RenderError("The 'playwright' package is not installed. Run:  pip install playwright   then:  "
                              "python -m playwright install chromium") from e
        self._pw = sync_playwright().start()
        try:
            self._browser = _launch(self._pw)
        except Exception:
            self.close()
            raise
        self.browser_name = f"{self._browser.browser_type.name} {self._browser.version}"
        ctx = self._browser.new_context(viewport={"width": self.w, "height": self.h}, device_scale_factor=1)
        self._page = ctx.new_page()

    def close(self) -> None:
        """Close the page, the browser and Playwright, ignoring errors from ones that are already gone."""
        for obj, meth in ((self._page, None), (self._browser, "close"), (self._pw, "stop")):
            try:
                if meth and obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._page = None

    # ------------------------------------------------------------------ rendering
    def _ctx(self, seg_pos: int, fig: Optional[Figure]) -> templates.Ctx:
        p, seg = self.project, self.project.segments[seg_pos]
        lang = p.settings.language
        return templates.Ctx(
            width=self.w, height=self.h, theme=self.cfg.theme, lang=lang, rtl=is_rtl(lang), show=p.settings.show_name,
            seg_title=seg.title, seg_index=seg_pos, seg_count=len(p.segments),
            host_names=(p.host("A").name, p.host("B").name), captions=self.cfg.captions, speakers=self.cfg.speakers,
            figure_uri=(self.project_dir / fig.file).resolve().as_uri() if fig else None,
            figure_caption=fig.caption if fig else "", font_css=self.font_css, extra={"families": self.families})

    def _figure(self, scene: Scene, figures: dict[str, Figure]) -> Optional[Figure]:
        if scene.layout != "figure" or not scene.figure or not self.cfg.use_figures:
            return None
        f = figures.get(scene.figure)
        return f if f and (self.project_dir / f.file).is_file() else None

    def render_scene(self, seg_pos: int, scene_idx: int, scene: Scene, states: list[tuple], figures: dict[str, Figure]) -> dict[tuple, Path]:
        """states: (reveal, speaker, name, text). Returns the PNG for each. Frames that already exist are reused."""
        fig = self._figure(scene, figures)
        ctx = self._ctx(seg_pos, fig)
        base = _h(templates.TEMPLATE_VERSION, self.w, self.h, self.cfg.theme, ctx.lang, ctx.show, ctx.seg_title, seg_pos, ctx.seg_count,
                  self.cfg.captions, self.cfg.speakers, scene.model_dump(), fig.file if fig else None,
                  (self.project_dir / fig.file).stat().st_size if fig else 0, self.families)
        out: dict[tuple, Path] = {}
        todo: list[tuple[tuple, Path]] = []
        for st in states:
            path = self.frames_dir / f"{_h(base, list(st))}.png"
            out[st] = path
            if path.exists():
                self.reused += 1
            else:
                todo.append((st, path))
        if not todo:
            return out
        self._start()
        page_file = self.pages_dir / f"{base}.html"
        page_file.write_text(templates.page_html(scene, ctx), encoding="utf-8")
        page = self._page
        page.goto(page_file.resolve().as_uri(), wait_until="load")
        page.evaluate("document.fonts.ready.then(()=>1)")
        page.evaluate("Promise.all([...document.images].map(i=>i.decode().catch(()=>0)))")
        bad_all: set[str] = set()
        for (reveal, speaker, name, text), path in todo:
            bad = page.evaluate("([r,s,n,t])=>setState(r,s,n,t)", [reveal, speaker, name, text])
            bad_all.update(bad or [])
            tmp = path.with_suffix(".tmp.png")
            page.screenshot(path=str(tmp), type="png")
            os.replace(tmp, path)
            self.rendered += 1
        if bad_all:
            self.warnings.append(f"Segment {seg_pos + 1}, scene {scene_idx + 1} ({scene.layout}): text does not fit and is cut off: "
                                 + "; ".join(sorted(bad_all))[:160])
        return out

    # ------------------------------------------------------------------ cross-fades
    def blend(self, a: Path, b: Path, n: int) -> list[Path]:
        """Cross-fade frame `a` into frame `b` in `n` steps; results are cached by name so repeated builds are fast."""
        key = _h(a.name, b.name, n)
        paths = [self.frames_dir / f"x{key}_{k}.png" for k in range(1, n + 1)]
        if all(p.exists() for p in paths):
            return paths
        with Image.open(a) as xa, Image.open(b) as xb:
            ia, ib = xa.convert("RGB"), xb.convert("RGB")
        if ia.size != ib.size:
            ib = ib.resize(ia.size)
        for k, p in enumerate(paths, 1):
            t = k / (n + 1)
            t = t * t * (3 - 2 * t)  # smoothstep: gentle start and end
            tmp = p.with_suffix(".tmp.png")
            Image.blend(ia, ib, t).save(tmp, "PNG", compress_level=1)
            os.replace(tmp, p)
        return paths


def thumbnail(src: Path, dst: Path, width: int = 360) -> None:
    """Save a small JPEG preview (default width 360 px) of a frame."""
    with Image.open(src) as x:
        im = x.convert("RGB")
    im = im.resize((width, int(im.height * width / im.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.jpg")
    im.save(tmp, "JPEG", quality=82)
    os.replace(tmp, dst)


def render_video_frames(project: Project, cfg: VideoCfg, shots: list[Shot], storyboards: dict[int, SegmentStoryboard],
                        figures: dict[str, Figure], *, cache_dir: Path, project_dir: Path, fonts_dir: Optional[Path] = None,
                        progress: Optional[Callable[[float, str], None]] = None) -> tuple[list[tuple[Path, float]], FrameRenderer, dict]:
    """Returns ([(png, seconds)...], renderer, info). The seconds add up to exactly the end of the last shot."""
    progress = progress or (lambda f, m="": None)
    fr = FrameRenderer(project, cfg, cache_dir=cache_dir, project_dir=project_dir, fonts_dir=fonts_dir)
    try:
        # which distinct states does each scene need? (plus the "everything shown, no caption" state used for thumbnails/poster)
        need: dict[tuple[int, int], list[tuple]] = {}
        for s in shots:
            lst = need.setdefault((s.seg, s.scene), [])
            st = (s.reveal, s.speaker, s.name, s.text)
            if st not in lst:
                lst.append(st)
        finals: dict[tuple[int, int], tuple] = {}
        for (sp, si), lst in need.items():
            sc = storyboards[project.segments[sp].index].scenes[si]
            n = len(sc.rows) if sc.layout == "compare" else len(sc.items)
            fin = (n, "", "", "")
            finals[(sp, si)] = fin
            if fin not in lst:
                lst.append(fin)
        paths: dict[tuple[int, int], dict[tuple, Path]] = {}
        keys = sorted(need)
        for i, (sp, si) in enumerate(keys):
            progress(0.02 + 0.8 * i / max(1, len(keys)), f"Drawing scene {i + 1}/{len(keys)}")
            sb = storyboards[project.segments[sp].index]
            paths[(sp, si)] = fr.render_scene(sp, si, sb.scenes[si], need[(sp, si)], figures)
        # frame list with cross-fades at scene changes
        fps = cfg.fps
        frame_t = 1.0 / fps
        out: list[tuple[Path, float]] = []
        prev: Optional[Shot] = None
        prev_path: Optional[Path] = None
        for k, s in enumerate(shots):
            path = paths[(s.seg, s.scene)][(s.reveal, s.speaker, s.name, s.text)]
            dur = s.dur
            n = 0
            if prev is not None and prev_path is not None and (prev.seg, prev.scene) != (s.seg, s.scene) and cfg.crossfade > 0:
                n = int(round(min(cfg.crossfade, dur * 0.5) * fps))
                n = max(0, min(n, int(dur / frame_t) - 1))
            if n:
                for p in fr.blend(prev_path, path, n):
                    out.append((p, frame_t))
                dur -= n * frame_t
            out.append((path, dur))
            prev, prev_path = s, path
            if k % 25 == 0:
                progress(0.82 + 0.1 * k / max(1, len(shots)), "Preparing frames")
        # merge neighbours that show the same picture
        merged: list[tuple[Path, float]] = []
        for p, d in out:
            if merged and merged[-1][0] == p:
                merged[-1] = (p, merged[-1][1] + d)
            else:
                merged.append((p, d))
        info = {"finals": {k: paths[k][v] for k, v in finals.items()}, "frames": len(merged), "rendered": fr.rendered, "reused": fr.reused,
                "browser": fr.browser_name}
        return merged, fr, info
    finally:
        fr.close()
