// 3D-вид горизонта ВЖР на Three.js (библиотека лежит в static/three, внешних ресурсов нет).
// Сцена строится из тех же двойников Ditto, что и 2D-план: атрибуты дают геометрию,
// features — состояние (вердикты камер, SF целиков, струи, люди, техника).
// Сцена намеренно лёгкая: коробки, цилиндры и немного частиц, чтобы работать без GPU.

import * as THREE from 'three';
import { OrbitControls } from './three/OrbitControls.js';

const COL = {
  rock: 0x151b22, floor: 0x1c2430, airway: 0x3a4a5e, blocked: 0x5a1515,
  allowed: 0x22c55e, restricted: 0xf59e0b, forbidden: 0xef4444, mined: 0x3b4654, virgin: 0x202a36,
  sfOk: 0x22c55e, sfWatch: 0xf59e0b, sfBad: 0xef4444, extracted: 0x0b0f14,
  fresh: 0x38bdf8, gasWarn: 0xf59e0b, gasBad: 0xef4444, still: 0x4b5563,
  crewIn: 0x22c55e, crewOut: 0xef4444, crewMove: 0x38bdf8, crewIdle: 0xf59e0b, crewSurface: 0x475569,
  lhd: 0xeab308, jumbo: 0xa78bfa, shaftFresh: 0x22c55e, shaftExhaust: 0xf97316, orePass: 0xf59e0b,
};

const LAYER_H = 4.5;

export function createView3D(container, hooks) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0f1419);
  scene.fog = new THREE.Fog(0x0f1419, 900, 2200);
  const camera = new THREE.PerspectiveCamera(45, 1, 1, 5000);
  camera.position.set(620, 520, 900);
  const renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: 'low-power' });
  renderer.setPixelRatio(1);
  container.appendChild(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(490, 0, 260);
  controls.maxPolarAngle = Math.PI / 2.05;
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;

  scene.add(new THREE.AmbientLight(0xffffff, 1.0));
  const sun = new THREE.DirectionalLight(0xffffff, 1.1);
  sun.position.set(300, 600, 200);
  scene.add(sun);

  // плита горизонта
  const floor = new THREE.Mesh(new THREE.BoxGeometry(1100, 6, 620), new THREE.MeshLambertMaterial({ color: COL.floor }));
  floor.position.set(490, -3, 260);
  scene.add(floor);
  const grid = new THREE.GridHelper(1100, 22, 0x2a3441, 0x1f2833);
  grid.position.set(490, 0.2, 260);
  scene.add(grid);

  const staticGroup = new THREE.Group();   // перестраивается при смене сигнатуры
  const liveGroup = new THREE.Group();     // люди и техника (анимация)
  const flowGroup = new THREE.Group();     // частицы струи
  scene.add(staticGroup, liveGroup, flowGroup);

  const pickables = [];                    // [{mesh, id}]
  const raycaster = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
  let hoverId = null;

  const labelSprite = (text, size = 13, color = '#e6edf3') => {
    const c = document.createElement('canvas');
    const ctx = c.getContext('2d');
    ctx.font = `600 ${size * 2}px -apple-system, Segoe UI, Roboto, Arial`;
    const w = Math.ceil(ctx.measureText(text).width) + 16, h = size * 2 + 12;
    c.width = w; c.height = h;
    ctx.font = `600 ${size * 2}px -apple-system, Segoe UI, Roboto, Arial`;
    ctx.fillStyle = 'rgba(11,15,20,0.7)'; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = color; ctx.textBaseline = 'middle'; ctx.fillText(text, 8, h / 2);
    const tex = new THREE.CanvasTexture(c);
    tex.minFilter = THREE.LinearFilter;
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
    sp.scale.set(w / 2.6, h / 2.6, 1);
    return sp;
  };

  const box = (w, h, d, color, opacity = 1) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshLambertMaterial({ color, transparent: opacity < 1, opacity }));
    return m;
  };

  let staticSig = '';
  let airways = [];        // для частиц: {a:[x,z], b:[x,z], q, v, dir, color}
  let norms = {};
  let twins = {};

  const airColor = (f) => {
    if (f.status === 'blocked') return COL.blocked;
    if (f.co > (norms.co_limit_pct || 0.0016) || f.no2 > (norms.no2_limit_pct || 0.00026)) return COL.gasBad;
    if (f.co > (norms.co_limit_pct || 0.0016) * 0.5) return COL.gasWarn;
    if (f.q < 1) return COL.still;
    return COL.fresh;
  };
  const sfColor = (f) => f.extracted ? COL.extracted : f.sf >= (norms.sf_ok || 1.5) ? COL.sfOk : f.sf >= (norms.sf_watch || 1.3) ? COL.sfWatch : COL.sfBad;
  const verdictColor = { 'разрешено': COL.allowed, 'ограничено': COL.restricted, 'запрещено': COL.forbidden };

  function rebuildStatic(byKind) {
    staticGroup.clear();
    pickables.length = 0;
    airways = [];
    // выработки
    (byKind.airway || []).forEach(id => {
      const t = twins[id], f = t.f, [a, b] = t.a.polyline;
      const dx = b[0] - a[0], dz = b[1] - a[1], len = Math.hypot(dx, dz) || 1;
      const side = Math.sqrt(t.a.area_m2) * 1.6;
      const m = box(side, side * 0.9, len, f.status === 'blocked' ? COL.blocked : COL.airway, 0.92);
      m.position.set((a[0] + b[0]) / 2, side * 0.45, (a[1] + b[1]) / 2);
      m.rotation.y = -Math.atan2(dz, dx) + Math.PI / 2;
      staticGroup.add(m); pickables.push({ mesh: m, id });
      // светящаяся струя по оси выработки
      if (f.status !== 'blocked' && f.q >= 0.5) {
        const core = box(side * 0.35, side * 0.35, len, airColor(f));
        core.material.emissive = new THREE.Color(airColor(f)); core.material.emissiveIntensity = 0.6;
        core.position.copy(m.position); core.rotation.copy(m.rotation);
        staticGroup.add(core);
      }
      if (t.a.door) {
        const closed = (t.a.door === 'door' && f.door_state === 'закрыта') || (t.a.door === 'regulator' && f.door_state === 'закрыт');
        const d = box(side * 1.3, side, 1.2, closed ? COL.forbidden : (t.a.door === 'door' ? COL.allowed : COL.fresh));
        d.position.copy(m.position); d.rotation.copy(m.rotation);
        staticGroup.add(d);
      }
      airways.push({ a, b, q: f.q, v: f.velocity, dir: f.direction >= 0 ? 1 : -1, color: airColor(f), y: side * 0.45, blocked: f.status === 'blocked' });
    });
    // стволы и ВГП
    (byKind.shaft || []).forEach(id => {
      const t = twins[id], ex = t.f.mode === 'исходящая';
      const c = new THREE.Mesh(new THREE.CylinderGeometry(7, 7, 120, 16, 1, true), new THREE.MeshLambertMaterial({ color: ex ? COL.shaftExhaust : COL.shaftFresh, transparent: true, opacity: 0.55, side: THREE.DoubleSide }));
      c.position.set(t.a.x, 60, t.a.y);
      staticGroup.add(c); pickables.push({ mesh: c, id });
      const l = labelSprite(`${t.a.name.split(' (')[0]} · ${Math.round(t.f.q)} м³/с`, 13, ex ? '#fdba74' : '#86efac');
      l.position.set(t.a.x, 132, t.a.y); staticGroup.add(l);
    });
    const fan = twins[`${ns()}:fan-main`];
    if (fan) {
      const on = fan.f.state === 'работает' && fan.f.speed_pct > 0;
      const fb = box(26, 26, 26, on ? 0x14532d : 0x7f1d1d);
      fb.position.set(fan.a.x + 40, 13, fan.a.y); staticGroup.add(fb); pickables.push({ mesh: fb, id: `${ns()}:fan-main` });
      const l = labelSprite(`ВГП ${fan.f.speed_pct} % · ${Math.round(fan.f.q)} м³/с · ${Math.round(fan.f.pressure)} Па`, 12, on ? '#86efac' : '#fca5a5');
      l.position.set(fan.a.x + 40, 40, fan.a.y); staticGroup.add(l);
    }
    // дробилка, рудоспуски, датчики
    (byKind.crusher || []).forEach(id => {
      const t = twins[id];
      const m = box(44, 18, 26, 0x334155); m.position.set(t.a.x, 9, t.a.y); staticGroup.add(m); pickables.push({ mesh: m, id });
      const l = labelSprite(`дробилка ${Math.round(t.f.throughput)} т/ч`, 11); l.position.set(t.a.x, 26, t.a.y); staticGroup.add(l);
    });
    (byKind['ore-pass'] || []).forEach(id => {
      const t = twins[id];
      const c = new THREE.Mesh(new THREE.CylinderGeometry(5, 5, 16, 12), new THREE.MeshLambertMaterial({ color: COL.orePass, emissive: COL.orePass, emissiveIntensity: Math.min(1, t.f.level / (t.a.capacity_t || 1500)) }));
      c.position.set(t.a.x, 8, t.a.y); staticGroup.add(c); pickables.push({ mesh: c, id });
    });
    (byKind.sensor || []).forEach(id => {
      const t = twins[id], bad = t.f.alarm !== 'норма';
      const m = box(6, 6, 6, bad ? COL.forbidden : COL.allowed); m.position.set(t.a.x, 14, t.a.y); staticGroup.add(m); pickables.push({ mesh: m, id });
      if (bad) { const l = labelSprite(`АГК: ${t.f.alarm}`, 11, '#fca5a5'); l.position.set(t.a.x, 24, t.a.y); staticGroup.add(l); }
    });
    // панели, камеры, целики
    (byKind.panel || []).forEach(id => {
      const t = twins[id], a = t.a, f = t.f;
      const w = a.cols * a.grid_m + 12, d = a.rows * a.grid_m + 12;
      const evac = f.status === 'эвакуация';
      const plate = box(w, 1, d, evac ? 0x3b0f0f : 0x111820);
      plate.position.set(a.origin_x + w / 2 - 6, 0.6, a.origin_y + d / 2 - 6); staticGroup.add(plate); pickables.push({ mesh: plate, id });
      const l = labelSprite(`${a.name}: ${Math.round(f.q_in)} м³/с · ${f.people} чел · сейсмика ${f.seismic_class} ${f.hazard}${evac ? ' · ЭВАКУАЦИЯ' : ''}`, 12, evac ? '#fca5a5' : '#e6edf3');
      l.position.set(a.origin_x + w / 2 - 6, 34, a.origin_y - 10); staticGroup.add(l);
    });
    (byKind.chamber || []).forEach(id => {
      const t = twins[id], a = t.a, f = t.f;
      const layers = Math.max(1, f.layer + ((f.state !== 'virgin' && f.state !== 'mined_out' && f.state !== 'idle') ? 1 : 0));
      const h = layers * LAYER_H;
      let color = f.state === 'mined_out' ? COL.mined : f.state === 'virgin' ? COL.virgin : (verdictColor[f.verdict] || COL.virgin);
      const m = box(a.w - 1, h, a.l - 1, color, f.state === 'virgin' ? 0.35 : 0.8);
      m.position.set(a.x + a.w / 2, h / 2, a.y + a.l / 2);
      if (f.state === 'ventilating') { m.material.emissive = new THREE.Color(0xfde68a); m.material.emissiveIntensity = 0.5; }
      staticGroup.add(m); pickables.push({ mesh: m, id });
    });
    (byKind.pillar || []).forEach(id => {
      const t = twins[id], a = t.a, f = t.f;
      const h = f.extracted ? 1.5 : Math.max(LAYER_H, f.height);
      const m = box(a.w - 1, h, a.w - 1, sfColor(f), 1);
      m.position.set(a.x + a.w / 2, h / 2, a.y + a.w / 2);
      m.material.emissive = new THREE.Color(sfColor(f)); m.material.emissiveIntensity = f.status === 'критический' ? 0.6 : 0.15;
      staticGroup.add(m); pickables.push({ mesh: m, id });
    });
  }

  // частицы струи воздуха
  const particles = [];
  function rebuildFlow() {
    flowGroup.clear(); particles.length = 0;
    const geo = new THREE.ConeGeometry(1.6, 5, 6);
    airways.forEach(aw => {
      if (aw.blocked || aw.q < 0.5) return;
      const n = Math.max(2, Math.min(14, Math.round(aw.q / 10)));
      const mat = new THREE.MeshBasicMaterial({ color: aw.color });
      for (let i = 0; i < n; i++) {
        const p = new THREE.Mesh(geo, mat);
        flowGroup.add(p);
        particles.push({ mesh: p, aw, t: i / n });
      }
    });
  }
  function stepFlow(dt) {
    particles.forEach(pt => {
      const aw = pt.aw, len = Math.hypot(aw.b[0] - aw.a[0], aw.b[1] - aw.a[1]) || 1;
      pt.t = (pt.t + dt * Math.max(0.4, aw.v) * 1.6 / len * 10) % 1;
      const s = aw.dir > 0 ? pt.t : 1 - pt.t;
      const x = aw.a[0] + (aw.b[0] - aw.a[0]) * s, z = aw.a[1] + (aw.b[1] - aw.a[1]) * s;
      pt.mesh.position.set(x, aw.y, z);
      const dx = (aw.b[0] - aw.a[0]) * aw.dir, dz = (aw.b[1] - aw.a[1]) * aw.dir;
      pt.mesh.rotation.set(Math.PI / 2, 0, -Math.atan2(dz, dx) - Math.PI / 2, 'YXZ');
      pt.mesh.rotation.order = 'YXZ';
      pt.mesh.rotation.y = -Math.atan2(dz, dx);
      pt.mesh.rotation.x = 0; pt.mesh.rotation.z = -Math.PI / 2;
    });
  }

  // люди и техника
  const live = {};
  function updateLive(byKind, posOf) {
    const ids = [...(byKind.crew || []), ...(byKind.lhd || []), ...(byKind.jumbo || [])];
    const seen = new Set();
    ids.forEach(id => {
      const t = twins[id], f = t.f, kind = t.a.kind;
      seen.add(id);
      let o = live[id];
      if (!o) {
        let mesh;
        if (kind === 'crew') {
          mesh = new THREE.Group();
          for (let i = 0; i < 5; i++) {
            const p = new THREE.Mesh(new THREE.CapsuleGeometry(0.9, 2.2, 3, 6), new THREE.MeshLambertMaterial({ color: COL.crewIn }));
            p.position.set((i % 3) * 2.4 - 2.4, 2, Math.floor(i / 3) * 2.4 - 1.2);
            mesh.add(p);
          }
        } else {
          mesh = box(kind === 'lhd' ? 9 : 7, 3.2, 3.4, kind === 'lhd' ? COL.lhd : COL.jumbo);
          mesh.position.y = 1.6;
        }
        const label = labelSprite(kind === 'crew' ? t.a.short || id.split(':')[1] : (kind === 'lhd' ? 'ПДМ' : 'СБУ') + id.slice(-2), 7);
        label.position.y = kind === 'crew' ? 7 : 5;
        const g = new THREE.Group(); g.add(mesh, label);
        liveGroup.add(g);
        o = live[id] = { g, mesh, label, kind };
        pickables.push({ mesh: g, id });
      }
      const p = posOf(id);
      o.g.position.set(p.x + (kind === 'crew' ? -4 : 4), 0, p.y);
      let col;
      if (kind === 'crew') col = f.state === 'в камере' ? COL.crewIn : f.state === 'выведено' ? COL.crewOut : f.state === 'спуск' ? COL.crewMove : f.state === 'на поверхности' ? COL.crewSurface : COL.crewIdle;
      else col = f.state === 'выпуск' || f.state === 'бурение' ? COL.allowed : f.state === 'переезд' ? COL.crewMove : (kind === 'lhd' ? COL.lhd : COL.jumbo);
      if (kind === 'crew') o.mesh.children.forEach(c => c.material.color.setHex(col)); else o.mesh.material.color.setHex(col);
      o.g.visible = f.state !== 'на поверхности';
    });
  }

  // наведение: подсказка 2D-плана
  renderer.domElement.addEventListener('mousemove', (e) => {
    const r = renderer.domElement.getBoundingClientRect();
    mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(pickables.map(p => p.mesh), true);
    let id = null;
    if (hits.length) {
      let obj = hits[0].object;
      while (obj && !id) { const p = pickables.find(p => p.mesh === obj); if (p) id = p.id; obj = obj.parent; }
    }
    hoverId = id;
    if (id) hooks.showTip(e, id); else hooks.hideTip();
  });
  renderer.domElement.addEventListener('mouseleave', () => { hoverId = null; hooks.hideTip(); });

  function resize() {
    const w = container.clientWidth || 800, h = container.clientHeight || 500;
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize);

  const ns = () => (Object.keys(twins)[0] || 'org.openegiz.zhezkazgan:x').split(':')[0];
  let last = performance.now();
  let active = false;
  function frame(now) {
    requestAnimationFrame(frame);
    if (!active) return;
    const dt = Math.min(0.1, (now - last) / 1000); last = now;
    stepFlow(dt);
    controls.update();
    renderer.render(scene, camera);
  }
  requestAnimationFrame(frame);

  return {
    setActive(on) { active = on; if (on) { resize(); last = performance.now(); } },
    update(allTwins, byKind, n, posOf) {
      twins = allTwins; norms = n || norms;
      const sig = [...(byKind.airway || []), ...(byKind.chamber || []), ...(byKind.pillar || []), ...(byKind.panel || []), ...(byKind.sensor || []), ...(byKind.shaft || [])]
        .map(id => { const f = twins[id].f; return id + (f.status || '') + (f.verdict || '') + (f.state || '') + (f.door_state || '') + Math.round((f.q || 0)) + (f.sf !== undefined ? f.sf.toFixed(1) : '') + (f.alarm || '') + (f.mode || '') + (f.layer ?? ''); }).join('|')
        + (twins[`${ns()}:fan-main`] ? JSON.stringify(twins[`${ns()}:fan-main`].f) : '');
      if (sig !== staticSig) { staticSig = sig; rebuildStatic(byKind); rebuildFlow(); }
      updateLive(byKind, posOf);
    },
    view(name) {
      const v = { all: [[620, 520, 900], [490, 0, 260]], p1: [[300, 160, 420], [240, 0, 180]], p2: [[540, 160, 420], [480, 0, 180]], p3: [[780, 160, 420], [720, 0, 180]] }[name];
      if (!v) return;
      camera.position.set(...v[0]); controls.target.set(...v[1]);
    },
    resize,
  };
}
