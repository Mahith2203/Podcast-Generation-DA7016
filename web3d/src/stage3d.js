/* Podcast Studio: the two hosts as stylised 3D characters.
 * Everything is built from primitives in code (no model files, no downloads). Bundled with three.js into one file.
 * The page talks to it through Stage3D.create(canvas, options) -> { set(state), resize(), dispose() }. */
import {
  WebGLRenderer, Scene, PerspectiveCamera, Group, Mesh, SphereGeometry, CapsuleGeometry, CylinderGeometry, TorusGeometry, TubeGeometry,
  QuadraticBezierCurve3, CircleGeometry, MeshStandardMaterial, DirectionalLight, Vector3, PCFSoftShadowMap, NeutralToneMapping,
  SRGBColorSpace, PMREMGenerator, DoubleSide,
} from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const damp = (cur, target, rate, dt) => cur + (target - cur) * (1 - Math.exp(-rate * dt));
const HAIR = '#241b2b';

export function supported() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch (_) { return false; }
}

/* ---- the head is an ellipsoid; features are placed on its surface ---- */
const HEAD = { rx: 0.5, ry: 0.58, rz: 0.52, cy: 0.55 };
const FRONT = new Vector3(0, 0, 1);
function facePoint(x, y, push = 0) {
  const { rx, ry, rz, cy } = HEAD;
  const u = x / rx, v = (y - cy) / ry;
  const z = rz * Math.sqrt(Math.max(0, 1 - u * u - v * v));
  const n = new Vector3(x / (rx * rx), (y - cy) / (ry * ry), z / (rz * rz)).normalize();
  return { p: new Vector3(x, y, z).addScaledVector(n, push), n };
}
function place(obj, x, y, push = 0) {
  const { p, n } = facePoint(x, y, push);
  obj.position.copy(p);
  obj.quaternion.setFromUnitVectors(FRONT, n);
  return obj;
}

const UNIT = new SphereGeometry(1, 40, 28);

function materials(cfg) {
  const std = (color, rough = 0.6, extra = {}) => new MeshStandardMaterial({ color, roughness: rough, metalness: 0, ...extra });
  return {
    skin: std(cfg.skin, 0.62, { emissive: cfg.skin, emissiveIntensity: 0.07 }),
    shade: std(cfg.shade, 0.65),
    hair: std(HAIR, 0.38, { side: DoubleSide }),
    jacket: std(cfg.jacket, 0.7),
    jacketDark: std(cfg.jacketDark, 0.7),
    shirt: std('#f6f1e7', 0.8),
    white: std('#fbfbf8', 0.35),
    iris: std('#1b1722', 0.25),
    spark: std('#ffffff', 0.2, { emissive: '#ffffff', emissiveIntensity: 0.8 }),
    lip: std('#a8454e', 0.5, { transparent: true }),
    lipOpen: std('#b24f58', 0.5, { transparent: true }),
    cavity: std('#4a1220', 0.9, { transparent: true }),
    teeth: std('#fffaf2', 0.4, { transparent: true }),
    tongue: std('#d9576b', 0.6, { transparent: true }),
    blush: std('#e0707a', 1, { transparent: true, opacity: 0.3, depthWrite: false }),
    gold: std('#f2a900', 0.25, { metalness: 0.85 }),
    frame: std('#1b2438', 0.35),
    lens: std('#cfe6ff', 0.05, { transparent: true, opacity: 0.1, depthWrite: false }),
  };
}

function cylBetween(a, b, r, mat) {
  const d = new Vector3().subVectors(b, a);
  const m = new Mesh(new CylinderGeometry(r, r, d.length(), 8), mat);
  m.position.copy(a).addScaledVector(d, 0.5);
  m.quaternion.setFromUnitVectors(new Vector3(0, 1, 0), d.normalize());
  return m;
}

function buildHost(k, cfg) {
  const m = materials(cfg);
  const toward = k === 'A' ? 1 : -1;               // +1: turns toward +x (A stands on the left)
  const female = cfg.gender !== 'male';
  const root = new Group();
  const shadowed = (mesh) => { mesh.castShadow = true; mesh.receiveShadow = true; return mesh; };
  const ell = (geo, mat, sx, sy, sz, x = 0, y = 0, z = 0) => {
    const me = new Mesh(geo, mat); me.scale.set(sx, sy, sz); me.position.set(x, y, z); return shadowed(me);
  };

  /* torso */
  const figure = new Group(); figure.scale.setScalar(0.86); root.add(figure);
  const body = new Group(); figure.add(body);
  body.add(ell(UNIT, m.jacket, 0.78, 0.66, 0.42, 0, 0.42, 0));
  [-1, 1].forEach((s) => body.add(ell(UNIT, m.jacket, 0.23, 0.23, 0.23, s * 0.68, 0.8, 0)));
  body.add(ell(UNIT, m.shirt, 0.3, 0.42, 0.3, 0, 0.8, 0.2));
  [-1, 1].forEach((s) => { const l = ell(UNIT, m.jacketDark, 0.13, 0.42, 0.1, s * 0.27, 0.72, 0.34); l.rotation.z = -s * 0.38; body.add(l); });
  const neck = shadowed(new Mesh(new CylinderGeometry(0.17, 0.2, 0.4, 24), m.shade)); neck.position.set(0, 1.2, 0.02); figure.add(neck);

  /* arms: only the speaker raises a hand, so the pose is two angles per arm */
  const upper = new CapsuleGeometry(0.13, 0.46, 6, 16), fore = new CapsuleGeometry(0.11, 0.36, 6, 16);
  const arms = [-1, 1].map((s) => {
    const sh = new Group(); sh.position.set(s * 0.72, 0.82, 0.02); figure.add(sh);
    sh.add(ell(upper, m.jacket, 1, 1, 1, 0, -0.32, 0));
    const el = new Group(); el.position.set(0, -0.6, 0); sh.add(el);
    el.add(ell(fore, m.jacket, 1, 1, 1, 0, -0.31, 0));
    el.add(ell(UNIT, m.skin, 0.11, 0.135, 0.09, 0, -0.67, 0.01));
    return { sh, el, s };
  });

  /* head */
  const head = new Group(); head.position.set(0, 1.135, 0.02); head.scale.setScalar(1.16); root.add(head);
  head.add(ell(UNIT, m.skin, HEAD.rx, HEAD.ry, HEAD.rz, 0, HEAD.cy, 0));
  [-1, 1].forEach((s) => head.add(ell(UNIT, m.skin, 0.05, 0.1, 0.08, s * 0.5, 0.52, -0.02)));
  const nose = ell(UNIT, m.shade, 0.055, 0.07, 0.06); place(nose, 0, 0.46, 0.03); head.add(nose);

  const eyes = [-1, 1].map((s) => {
    const base = facePoint(s * 0.18, 0.62, -0.035).p;
    const ball = new Group(); ball.position.copy(base); head.add(ball);
    ball.add(ell(UNIT, m.white, 0.088, 0.088, 0.088));
    const iris = ell(UNIT, m.iris, 0.05, 0.05, 0.03, 0, 0, 0.074); ball.add(iris);
    ball.add(ell(UNIT, m.spark, 0.012, 0.012, 0.01, 0.018, 0.022, 0.093));
    const lid = new Group(); lid.position.copy(base); head.add(lid);
    const dome = new Mesh(new SphereGeometry(0.1, 24, 12, 0, Math.PI * 2, 0, Math.PI / 2), m.skin); shadowed(dome); lid.add(dome);
    return { ball, lid, s };
  });
  const brows = [-1, 1].map((s) => {
    const g = new Group(); place(g, s * 0.19, 0.8, 0.02); head.add(g);
    const b = new Mesh(new CapsuleGeometry(0.02, 0.15, 4, 10).rotateZ(Math.PI / 2), m.hair); g.add(b);
    return { b, s };
  });
  [-1, 1].forEach((s) => { const c = new Mesh(new CircleGeometry(0.075, 20), m.blush); place(c, s * 0.3, 0.38, 0.006); head.add(c); });

  /* mouth: a smile curve when closed, lips + cavity + teeth + tongue when open */
  const mouth = new Group(); place(mouth, 0, 0.28, 0.004); mouth.scale.setScalar(1.2); head.add(mouth);
  const smile = new Mesh(new TubeGeometry(new QuadraticBezierCurve3(new Vector3(-0.12, 0.016, -0.012), new Vector3(0, -0.062, 0.006), new Vector3(0.12, 0.016, -0.012)), 24, 0.0135, 8), m.lip);
  mouth.add(smile);
  const open = new Group(); mouth.add(open);
  const cavity = ell(UNIT, m.cavity, 0.115, 1, 0.03, 0, 0, 0.004);
  const teeth = ell(UNIT, m.teeth, 0.086, 0.017, 0.02, 0, -0.014, 0.012);
  const tongue = ell(UNIT, m.tongue, 0.06, 0.024, 0.02, 0, 0, 0.012);
  const lipU = new Mesh(new CapsuleGeometry(0.02, 0.17, 4, 10).rotateZ(Math.PI / 2), m.lipOpen);
  const lipL = new Mesh(new CapsuleGeometry(0.022, 0.13, 4, 10).rotateZ(Math.PI / 2), m.lipOpen);
  lipU.position.z = lipL.position.z = 0.016;
  open.add(cavity, teeth, tongue, lipU, lipL);
  const openMats = [m.cavity, m.teeth, m.tongue, m.lipOpen];

  /* hair, glasses, earrings */
  const hairScale = [0.545, 0.625, 0.575], hairC = [0, 0.57, -0.015];
  const shell = (phiStart, phiLen, th0, th1) => {
    const me = new Mesh(new SphereGeometry(1, 40, 28, phiStart, phiLen, th0, th1 - th0), m.hair);
    me.scale.set(...hairScale); me.position.set(...hairC); return shadowed(me);
  };
  const wrap = (w, th0, th1) => shell(Math.PI / 2 + w, Math.PI * 2 - 2 * w, th0, th1);
  if (female) {
    head.add(shell(0, Math.PI * 2, 0, 1.02), wrap(0.95, 0.4, 2.1));
    head.add(ell(UNIT, m.hair, 0.46, 0.72, 0.3, 0, -0.1, -0.3));
    [-1, 1].forEach((s) => { const l = ell(new CapsuleGeometry(0.085, 0.55, 6, 14), m.hair, 1, 1, 1, s * 0.47, 0.12, 0.1); l.rotation.z = s * 0.05; head.add(l); });
    head.add(Object.assign(shell(Math.PI / 2 + 0.05, 1.5, 0.95, 1.3), {}));   // a side-swept fringe: lower hairline on one side
    [-1, 1].forEach((s) => head.add(ell(UNIT, m.gold, 0.036, 0.036, 0.036, s * 0.5, 0.36, -0.01)));
  } else {
    head.add(shell(0, Math.PI * 2, 0, 1.08), wrap(1.15, 0.5, 1.5));
    const q = ell(UNIT, m.hair, 0.34, 0.12, 0.3, 0.05, 0.99, 0.1); q.rotation.z = -0.15; head.add(q);
    [-1, 1].forEach((s) => {
      const g = new Group(); place(g, s * 0.18, 0.62, 0.075); head.add(g);
      const lens = new Mesh(new CircleGeometry(0.112, 24), m.lens); lens.position.z = 0.002;
      g.add(new Mesh(new TorusGeometry(0.118, 0.011, 10, 36), m.frame), lens);
      head.add(cylBetween(facePoint(s * 0.3, 0.64, 0.06).p, facePoint(s * 0.5, 0.58, 0.0).p, 0.009, m.frame));
    });
    head.add(cylBetween(new Vector3(-0.07, 0.64, facePoint(0, 0.64, 0.08).p.z), new Vector3(0.07, 0.64, facePoint(0, 0.64, 0.08).p.z), 0.009, m.frame));
  }

  /* per-frame state kept between updates */
  const st = { open: 0, blink: 0, nextBlink: 1 + Math.random() * 2, blinkT: -1, raise: 0, tilt: 0, smile: 0.45, wide: 0, gest: 0, lvl: 0,
    nodT: -1, nodAmp: 0, nextNod: 2 + Math.random() * 2, glanceUntil: 0, nextGlance: 4 + Math.random() * 3, gazeX: 0, gazeY: 0, yaw: 0, pitch: 0, roll: 0, jx: 0 };

  function update(p, t, dt) {
    const { speaking, ex, reacting, mode, reduced, mo } = p;
    const listening = p.partnerSpeaking && !speaking;

    /* mouth */
    st.open = reduced ? mo : damp(st.open, mo, 45, dt);
    const a = clamp(st.open);
    const h = 0.006 + 0.082 * a;
    const vis = clamp(a * 9);
    openMats.forEach((mt) => { mt.opacity = vis; });
    open.visible = vis > 0.01;
    smile.material.opacity = 1 - vis; smile.visible = vis < 0.99;
    cavity.scale.y = h / 2; cavity.position.y = -h / 2;
    lipU.position.y = 0.002; lipL.position.y = -h;
    tongue.position.y = -h + 0.022; tongue.scale.set(0.06 * (0.8 + 0.2 * a), 0.024 * clamp(a * 2.5), 0.02);
    teeth.visible = a > 0.12; open.scale.x = 1 - 0.13 * a;

    /* expression targets */
    let raise = 0, tilt = 0, smileT = 0.5, wide = 0, roll = 0;
    if (ex.includes('ask')) { raise = 0.55; roll = 0.07; }
    if (ex.includes('joy')) { smileT = 1.15; raise = 0.2; }
    if (ex.includes('surprise')) { raise = 1; wide = 0.7; smileT = 0.2; }
    if (ex.includes('anger')) { tilt = 1; raise = -0.2; smileT = 0.1; }
    if (ex.includes('sad')) { tilt = -1; smileT = -0.7; raise = 0.1; }
    if (reacting) { smileT = Math.max(smileT, 0.95); raise += 0.15; }
    raise += 0.35 * a;                                 // eyebrows add emphasis on loud syllables
    const r = reduced ? 1 : 1 - Math.exp(-9 * dt);
    st.raise += (raise - st.raise) * r; st.tilt += (tilt - st.tilt) * r; st.smile += (smileT - st.smile) * r; st.wide += (wide - st.wide) * r;
    smile.scale.y = st.smile;
    smile.position.y = -0.004 * st.smile;
    brows.forEach(({ b, s }) => { b.position.y = st.raise * 0.045; b.rotation.z = s * 0.38 * st.tilt - s * 0.08 * clamp(st.raise, 0, 1); });

    /* blinking */
    if (!reduced) {
      if (st.blinkT < 0 && t > st.nextBlink) st.blinkT = 0;
      if (st.blinkT >= 0) {
        st.blinkT += dt;
        st.blink = Math.sin(clamp(st.blinkT / 0.17) * Math.PI);
        if (st.blinkT > 0.17) { st.blinkT = -1; st.blink = 0; st.nextBlink = t + 2.2 + Math.random() * 3.6; }
      }
    }
    const restLid = -0.42 - st.wide * 0.34 + (listening ? 0.05 : 0);
    eyes.forEach(({ lid }) => { lid.rotation.x = lerp(restLid, 0.95, st.blink); });

    /* where heads and eyes point */
    let yawT = toward * (0.1 + (listening ? 0.2 : speaking ? 0.08 : 0.12));
    let pitchT = 0.02 + 0.07 * a, rollT = roll * toward, eyeYaw = toward * 0.42, eyePitch = 0;
    if (mode === 'think') { pitchT = -0.12; yawT = toward * -0.05; rollT = toward * 0.1 * Math.sin(t * 0.7); eyeYaw = toward * (-0.1); eyePitch = -0.32; }
    else if (!reduced) {
      if (speaking && t > st.nextGlance) { st.glanceUntil = t + 0.9; st.nextGlance = t + 5 + Math.random() * 4; }
      if (t < st.glanceUntil) eyeYaw = -toward * 0.05;        // a quick look at the viewer
      yawT += 0.03 * Math.sin(t * 0.6 + toward);
      pitchT += 0.015 * Math.sin(t * 0.9 + toward * 2);
      rollT += 0.02 * Math.sin(t * 0.5 + toward);
      if (speaking) { pitchT += 0.03 * Math.sin(t * 5.2) * a; rollT += 0.02 * Math.sin(t * 2.3 + toward); }
      // listeners nod now and then; a "mm-hm" reaction nods twice, quicker
      if (listening && st.nodT < 0 && t > st.nextNod) { st.nodT = 0; st.nodAmp = 0.12; st.nextNod = t + 2.6 + Math.random() * 2.6; }
      if (reacting && st.nodT < 0) { st.nodT = 0; st.nodAmp = 0.2; }
      if (st.nodT >= 0) {
        st.nodT += dt;
        const dur = st.nodAmp > 0.15 ? 0.7 : 0.6;
        pitchT += st.nodAmp * Math.sin(clamp(st.nodT / dur) * Math.PI * (st.nodAmp > 0.15 ? 4 : 2)) * (1 - st.nodT / dur * 0.3);
        if (st.nodT > dur) st.nodT = -1;
      }
    }
    const rh = reduced ? 1 : 1 - Math.exp(-6 * dt);
    st.yaw += (yawT - st.yaw) * rh; st.pitch += (pitchT - st.pitch) * (reduced ? 1 : 1 - Math.exp(-12 * dt)); st.roll += (rollT - st.roll) * rh;
    head.rotation.set(st.pitch, st.yaw, st.roll);
    head.position.y = 1.135 - 0.012 * a;
    st.gazeX += (eyeYaw - st.gazeX) * (reduced ? 1 : 1 - Math.exp(-14 * dt));
    st.gazeY += (eyePitch - st.gazeY) * (reduced ? 1 : 1 - Math.exp(-10 * dt));
    eyes.forEach(({ ball }) => ball.rotation.set(st.gazeY, st.gazeX, 0));

    /* breathing and hand gestures */
    const br = reduced ? 0 : Math.sin(t * 1.5 + toward) * 0.012;
    body.scale.set(1 + br * 0.6, 1 + br, 1 + br * 0.6);
    body.rotation.y = reduced ? 0 : toward * 0.035 * Math.sin(t * 0.45);
    st.lvl = damp(st.lvl, a, 6, dt);
    const gTarget = reduced ? 0 : speaking ? 0.3 + 0.7 * clamp(st.lvl * 1.6) : 0;
    st.gest = damp(st.gest, gTarget, speaking ? 2.2 : 3.5, dt);
    arms.forEach(({ sh, el, s }, i) => {
      const g = st.gest * (i === (k === 'A' ? 1 : 0) ? 1 : 0.55);        // the arm nearer the partner moves most
      const wob = Math.sin(t * 3.1 + i * 1.7 + toward) * 0.22 * g;
      sh.rotation.set(-0.12 - 0.46 * g, 0, s * 0.08);
      const psi = lerp(-0.35, -1.85 + wob, g);
      el.rotation.x = psi - sh.rotation.x;
    });
  }
  return { root, update, toward };
}

export function create(canvas, opts = {}) {
  const hostsCfg = opts.hosts;
  const renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.toneMapping = NeutralToneMapping;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = PCFSoftShadowMap;
  renderer.setClearColor(0x000000, 0);

  const scene = new Scene();
  const pmrem = new PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.62;
  const camera = new PerspectiveCamera(28, 3, 0.1, 60);

  const key = new DirectionalLight('#fff1dc', 2.1); key.position.set(-3, 5, 5.5); key.target.position.set(0, 1.3, 0);
  key.castShadow = true; key.shadow.mapSize.set(1024, 1024); key.shadow.bias = -0.0006; key.shadow.normalBias = 0.035; key.shadow.radius = 4;
  Object.assign(key.shadow.camera, { left: -5, right: 5, top: 4, bottom: -1.5, near: 1, far: 20 });
  const fill = new DirectionalLight('#cfe0ff', 0.55); fill.position.set(4, 2, 4);
  const rim = { A: new DirectionalLight(hostsCfg.A.rim, 1.0), B: new DirectionalLight(hostsCfg.B.rim, 1.0) };
  rim.A.position.set(-4, 3.5, -3); rim.B.position.set(4, 3.5, -3);
  scene.add(key, key.target, fill, rim.A, rim.B);

  const chars = { A: buildHost('A', hostsCfg.A), B: buildHost('B', hostsCfg.B) };
  scene.add(chars.A.root, chars.B.root);

  const state = { mode: 'play', live: false, speaker: null, mo: { A: 0, B: 0 }, ex: { A: [], B: [] }, reacting: { A: false, B: false }, reduced: false, dock: false };
  let visible = true, alive = true, raf = 0, last = 0, camX = 0, speakerMix = { A: 0, B: 0 }, dist = 6, X = 2, W = 1, H = 1, lastDraw = 0;
  const dockCtx = opts.dockCanvas ? opts.dockCanvas.getContext('2d') : null;

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = Math.max(1, canvas.clientWidth); H = Math.max(1, canvas.clientHeight);
    renderer.setPixelRatio(dpr); renderer.setSize(W, H, false);
    camera.aspect = W / H; camera.updateProjectionMatrix();
    const tanH = Math.tan(camera.fov * Math.PI / 360);
    const halfW0 = camera.aspect * tanH * 6;
    X = clamp(halfW0 * 0.4, 1.3, 2.3);
    dist = Math.max(6, (X + 1.15) / (camera.aspect * tanH));
    chars.A.root.position.set(-X, 0, 0); chars.B.root.position.set(X, 0, 0);
    chars.A.root.rotation.y = 0.58; chars.B.root.rotation.y = -0.58;
    placeCamera(0, 0);
    if (opts.onLayout) {
      const toPx = (wx, wy) => { const v = new Vector3(wx, wy, 0).project(camera); return { x: (v.x + 1) / 2 * W, y: (1 - v.y) / 2 * H }; };
      opts.onLayout({ A: { ...toPx(-X - 0.3, 2.6), cx: toPx(-X, 0).x }, B: { ...toPx(X + 0.3, 2.6), cx: toPx(X, 0).x } });
    }
  }
  function placeCamera(dx, t) {
    camera.position.set(dx * 0.7 + Math.sin(t * 0.17) * 0.12, 1.38, dist);
    camera.lookAt(dx * 0.5, 1.3, 0);
    camera.updateMatrixWorld(true);
  }

  let dockTick = 0;
  function drawDock() {
    if (!dockCtx || !state.dock || (++dockTick & 1)) return;   // the small copy only needs ~30 fps
    const dc = opts.dockCanvas, dw = dc.width, dh = dc.height;
    if (!dw || !dh) return;
    const pxPerUnit = canvas.height / (2 * dist * Math.tan(camera.fov * Math.PI / 360));
    const cw = (2 * X + 2.9) * pxPerUnit, ch = cw * dh / dw;
    const c = new Vector3(0, 1.85, 0).project(camera);
    const cx = (c.x + 1) / 2 * canvas.width, cy = (1 - c.y) / 2 * canvas.height;
    dockCtx.clearRect(0, 0, dw, dh);
    dockCtx.drawImage(canvas, cx - cw / 2, cy - ch / 2, cw, ch, 0, 0, dw, dh);
  }

  function frame(ts) {
    raf = 0;
    if (!alive) return;
    const t = ts / 1000;
    const idle = !state.live && state.mode === 'play';
    const need = (visible || state.dock) && !document.hidden;
    if (need && !(idle && ts - lastDraw < 33)) {          // idle breathing only needs ~30 fps
      lastDraw = ts;
      const dt = Math.min(0.05, last ? t - last : 0.016); last = t;
      const sp = state.speaker;
      for (const k of ['A', 'B']) speakerMix[k] = damp(speakerMix[k], sp === k ? 1 : 0, 6, dt);
      const partner = { A: 'B', B: 'A' };
      for (const k of ['A', 'B']) {
        chars[k].update({ speaking: sp === k, partnerSpeaking: sp === partner[k], ex: state.ex[k], reacting: state.reacting[k], mode: state.mode, reduced: state.reduced, mo: state.mo[k] }, t, dt);
        rim[k].intensity = 0.9 + 1.2 * speakerMix[k];
      }
      const dxT = state.reduced ? 0 : (speakerMix.B - speakerMix.A) * 0.55;
      camX = damp(camX, dxT, 2.2, dt);
      placeCamera(camX, state.reduced ? 0 : t);
      renderer.render(scene, camera);
      drawDock();
    } else if (!need) last = 0;
    raf = requestAnimationFrame(frame);
  }

  const ro = new ResizeObserver(resize); ro.observe(canvas);
  const io = 'IntersectionObserver' in window ? new IntersectionObserver(([e]) => { visible = e.isIntersecting; }) : null;
  io?.observe(canvas);
  resize();
  raf = requestAnimationFrame(frame);

  return {
    set(p) {
      for (const key2 of Object.keys(p)) {
        if (p[key2] && typeof p[key2] === 'object' && !Array.isArray(p[key2])) Object.assign(state[key2], p[key2]); else state[key2] = p[key2];
      }
    },
    resize,
    dispose() {
      alive = false; cancelAnimationFrame(raf); ro.disconnect(); io?.disconnect();
      scene.traverse((o) => { o.geometry?.dispose?.(); });
      renderer.dispose(); pmrem.dispose(); renderer.forceContextLoss();
    },
  };
}
