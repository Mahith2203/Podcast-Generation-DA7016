"""HTML/CSS for every scene layout. Pages are drawn by a real browser engine (Chromium / Edge), so every Indic script and
right-to-left language is shaped correctly. All sizes are in rem: 1rem = 16px at 720p (the canvas is 80rem x 45rem)."""
from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from typing import Optional

from ..models import Scene

TEMPLATE_VERSION = "t3"

THEMES = {
    "classic": dict(bg1="#f6f8fc", bg2="#e4ebf7", ink="#14213d", ink2="#4b5877", card="#ffffff", line="#d5deee",
                    a="#0f7b78", b="#b72a5e", mark="#f2b705", shadow="rgba(20,33,61,.14)", cap="rgba(20,33,61,.92)", capink="#ffffff"),
    "dark": dict(bg1="#0e1424", bg2="#18223c", ink="#eef2ff", ink2="#a9b6d6", card="#1b2641", line="#2c3a5e",
                 a="#3fc7c0", b="#ff6fa3", mark="#ffd24a", shadow="rgba(0,0,0,.45)", cap="rgba(0,0,0,.78)", capink="#ffffff"),
    "warm": dict(bg1="#fff9ee", bg2="#fbe9cf", ink="#3b2a1a", ink2="#7a6048", card="#fffdf8", line="#ecd9bc",
                 a="#c2570c", b="#7a3e9d", mark="#e0a100", shadow="rgba(92,58,20,.16)", cap="rgba(59,42,26,.92)", capink="#ffffff"),
}

FONT_STACK = ('"Segoe UI","Nirmala UI","Noto Sans","Noto Sans Devanagari","Noto Sans Bengali","Noto Sans Gujarati","Noto Sans Gurmukhi",'
              '"Noto Sans Kannada","Noto Sans Malayalam","Noto Sans Oriya","Noto Sans Tamil","Noto Sans Telugu","Noto Naskh Arabic",'
              '"Noto Nastaliq Urdu","Mangal","Latha","Gautami","Vrinda","Shruti","Kartika","Tahoma","Arial",system-ui,sans-serif')

RTL_SCRIPTS = ("Arabic",)


@dataclass
class Ctx:
    """Everything a slide needs besides its own content: size, theme, language and direction, show name, progress through
    the episode, and whether captions and speaker names are shown.
    """
    width: int
    height: int
    theme: str = "classic"
    lang: str = "en"
    rtl: bool = False
    show: str = ""
    seg_title: str = ""
    seg_index: int = 0
    seg_count: int = 1
    host_names: tuple[str, str] = ("A", "B")
    captions: bool = True
    speakers: bool = True
    figure_uri: Optional[str] = None
    figure_caption: str = ""
    font_css: str = ""
    extra: dict = field(default_factory=dict)


def _e(s: str) -> str:
    return html.escape(s or "", quote=True)


def _css(ctx: Ctx) -> str:
    t = THEMES.get(ctx.theme, THEMES["classic"])
    rem = ctx.height / 45.0
    return f"""
{ctx.font_css}
:root{{--bg1:{t['bg1']};--bg2:{t['bg2']};--ink:{t['ink']};--ink2:{t['ink2']};--card:{t['card']};--line:{t['line']};--a:{t['a']};--b:{t['b']};--mark:{t['mark']};--shadow:{t['shadow']};--cap:{t['cap']};--capink:{t['capink']}}}
*{{box-sizing:border-box;margin:0;padding:0}}
html{{font-size:{rem:.4f}px}}
body{{width:80rem;height:45rem;overflow:hidden;position:relative;color:var(--ink);font-family:{ctx.extra.get('families','')}{FONT_STACK};
 background:radial-gradient(120rem 70rem at 12% -10%,var(--bg2),transparent 60%),linear-gradient(160deg,var(--bg1),var(--bg2));
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}}
.deco{{position:absolute;border-radius:50%;opacity:.16;pointer-events:none}}
.d1{{width:30rem;height:30rem;right:-8rem;top:-9rem;background:var(--a)}}
.d2{{width:20rem;height:20rem;left:-7rem;bottom:-8rem;background:var(--b)}}
.top{{position:absolute;left:3.2rem;right:3.2rem;top:1.7rem;height:2.4rem;display:flex;align-items:center;justify-content:space-between;gap:2rem}}
.brand{{display:flex;align-items:center;gap:.8rem;font-size:1.25rem;font-weight:700;color:var(--ink2);letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:48rem}}
.brand i{{width:1.1rem;height:1.1rem;border-radius:.3rem;background:linear-gradient(135deg,var(--a) 50%,var(--b) 50%);flex:none}}
.dots{{display:flex;gap:.55rem;align-items:center}}
.dots b{{width:.7rem;height:.7rem;border-radius:50%;background:var(--line)}}
.dots b.on{{background:var(--a);width:1.9rem;border-radius:.4rem}}
.dots b.done{{background:var(--ink2);opacity:.55}}
.main{{position:absolute;left:3.2rem;right:3.2rem;top:5.2rem;bottom:{'11.4rem' if ctx.captions else '3rem'};display:flex;flex-direction:column}}
h1.big{{font-size:4.6rem;line-height:1.12;font-weight:800;letter-spacing:-.01em}}
h2.head{{font-size:2.7rem;line-height:1.15;font-weight:800;margin-bottom:1.6rem;flex:none}}
.sub{{font-size:1.9rem;color:var(--ink2);margin-top:1.2rem;font-weight:600}}
.fit{{overflow:hidden}}
.item{{opacity:0;transform:translateY(.6rem);transition:none}}
.item.on{{opacity:1;transform:none}}
.item.cur .lbl,.item.cur .t{{color:var(--ink)}}
/* title */
.title{{justify-content:center;align-items:flex-start;gap:.4rem}}
.title .bar{{width:7rem;height:.6rem;border-radius:.3rem;background:linear-gradient(90deg,var(--a),var(--b));margin-bottom:2rem}}
/* bullets & recap */
.list,.cmp{{--u:calc(1rem*var(--k,1))}}
.list>:first-child,.cmp>:first-child,.kf>:first-child{{margin-top:auto}}
.list>:last-child,.cmp>:last-child,.kf>:last-child{{margin-bottom:auto}}
.list{{display:flex;flex-direction:column;gap:calc(.85*var(--u));flex:1;min-height:0}}
.row{{display:flex;align-items:center;gap:calc(1.3*var(--u));background:var(--card);border:.1rem solid var(--line);border-radius:calc(1.1*var(--u));padding:calc(.75*var(--u)) calc(1.5*var(--u));box-shadow:0 .3rem 1.1rem var(--shadow)}}
.row .n{{flex:none;width:calc(2.7*var(--u));height:calc(2.7*var(--u));border-radius:50%;background:var(--a);color:#fff;display:grid;place-items:center;font-weight:800;font-size:calc(1.4*var(--u))}}
.row:nth-child(even) .n{{background:var(--b)}}
.row .t{{font-size:calc(2*var(--u));line-height:1.25;font-weight:650;color:var(--ink)}}
.row.chk .n{{background:var(--a)}}
/* stepper */
.stepper{{flex:1;display:grid;align-items:center;position:relative;min-height:0}}
.stepper .line{{position:absolute;left:5%;right:5%;top:50%;height:.35rem;background:var(--line);border-radius:.2rem;transform:translateY(-3.2rem)}}
.step{{position:relative;display:flex;flex-direction:column;align-items:center;text-align:center;gap:1.2rem;padding:0 .6rem}}
.step .c{{width:5rem;height:5rem;border-radius:50%;background:var(--a);color:#fff;display:grid;place-items:center;font-size:2.2rem;font-weight:800;box-shadow:0 .4rem 1.2rem var(--shadow);border:.4rem solid var(--bg1)}}
.step:nth-child(odd) .c{{background:var(--a)}}.step:nth-child(even) .c{{background:var(--b)}}
.step .lbl{{font-size:var(--sf,1.85rem);line-height:1.25;font-weight:650;background:var(--card);border:.1rem solid var(--line);border-radius:1rem;padding:.8rem 1rem;box-shadow:0 .3rem 1rem var(--shadow);width:100%}}
/* cycle */
.cyc{{flex:1;position:relative;min-height:0}}
.cyc svg{{position:absolute;inset:0;width:100%;height:100%;overflow:visible}}
.node{{position:absolute;translate:-50% -50%;background:var(--card);border:.2rem solid var(--a);border-radius:1.1rem;padding:.7rem 1.1rem;width:15rem;max-height:8.5rem;overflow:hidden;text-align:center;font-size:1.7rem;font-weight:700;line-height:1.2;box-shadow:0 .3rem 1.1rem var(--shadow)}}
.node:nth-of-type(even){{border-color:var(--b)}}
.node small{{display:block;font-size:1rem;color:var(--ink2);font-weight:800;margin-bottom:.2rem}}
.arc{{fill:none;stroke:var(--ink2);stroke-width:.28rem;stroke-linecap:round;stroke-linejoin:round;opacity:0}}
.arc.ah{{fill:var(--ink2)}}
.arc.on{{opacity:.75}}
.hub{{position:absolute;left:50%;top:50%;translate:-50% -50%;text-align:center;font-size:2.1rem;font-weight:800;color:var(--ink2);width:17rem;line-height:1.2}}
/* definition */
.defn{{justify-content:center;gap:1.6rem}}
.term{{font-size:4.6rem;line-height:1.1;font-weight:800;color:var(--a);border-left:.9rem solid var(--b);padding-left:1.6rem}}
.mean{{font-size:2.45rem;line-height:1.38;font-weight:550;background:var(--card);border:.1rem solid var(--line);border-radius:1.3rem;padding:1.6rem 2rem;box-shadow:0 .3rem 1.3rem var(--shadow);flex:0 1 auto}}
/* compare */
.cmp{{flex:1;display:flex;flex-direction:column;gap:calc(.8*var(--u));min-height:0}}
.cmp .h,.cmp .r{{display:grid;grid-template-columns:1fr 1fr;gap:calc(1*var(--u))}}
.cmp .h div{{background:var(--a);color:#fff;font-weight:800;font-size:calc(2*var(--u));border-radius:calc(.9*var(--u));padding:calc(.8*var(--u)) calc(1.2*var(--u));text-align:center}}
.cmp .h div+div{{background:var(--b)}}
.cmp .r div{{background:var(--card);border:.1rem solid var(--line);border-radius:calc(.9*var(--u));padding:calc(.8*var(--u)) calc(1.2*var(--u));font-size:calc(1.8*var(--u));line-height:1.25;font-weight:600;box-shadow:0 .25rem .9rem var(--shadow)}}
/* figure */
.fig{{flex:1;display:flex;flex-direction:column;align-items:center;gap:1rem;min-height:0}}
.fig .img{{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;width:100%}}
.fig img{{max-width:100%;max-height:100%;object-fit:contain;border-radius:1.1rem;background:#fff;border:.15rem solid var(--line);box-shadow:0 .5rem 1.6rem var(--shadow)}}
.fig .cap{{font-size:1.5rem;color:var(--ink2);font-style:italic;text-align:center;max-width:62rem;line-height:1.3;flex:none}}
/* keyfact */
.kf{{flex:1;display:flex;flex-direction:column;align-items:center;text-align:center;gap:1rem;min-height:0}}
.kf .v{{font-size:11rem;line-height:1;font-weight:900;background:linear-gradient(90deg,var(--a),var(--b));-webkit-background-clip:text;background-clip:text;color:transparent;padding:.4rem 1rem}}
.kf .l{{font-size:2.6rem;font-weight:650;max-width:58rem;line-height:1.25;color:var(--ink)}}
/* captions */
.capbar{{position:absolute;left:3.2rem;right:3.2rem;bottom:1.9rem;min-height:7.2rem;display:flex;align-items:center;gap:1.4rem;background:var(--cap);color:var(--capink);border-radius:1.4rem;padding:1rem 1.8rem;box-shadow:0 .6rem 2rem var(--shadow)}}
.capbar .who{{flex:none;display:flex;align-items:center;gap:.8rem;font-weight:800;font-size:1.35rem;white-space:nowrap}}
.capbar .who i{{width:3rem;height:3rem;border-radius:50%;display:grid;place-items:center;font-style:normal;color:#fff;font-size:1.5rem}}
.capbar .who.A i{{background:var(--a)}}.capbar .who.B i{{background:var(--b)}}
.capbar .txt{{font-size:1.95rem;line-height:1.3;font-weight:560;flex:1;overflow:hidden;max-height:5.3rem}}
.capbar.empty{{visibility:hidden}}
body.rtl{{direction:rtl}}
.foot{{position:absolute;right:3.2rem;bottom:.55rem;font-size:.9rem;color:var(--ink2);opacity:.7}}
"""


def _item_rows(scene: Scene, cls: str = "", chk: bool = False) -> str:
    out = []
    for i, it in enumerate(scene.items):
        mark = "✓" if chk else str(i + 1)
        out.append(f'<div class="row item {cls}{" chk" if chk else ""}" data-i="{i}"><div class="n">{mark}</div><div class="t">{_e(it)}</div></div>')
    return "".join(out)


def _cycle(scene: Scene) -> str:
    n = max(3, len(scene.items))
    items = scene.items[:n]
    cx, cy, rx, ry = 50.0, 50.0, 31.0, 38.0   # percent of the area
    pts = []
    for k in range(len(items)):
        ang = -math.pi / 2 + 2 * math.pi * k / len(items)
        pts.append((cx + rx * math.cos(ang), cy + ry * math.sin(ang), ang))
    nodes = "".join(
        f'<div class="node item fit" data-fit="1" data-i="{k}" style="left:{x:.2f}%;top:{y:.2f}%"><small>{k + 1}</small>{_e(it)}</div>' for k, (it, (x, y, _)) in enumerate(zip(items, pts)))
    return f'<div class="cyc"><svg></svg><div class="hub">{_e(scene.title)}</div>{nodes}</div>'


def scene_body(scene: Scene, ctx: Ctx) -> str:
    """The HTML of one scene's main area for its layout (title, bullets, steps, cycle, definition, compare, figure, key
    fact, recap).
    """
    L = scene.layout
    head = f'<h2 class="head fit" data-fit="1" style="max-height:7rem">{_e(scene.title)}</h2>' if scene.title and L not in ("title", "cycle", "keyfact") else ""
    if L == "title":
        sub = f'<div class="sub">{_e(scene.text)}</div>' if scene.text else ""
        return f'<div class="main title"><div class="bar"></div><h1 class="big fit" data-fit="1" style="max-height:24rem">{_e(scene.title)}</h1>{sub}</div>'
    if L == "bullets":
        return f'<div class="main">{head}<div class="list fit" data-fit="k">{_item_rows(scene)}</div></div>'
    if L == "recap":
        return f'<div class="main">{head}<div class="list fit" data-fit="k">{_item_rows(scene, chk=True)}</div></div>'
    if L == "steps":
        n = max(1, len(scene.items))
        cols = f"repeat({n},minmax(0,1fr))"
        steps = "".join(f'<div class="step item" data-i="{i}"><div class="c">{i + 1}</div><div class="lbl fit" data-fit="1" style="max-height:12rem">{_e(it)}</div></div>'
                        for i, it in enumerate(scene.items))
        sf = "1.85rem" if n <= 3 else ("1.6rem" if n == 4 else "1.4rem")
        return f'<div class="main">{head}<div class="stepper" style="grid-template-columns:{cols};--sf:{sf}"><div class="line"></div>{steps}</div></div>'
    if L == "cycle":
        return f'<div class="main">{_cycle(scene)}</div>'
    if L == "definition":
        return (f'<div class="main defn"><div class="term fit" data-fit="1" style="max-height:12rem">{_e(scene.term or scene.title)}</div>'
                f'<div class="mean fit" data-fit="1" style="max-height:20rem">{_e(scene.text)}</div></div>')
    if L == "compare":
        cols = (scene.columns + ["", ""])[:2]
        rows = "".join(f'<div class="r item" data-i="{i}"><div>{_e(r[0])}</div><div>{_e(r[1])}</div></div>' for i, r in enumerate(scene.rows))
        return f'<div class="main">{head}<div class="cmp fit" data-fit="k"><div class="h"><div>{_e(cols[0])}</div><div>{_e(cols[1])}</div></div>{rows}</div></div>'
    if L == "figure":
        img = f'<img src="{ctx.figure_uri}" alt="">' if ctx.figure_uri else ""
        cap = f'<div class="cap fit" data-fit="1" style="max-height:5rem">{_e(ctx.figure_caption)}</div>' if ctx.figure_caption else ""
        return f'<div class="main">{head}<div class="fig"><div class="img">{img}</div>{cap}</div></div>'
    if L == "keyfact":
        return (f'<div class="main"><div class="kf"><div class="v fit" data-fit="1" style="max-height:14rem">{_e(scene.term)}</div>'
                f'<div class="l fit" data-fit="1" style="max-height:9rem">{_e(scene.text)}</div></div></div>')
    return f'<div class="main">{head}</div>'


SCRIPT = """
function over(el){return el.scrollHeight>el.clientHeight+2||el.scrollWidth>el.clientWidth+2}
function fitAll(){
  const bad=[];
  document.querySelectorAll('.fit').forEach(el=>{
    if(el.dataset.fit==='k'){
      el.style.removeProperty('--k');let k=1,n=0;
      while(over(el)&&k>0.45&&n<40){k*=0.95;el.style.setProperty('--k',k.toFixed(3));n++}
    }else{
      el.style.fontSize='';
      let fs=parseFloat(getComputedStyle(el).fontSize),min=fs*0.5,n=0;
      while(over(el)&&fs>min&&n<40){fs*=0.95;el.style.fontSize=fs+'px';n++}
    }
    if(over(el))bad.push((el.textContent||'').trim().slice(0,40));
  });
  return bad;
}
function drawCycle(){
  const c=document.querySelector('.cyc');if(!c)return;
  const svg=c.querySelector('svg'),W=c.clientWidth,H=c.clientHeight,rem=parseFloat(getComputedStyle(document.documentElement).fontSize);
  svg.setAttribute('viewBox','0 0 '+W+' '+H);svg.innerHTML='';
  const cb=c.getBoundingClientRect(),nodes=[...c.querySelectorAll('.node')],m=nodes.length;
  const R=nodes.map(n=>{const r=n.getBoundingClientRect();return{l:r.left-cb.left,r:r.right-cb.left,t:r.top-cb.top,b:r.bottom-cb.top}});
  const inside=(r,x,y,p)=>x>r.l-p&&x<r.r+p&&y>r.t-p&&y<r.b+p;
  const cx=W/2,cy=H/2,rx=W*0.31,ry=H*0.38,pad=0.5*rem,NS='http://www.w3.org/2000/svg';
  for(let k=0;k<m;k++){
    const a0=-Math.PI/2+2*Math.PI*k/m,a1=a0+2*Math.PI/m,pts=[];
    for(let s=0;s<=80;s++){const a=a0+(a1-a0)*s/80,x=cx+rx*Math.cos(a),y=cy+ry*Math.sin(a);
      if(!inside(R[k],x,y,pad)&&!inside(R[(k+1)%m],x,y,pad))pts.push([x,y]);}
    if(pts.length<3)continue;
    const path=document.createElementNS(NS,'path');path.setAttribute('class','arc');path.dataset.arc=k;
    path.setAttribute('d','M '+pts.map(q=>q[0].toFixed(1)+' '+q[1].toFixed(1)).join(' L '));svg.appendChild(path);
    const e=pts[pts.length-1],f=pts[Math.max(0,pts.length-4)],dx=e[0]-f[0],dy=e[1]-f[1],L=Math.hypot(dx,dy)||1,ux=dx/L,uy=dy/L,sz=1.1*rem;
    const tri=document.createElementNS(NS,'polygon');tri.setAttribute('class','arc ah');tri.dataset.arc=k;
    const bx=e[0]-ux*sz,by=e[1]-uy*sz;
    tri.setAttribute('points',[[e[0]+ux*sz*0.3,e[1]+uy*sz*0.3],[bx-uy*sz*0.55,by+ux*sz*0.55],[bx+uy*sz*0.55,by-ux*sz*0.55]].map(q=>q[0].toFixed(1)+','+q[1].toFixed(1)).join(' '));
    svg.appendChild(tri);
  }
}
function setState(reveal,who,name,text){
  const bar=document.querySelector('.capbar');
  if(bar){
    if(!text){bar.classList.add('empty')}else{
      bar.classList.remove('empty');
      const w=bar.querySelector('.who');
      if(w){w.className='who '+who;w.querySelector('i').textContent=(Array.from((name||who).trim())[0]||'').toUpperCase();w.querySelector('span').textContent=name;w.style.display=name?'flex':'none'}
      bar.querySelector('.txt').textContent=text;
    }
  }
  const bad=fitAll();drawCycle();
  document.querySelectorAll('.item').forEach(el=>{
    const i=parseInt(el.dataset.i);el.classList.toggle('on',i<reveal);el.classList.toggle('cur',i===reveal-1);
  });
  const total=document.querySelectorAll('.node').length;
  document.querySelectorAll('.arc').forEach(el=>{
    const k=parseInt(el.dataset.arc);
    el.classList.toggle('on',(k<total-1&&reveal>k+1)||(k===total-1&&reveal>=total));
  });
  return bad;
}
"""


def page_html(scene: Scene, ctx: Ctx) -> str:
    """A complete HTML page for one scene: its body, the progress strip and the caption bar."""
    dots = "".join(f'<b class="{"on" if i == ctx.seg_index else ("done" if i < ctx.seg_index else "")}"></b>' for i in range(ctx.seg_count))
    cap = ""
    if ctx.captions:
        who = '<div class="who A" style="display:none"><i>A</i><span></span></div>' if ctx.speakers else '<div class="who" style="display:none"><i></i><span></span></div>'
        cap = f'<div class="capbar empty">{who}<div class="txt fit" data-fit="1"></div></div>'
    return (f'<!doctype html><html lang="{_e(ctx.lang)}" dir="{"rtl" if ctx.rtl else "ltr"}"><head><meta charset="utf-8"><style>{_css(ctx)}</style></head>'
            f'<body class="{"rtl" if ctx.rtl else ""}"><div class="deco d1"></div><div class="deco d2"></div>'
            f'<div class="top"><div class="brand"><i></i><span>{_e(ctx.show)}</span></div><div class="dots">{dots}</div></div>'
            f'{scene_body(scene, ctx)}{cap}<script>{SCRIPT}</script></body></html>')
