'use strict';
/* 雷达 / 相机页 —— Odin2 点云 + 双目图像
   数据经 main.js 的 9802 通道（JSON 头 + 二进制负载，见 bridge/odin_bridge.py）。 */

const V = {
  connected: false,
  cloud: null,            // Float32Array，xyz 三元组（雷达系 x 前 / y 左 / z 上，单位 m）
  npts: 0,
  cam: { 0: { url: null }, 1: { url: null } },
  cnt: { cloud: 0, cam0: 0, cam1: 0 },
  hz: { cloud: 0, cam0: 0, cam1: 0 },
  _lastTick: performance.now(),
  server: null,
  view: { yaw: 0.35, el: 0.95, range: 6.0 },
  mode: 'dist',
  dragging: false, lastX: 0, lastY: 0,
  paused: false,
  userDisconnected: false,   // 手动点过「断开」后不再自动重连
};

const $v = (id) => document.getElementById(id);
const canvas = $v('oCanvas');
const ctx = canvas.getContext('2d');

/* ---------- 调色板（按距离 / 按高度，64 级预生成，避免逐点拼字符串） ---------- */
function buildPalette(stops, vmin, vmax) {
  const pal = new Array(64);
  for (let i = 0; i < 64; i++) {
    const v = vmin + (vmax - vmin) * (i / 63);
    let a = stops[0], b = stops[stops.length - 1];
    for (let k = 0; k < stops.length - 1; k++) {
      if (v >= stops[k][0] && v <= stops[k + 1][0]) { a = stops[k]; b = stops[k + 1]; break; }
    }
    const t = b[0] === a[0] ? 0 : (v - a[0]) / (b[0] - a[0]);
    const ca = hex2rgb(a[1]), cb = hex2rgb(b[1]);
    const r = Math.round(ca[0] + (cb[0] - ca[0]) * t);
    const g = Math.round(ca[1] + (cb[1] - ca[1]) * t);
    const bl = Math.round(ca[2] + (cb[2] - ca[2]) * t);
    pal[i] = `rgb(${r},${g},${bl})`;
  }
  return pal;
}
function hex2rgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}
const PAL_DIST = buildPalette(
  [[0, '#ff3b30'], [1, '#ff9500'], [2, '#ffd60a'], [3.5, '#34c759'], [6, '#00c7ff'], [10, '#5e5ce6']], 0, 10);
const PAL_H = buildPalette(
  [[-1.5, '#3b82f6'], [0, '#22c55e'], [1, '#eab308'], [2.5, '#ef4444']], -1.5, 2.5);

/* ---------- 视图切换 ---------- */
function switchView(which) {
  const vision = which === 'vision';
  document.body.classList.toggle('view-vision', vision);
  document.body.classList.toggle('view-joints', !vision);
  $v('viewVision').classList.toggle('hidden', !vision);
  $v('viewJoints').classList.toggle('hidden', vision);
  $v('viewBtnVision').classList.toggle('active', vision);
  $v('viewBtnJoints').classList.toggle('active', !vision);
}
$v('viewBtnJoints').addEventListener('click', () => switchView('joints'));
$v('viewBtnVision').addEventListener('click', () => switchView('vision'));

/* ---------- 点云绘制 ---------- */
function draw() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  ctx.fillStyle = '#05070a';
  ctx.fillRect(0, 0, w, h);

  const cx = w / 2, cy = h * 0.60;
  const scale = Math.min(w, h) / (2 * V.view.range);
  const ca = Math.cos(V.view.yaw), sa = Math.sin(V.view.yaw);
  const ce = Math.cos(V.view.el), se = Math.sin(V.view.el);
  // 投影：yaw 绕 z 旋转 → 仰角 el 压平 → 屏幕(x=-y1, y=-(x1*sin+z*cos))
  const P = (x, y, z) => {
    const x1 = x * ca - y * sa, y1 = x * sa + y * ca;
    return [cx + (-y1) * scale, cy - (x1 * se + z * ce) * scale];
  };

  // 地面网格（±5 m，1 m 间隔）+ 坐标轴
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#141d27';
  ctx.beginPath();
  for (let i = -5; i <= 5; i++) {
    let p = P(i, -5, 0), q = P(i, 5, 0);
    ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]);
    p = P(-5, i, 0); q = P(5, i, 0);
    ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]);
  }
  ctx.stroke();
  const axis = (v, color) => {
    const o = P(0, 0, 0), p = P(v[0], v[1], v[2]);
    ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(o[0], o[1]); ctx.lineTo(p[0], p[1]); ctx.stroke();
  };
  axis([1, 0, 0], '#f85149');   // X 前
  axis([0, 1, 0], '#3fb950');   // Y 左
  axis([0, 0, 1], '#2f81f7');   // Z 上

  // 点云
  const pts = V.cloud, n = V.npts;
  if (pts && n > 0) {
    const pal = V.mode === 'dist' ? PAL_DIST : PAL_H;
    const vm = V.mode === 'dist' ? [0, 10] : [-1.5, 2.5];
    const lim = V.view.range * 1.25;
    for (let i = 0; i < n; i++) {
      const x = pts[i * 3], y = pts[i * 3 + 1], z = pts[i * 3 + 2];
      const x1 = x * ca - y * sa, y1 = x * sa + y * ca;
      const px = -y1, py = -(x1 * se + z * ce);
      if (px < -lim || px > lim || py < -lim || py > lim) continue;
      const sxp = cx + px * scale, syp = cy + py * scale;
      if (sxp < 0 || sxp >= w || syp < 0 || syp >= h) continue;
      let v = V.mode === 'dist' ? Math.sqrt(x * x + y * y + z * z) : z;
      let k = ((v - vm[0]) / (vm[1] - vm[0])) * 63 | 0;
      if (k < 0) k = 0; else if (k > 63) k = 63;
      ctx.fillStyle = pal[k];
      ctx.fillRect(sxp, syp, 1.6, 1.6);
    }
  }

  // HUD
  ctx.fillStyle = '#6e7681';
  ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
  const deg = (r) => (r * 180 / Math.PI).toFixed(0);
  ctx.fillText(`yaw ${deg(V.view.yaw)}°  el ${deg(V.view.el)}°  range ${V.view.range.toFixed(1)} m`, 10, h - 10);
  if (!V.cloud || V.npts === 0) {
    ctx.fillStyle = '#8b949e';
    ctx.font = '13px sans-serif';
    ctx.fillText(V.connected ? '等待点云数据…' : '未连接（点上方「连接」）', w / 2 - 70, h / 2);
  }
}

/* ---------- 相机图像 ---------- */
function setImage(cam, u8) {
  const img = $v(cam === 0 ? 'oCam0' : 'oCam1');
  const st = V.cam[cam];
  const url = URL.createObjectURL(new Blob([u8], { type: 'image/jpeg' }));
  img.src = url;
  if (st.url) URL.revokeObjectURL(st.url);   // 旧帧已不需要
  st.url = url;
  V.cnt['cam' + cam]++;
}

/* ---------- 来自主进程的帧 ---------- */
window.odin.onFrame(({ header, payload }) => {
  if (!header) return;
  // 不依赖 hello（可能早于渲染层注册监听而丢失）：任何一帧到达都视为已连接
  if (!V.connected) { V.connected = true; setConnUI(true); }
  switch (header.t) {
    case 'hello':
      break;
    case 'cloud':
      if (payload) {
        V.cloud = new Float32Array(payload.slice().buffer);   // 复制一份，保证 4 字节对齐
        V.npts = header.n || (V.cloud.length / 3);
        V.cnt.cloud++;
      }
      break;
    case 'image':
      if (payload) setImage(header.cam === 1 ? 1 : 0, payload);
      break;
    case 'status':
      V.server = header;
      break;
    case 'cfg_ok':
    case 'pong':
      break;
  }
});

window.odin.onStatus((s) => {
  if (!s.connected) {
    V.connected = false;
    setConnUI(false, s.error);
    if (!V.userDisconnected) scheduleAuto(true);   // 断线自动重连
  }
});

function setConnUI(on, err) {
  $v('oConnect').disabled = on;
  $v('oDisconnect').disabled = !on;
  const pill = $v('pillOdin');
  pill.innerHTML = `<i class="dot${on ? ' on' : ''}"></i>雷达: ${on ? '已连接' : '未连接'}`;
  if (!on) {
    $v('pillOdinStats').textContent = err ? `连接失败: ${err}` : '--';
    $v('oCam0Hz').textContent = '--';
    $v('oCam1Hz').textContent = '--';
  }
}

/* ---------- 控制条 ---------- */
function sendCfg() {
  if (!V.connected) return;
  window.odin.send({
    t: 'cfg',
    stride: Number($v('oStride').value),
    cloud_hz: Number($v('oCloudHz').value),
    image_hz: Number($v('oImgHz').value),
    cloud: true, images: true,
  });
}
for (const id of ['oStride', 'oCloudHz', 'oImgHz']) {
  $v(id).addEventListener('change', sendCfg);
}
$v('oColor').addEventListener('change', () => { V.mode = $v('oColor').value; });
$v('oPause').addEventListener('change', () => { V.paused = $v('oPause').checked; });
$v('oReset').addEventListener('click', resetView);

/* ---------- 连接（启动自动连，断线自动重连） ---------- */
let autoTimer = null;

async function doConnect() {
  const host = $v('oHost').value.trim(), port = Number($v('oPort').value);
  $v('pillOdinStats').textContent = '连接中…';
  const r = await window.odin.connect(host, port);
  if (!r.ok) { setConnUI(false, r.error); scheduleAuto(true); }
  else { setConnUI(true); sendCfg(); }
}
function scheduleAuto(reconnect) {
  if (V.userDisconnected) return;
  clearTimeout(autoTimer);
  autoTimer = setTimeout(() => { if (!V.connected) doConnect(); },
                        reconnect ? 3000 : 800);
}

$v('oConnect').addEventListener('click', () => {
  V.userDisconnected = false;
  doConnect();
});
$v('oDisconnect').addEventListener('click', async () => {
  V.userDisconnected = true;
  clearTimeout(autoTimer);
  await window.odin.disconnect();
  setConnUI(false);
});

/* ---------- 视角交互 ---------- */
function resetView() { V.view = { yaw: 0.35, el: 0.95, range: 6.0 }; }
canvas.addEventListener('mousedown', (e) => {
  V.dragging = true; V.lastX = e.clientX; V.lastY = e.clientY;
});
window.addEventListener('mouseup', () => { V.dragging = false; });
window.addEventListener('mousemove', (e) => {
  if (!V.dragging) return;
  V.view.yaw += (e.clientX - V.lastX) * 0.008;
  V.view.el = Math.min(1.55, Math.max(0.05, V.view.el + (e.clientY - V.lastY) * 0.008));
  V.lastX = e.clientX; V.lastY = e.clientY;
});
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  V.view.range = Math.min(30, Math.max(1.5, V.view.range * (e.deltaY > 0 ? 1.12 : 0.89)));
}, { passive: false });
canvas.addEventListener('dblclick', resetView);

/* ---------- 统计与渲染循环 ---------- */
setInterval(() => {
  const now = performance.now();
  const dt = (now - V._lastTick) / 1000;
  V._lastTick = now;
  for (const k of ['cloud', 'cam0', 'cam1']) {
    V.hz[k] = V.cnt[k] / dt;
    V.cnt[k] = 0;
  }
  if (!V.connected) return;
  $v('oCam0Hz').textContent = V.hz.cam0.toFixed(1) + ' Hz';
  $v('oCam1Hz').textContent = V.hz.cam1.toFixed(1) + ' Hz';
  const s = V.server;
  $v('pillOdinStats').textContent = s
    ? `收到 点云 ${V.hz.cloud.toFixed(1)} / ${s.cloud_hz?.toFixed(1) ?? '-'} Hz · 有效点 ${s.points ?? '-'}`
    : `收到 点云 ${V.hz.cloud.toFixed(1)} Hz`;
  $v('oPts').textContent = String(V.npts);
}, 500);

function loop() {
  if (!V.paused) draw();
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);

switchView('joints');
setConnUI(false);
scheduleAuto(false);   // 打开面板即自动连接雷达/相机流
