'use strict';
/* T800 Pro 控制面板 —— 渲染层
   通过 window.bridge (preload) 与主进程通信，主进程维护到 Orin relay 的 TCP 连接。 */

const NJ = 43;
const GROUPS = [
  { name: '左腿', idx: 'J00-J05', from: 0, to: 6 },
  { name: '右腿', idx: 'J06-J11', from: 6, to: 12 },
  { name: '腰', idx: 'J12', from: 12, to: 13 },
  { name: '左臂', idx: 'J13-J17', from: 13, to: 18 },
  { name: '左腕', idx: 'J18-J19', from: 18, to: 20 },
  { name: '左手', idx: 'J20-J26', from: 20, to: 27 },
  { name: '右臂', idx: 'J27-J31', from: 27, to: 32 },
  { name: '右腕', idx: 'J32-J33', from: 32, to: 34 },
  { name: '右手', idx: 'J34-J40', from: 34, to: 41 },
  { name: '头', idx: 'J41-J42', from: 41, to: 43 },
];

const S = {
  connected: false,
  joints: [],
  pos: new Array(NJ).fill(0),
  vel: new Array(NJ).fill(0),
  tau: new Array(NJ).fill(0),
  tmo: new Array(NJ).fill(null),   // 电机温度 ℃
  tmos: new Array(NJ).fill(null),  // 驱动 MOS 温度 ℃
  targets: new Array(NJ).fill(0),
  touched: new Array(NJ).fill(false),   // 用户是否主动设过该关节目标；未触碰的关节始终跟随当前角度
  mode: '', avail: [], enable: false, stream: false, clients: 0,
  battery: null,
  dirty: false, slew: 3.0, kp: new Array(NJ).fill(100), kd: new Array(NJ).fill(1),
  rows: [], gainInputs: [],
};

const $ = (id) => document.getElementById(id);

function log(level, msg) {
  const el = $('log');
  const time = new Date().toTimeString().slice(0, 8);
  const line = document.createElement('div');
  if (level) line.className = level;
  line.textContent = `[${time}] ${msg}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  while (el.childNodes.length > 400) el.removeChild(el.firstChild);
}

/* ---------------- 关节行 ---------------- */
function buildJoints() {
  const host = $('joints');
  host.innerHTML = '';
  S.rows = [];
  for (const g of GROUPS) {
    const sec = document.createElement('div');
    sec.className = 'group';
    const h = document.createElement('h3');
    h.textContent = `${g.name} (${g.idx})`;
    sec.appendChild(h);
    for (let i = g.from; i < g.to; i++) {
      const j = S.joints[i] || { name: 'J' + String(i).padStart(2, '0'), lo: -3.2, hi: 3.2 };
      const row = document.createElement('div');
      row.className = 'jrow';
      row.innerHTML =
        `<span class="jname" title="${j.name}">${j.name}</span>` +
        `<span class="cur">0.00</span>` +
        `<input type="range" min="${j.lo}" max="${j.hi}" step="0.005" value="0">` +
        `<span class="tau" title="关节力矩反馈 (N·m)">0.0</span>` +
        `<span class="temp" title="电机温度 (℃)">--</span>` +
        `<input type="number" class="tgt" min="${j.lo}" max="${j.hi}" step="0.01" value="0">` +
        `<button class="snap" title="目标 = 当前角度">⟲</button>`;
      const slider = row.querySelector('input[type=range]');
      const tgt = row.querySelector('.tgt');
      const cur = row.querySelector('.cur');
      const tau = row.querySelector('.tau');
      const temp = row.querySelector('.temp');
      const snap = row.querySelector('.snap');
      slider.addEventListener('input', () => setTarget(i, parseFloat(slider.value)));
      tgt.addEventListener('change', () => setTarget(i, parseFloat(tgt.value)));
      snap.addEventListener('click', () => setTarget(i, S.pos[i]));
      S.rows[i] = { row, slider, tgt, cur, tau, temp, j };
      sec.appendChild(row);
    }
    host.appendChild(sec);
  }
}

function setTarget(i, v) {
  if (!isFinite(v) || !S.rows[i]) return;
  const j = S.rows[i].j;
  v = Math.min(Math.max(v, j.lo), j.hi);
  S.targets[i] = v;
  // 拖回接近当前角度即视为“未触碰”，恢复自动跟随
  S.touched[i] = Math.abs(v - S.pos[i]) > 0.01;
  S.rows[i].slider.value = v;
  S.rows[i].tgt.value = Number(v.toFixed(3));
  S.dirty = true;
}

function syncTargetsToPos() {
  for (let i = 0; i < NJ; i++) {
    if (!S.rows[i]) continue;
    S.targets[i] = S.pos[i];
    S.touched[i] = false;
    S.rows[i].slider.value = S.pos[i];
    S.rows[i].tgt.value = Number(S.pos[i].toFixed(3));
  }
  S.dirty = true;
}

/* ---------------- 增益 ---------------- */
function buildGains() {
  const host = $('gains');
  host.innerHTML = '';
  S.gainInputs = [];
  for (const g of GROUPS) {
    const div = document.createElement('div');
    div.className = 'gainrow';
    const kp = S.kp[g.from] ?? 100;
    const kd = S.kd[g.from] ?? 1;
    div.innerHTML = `<span>${g.name}</span>` +
      `<input type="number" value="${kp}" step="5" title="kp">` +
      `<input type="number" value="${kd}" step="0.05" title="kd">`;
    const [kpI, kdI] = div.querySelectorAll('input');
    S.gainInputs.push({ g, kpI, kdI });
    host.appendChild(div);
  }
}

/* ---------------- 渲染 ---------------- */
function renderState() {
  $('pillMode').innerHTML = `模式: <b>${S.mode || '-'}</b>`;
  $('pillEnable').innerHTML = `电机: <b>${S.enable ? '已使能' : '未使能'}</b>`;
  $('pillStream').innerHTML = `下发: <b>${S.stream ? '开' : '关'}</b>`;

  // 电量图标
  const b = S.battery || {};
  const pct = (typeof b.pct === 'number') ? b.pct : null;
  const fill = $('batFill'), txt = $('batText'), pill = $('pillBattery');
  if (pct === null) {
    fill.setAttribute('width', '0');
    txt.textContent = '--%';
    pill.title = '电池：等待数据…';
  } else {
    const p = Math.min(100, Math.max(0, pct));
    fill.setAttribute('width', (Math.max(0.6, 16 * p / 100)).toFixed(1));
    fill.setAttribute('fill', p > 50 ? '#3fb950' : (p > 20 ? '#d29922' : '#f85149'));
    txt.textContent = p.toFixed(0) + '%';
    const parts = [`电量 ${p.toFixed(1)}%`];
    if (typeof b.volt === 'number') parts.push(`电压 ${b.volt.toFixed(1)}V`);
    if (typeof b.current === 'number') parts.push(`电流 ${b.current.toFixed(2)}A`);
    if (typeof b.limit === 'number') parts.push(`限流 ${b.limit.toFixed(0)}A`);
    if (b.err) parts.push(`故障码 ${b.err}`);
    pill.title = '电池：' + parts.join(' · ');
  }
  $('modeNow').textContent = S.mode || '-';
  $('enableNow').textContent = S.enable ? '已使能' : '未使能';
  $('streamNow').textContent = S.stream ? '开' : '关';
  $('clientsNow').textContent = String(S.clients ?? 0);

  const mb = $('modeBtns');
  const key = S.avail.join(',');
  if (mb.dataset.key !== key) {
    mb.dataset.key = key;
    mb.innerHTML = '';
    if (!S.avail.length) {
      mb.innerHTML = '<span class="hint">（无可切换目标）</span>';
    } else {
      for (const name of S.avail) {
        const b = document.createElement('button');
        b.textContent = name;
        b.onclick = () => { window.bridge.send({ t: 'mode', name }); log('', `请求切换模式: ${name}`); };
        mb.appendChild(b);
      }
    }
  }

  for (let i = 0; i < NJ; i++) {
    const r = S.rows[i];
    if (!r) continue;
    r.cur.textContent = S.pos[i].toFixed(2);
    const moved = Math.abs(S.targets[i] - S.pos[i]) > 0.02;
    r.row.classList.toggle('moved', moved);
    // 力矩反馈
    const t = S.tau[i] || 0;
    const at = Math.abs(t);
    r.tau.textContent = (at < 0.05 ? '0.0' : t.toFixed(1));
    r.tau.className = 'tau' + (at >= 60 ? ' max' : (at >= 25 ? ' hot' : ''));
    // 电机/MOS 温度：手指等关节不上报电机温度，回退显示 MOS 温度
    const mtv = S.tmo[i], mos = S.tmos[i];
    const tc = (typeof mtv === 'number' && mtv > 0) ? mtv
             : (typeof mos === 'number' && mos > 0) ? mos : null;
    if (tc === null) {
      r.temp.textContent = '--';
      r.temp.className = 'temp';
      r.temp.title = '温度 (℃)：无数据';
    } else {
      r.temp.textContent = Math.round(tc) + '°';
      r.temp.className = 'temp' + (tc >= 80 ? ' max' : (tc >= 60 ? ' hot' : ''));
      const parts = [];
      if (typeof mtv === 'number' && mtv > 0) parts.push(`电机 ${mtv.toFixed(1)}℃`);
      if (typeof mos === 'number' && mos > 0) parts.push(`MOS ${mos.toFixed(1)}℃`);
      r.temp.title = parts.join(' · ') || '无数据';
    }
  }

  // 最大力矩汇总
  let mi = 0;
  for (let i = 1; i < NJ; i++) if (Math.abs(S.tau[i] || 0) > Math.abs(S.tau[mi] || 0)) mi = i;
  const mt = S.tau[mi] || 0;
  const mname = (S.joints[mi] && S.joints[mi].name || ('J' + mi)).split('_')[0];
  $('tauMax').textContent = Math.abs(mt) < 0.05 ? '-' :
    `${mname} ${mt.toFixed(1)} N·m`;

  // 最高电机温度汇总
  let hi = -1, hv = 0, hs = 0;
  for (let i = 0; i < NJ; i++) {
    const v = S.tmo[i];
    if (typeof v === 'number' && v > 0 && v > hv) { hv = v; hi = i; hs = S.tmos[i] || 0; }
  }
  $('tempMax').textContent = hi < 0 ? '-' :
    `${(S.joints[hi] && S.joints[hi].name || ('J' + hi)).split('_')[0]} ${hv.toFixed(1)}℃` +
    (hs > 0 ? `（MOS ${hs.toFixed(1)}℃）` : '');

  const bs = $('btnStream');
  bs.textContent = S.stream ? '停止发送' : '开始发送';
  bs.classList.toggle('active', S.stream);

  const bt = $('btnToJoint');
  if (chainRunning) {
    bt.textContent = '切换中…(再点取消)';
    bt.className = 'part-joint active';
  } else if (S.mode === 'joint_bridge') {
    bt.textContent = 'joint_bridge ✓';
    bt.className = 'part-joint okbtn';
  } else if (Date.now() - chainArmed < 5000 && chainArmed > 0) {
    bt.textContent = '确认切换？再点一次';
    bt.className = 'part-joint danger';
  } else {
    bt.textContent = '进入关节控制';
    bt.className = 'part-joint';
  }

  const bn = $('banner');
  if (!S.connected) {
    bn.className = 'hidden';
  } else {
    const warns = [];
    let bad = false;
    if (!S.enable) { warns.push('电机未使能（先用急停遥控器使能电机系统）'); bad = true; }
    if (S.mode !== 'joint_bridge') warns.push(`当前模式 ${S.mode || '-'}，全关节指令只在 joint_bridge 模式生效`);
    if (warns.length) { bn.textContent = '⚠ ' + warns.join(' ｜ '); bn.className = bad ? 'bad' : ''; }
    else bn.className = 'hidden';
  }
}

/* ---------------- 一键进入 joint_bridge ----------------
   逐级发送 mode 请求：每一步都只从机器人实时下发的 avail 里挑目标，
   路径偏好 joint_bridge > pd_stand > lower_body_balance > rl_basic > passive，
   10s 超时重试一次，Esc/紧急停止可中断。 */
const MODE_PREF = ['joint_bridge', 'pd_stand', 'lower_body_balance', 'rl_basic', 'passive'];
let chainRunning = false;
let chainArmed = 0;

function waitForMode(target, timeoutMs) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (!chainRunning || !S.connected) { clearInterval(iv); resolve(false); }
      else if (S.mode === target) { clearInterval(iv); resolve(true); }
      else if (Date.now() - t0 > timeoutMs) { clearInterval(iv); resolve(false); }
    }, 150);
  });
}

async function autoChainToJointBridge() {
  if (chainRunning) { chainRunning = false; log('w', '已取消自动切换'); return; }
  if (!S.connected) { log('e', '未连接'); return; }
  if (!S.enable) { log('e', '拒绝：电机未使能（先用急停遥控器使能）'); return; }
  if (S.mode === 'joint_bridge') { log('', '已在 joint_bridge 模式'); return; }
  if (Date.now() - chainArmed > 5000) {
    chainArmed = Date.now();
    log('w', '再次点击确认：机器人将开始动作（务必吊架/支撑到位、周围清场）');
    return;
  }
  chainArmed = 0;
  chainRunning = true;
  log('', '开始自动切换到 joint_bridge…（再点一次按钮可取消）');
  try {
    for (let hop = 0; hop < 8 && chainRunning; hop++) {
      if (S.mode === 'joint_bridge') break;
      const avail = S.avail || [];
      const target = MODE_PREF.find((p) => p !== S.mode && avail.includes(p));
      if (!target) {
        log('e', `无法继续：当前 ${S.mode || '-'}，可切换目标 [${avail.join(', ') || '无'}] 不在预期路径上`);
        break;
      }
      log('', `第 ${hop + 1} 步：${S.mode || '-'} → ${target}`);
      window.bridge.send({ t: 'mode', name: target });
      const from = S.mode;
      if (await waitForMode(target, 10000)) {
        // current_motion_task 在请求受理时就可能改名（绿灯闪烁=切换中），停 1.2s 让动作落定
        for (let i = 0; i < 12 && chainRunning; i++) await new Promise((r) => setTimeout(r, 100));
        log('', `已进入 ${target}`);
      } else if (S.mode === from && (S.avail || []).includes(target)) {
        log('w', `切换超时，重试一次 → ${target}`);
        window.bridge.send({ t: 'mode', name: target });
        if (!(await waitForMode(target, 10000))) {
          log('e', `切换失败：当前仍为 ${S.mode || '-'}`);
          break;
        }
        log('', `已进入 ${target}`);
      } else {
        log('e', `切换未达成：当前 ${S.mode || '-'}`);
        break;
      }
    }
    if (S.mode === 'joint_bridge') log('', '✅ 已进入 joint_bridge，点「开始发送」即可控制关节');
  } finally {
    chainRunning = false;
    chainArmed = 0;
  }
}

/* ---------------- 事件 ---------------- */
let manualDisconnect = false;
let reconnectTimer = null;

function scheduleReconnect() {
  if (reconnectTimer || manualDisconnect) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (!S.connected) $('btnConnect').click();
  }, 3000);
}

window.bridge.onStatus((st) => {
  S.connected = !!st.connected;
  $('pillConn').innerHTML = `<i class="dot ${S.connected ? 'on' : ''}"></i>${S.connected ? '已连接' : '未连接'}`;
  $('btnConnect').disabled = S.connected;
  $('btnDisconnect').disabled = !S.connected;
  $('btnStream').disabled = !S.connected;
  $('btnHold').disabled = !S.connected;
  $('btnApplyGains').disabled = !S.connected;
  $('btnToJoint').disabled = !S.connected;
  if (!S.connected && st.error) log('e', '连接错误: ' + st.error);
  if (S.connected) { log('', `已连接 ${st.host || ''}${st.port ? ':' + st.port : ''}`); }
  else if (!manualDisconnect) { log('w', '连接断开，3 秒后自动重连…'); scheduleReconnect(); }
});

window.bridge.onMessage((m) => {
  if (m.t === 'hello') {
    S.joints = m.joints || [];
    S.slew = m.slew_default || 3;
    S.kp = m.kp || S.kp;
    S.kd = m.kd || S.kd;
    $('slew').value = S.slew;
    buildJoints();
    buildGains();
    log('', `握手完成：${S.joints.length} 个关节，rate=${m.rate}Hz，slew=${m.slew_default}rad/s`);
  } else if (m.t === 'state') {
    if (m.pos && m.pos.length === NJ) S.pos = m.pos;
    if (m.vel && m.vel.length === NJ) S.vel = m.vel;
    if (m.tau && m.tau.length === NJ) S.tau = m.tau;
    if (m.tmo && m.tmo.length === NJ) S.tmo = m.tmo;
    if (m.tmos && m.tmos.length === NJ) S.tmos = m.tmos;
    S.mode = m.mode; S.avail = m.avail || [];
    S.enable = !!m.enable; S.stream = !!m.stream; S.clients = m.clients;
    if (m.battery) S.battery = m.battery;
    // 未触碰的关节始终跟随当前角度（避免把陈旧/零目标发下去）
    for (let i = 0; i < NJ; i++) {
      if (!S.touched[i] && S.rows[i]) {
        if (Math.abs(S.targets[i] - S.pos[i]) > 1e-4) S.dirty = true;
        S.targets[i] = S.pos[i];
        S.rows[i].slider.value = S.pos[i];
        S.rows[i].tgt.value = Number(S.pos[i].toFixed(3));
      }
    }
    renderState();
  } else if (m.t === 'log') {
    log(m.level === 'warn' ? 'w' : (m.level === 'error' ? 'e' : ''), m.msg);
  }
});

$('btnConnect').onclick = async () => {
  manualDisconnect = false;
  const host = $('host').value.trim();
  const port = parseInt($('port').value, 10) || 9801;
  log('', `连接 ${host}:${port} …`);
  const r = await window.bridge.connect(host, port);
  if (!r.ok) { log('e', '连接失败: ' + r.error); scheduleReconnect(); }
};

$('btnDisconnect').onclick = () => { manualDisconnect = true; window.bridge.disconnect(); };

$('btnToJoint').onclick = () => autoChainToJointBridge();

$('btnStream').onclick = async () => {
  if (S.stream) {
    await window.bridge.send({ t: 'stream', on: false });
    log('w', '已停止下发（joint_bridge 下机器人保持最后姿态）');
    return;
  }
  if (S.mode !== 'joint_bridge') { log('e', '拒绝：当前模式不是 joint_bridge，请先切换'); return; }
  if (!S.enable) { log('e', '拒绝：电机未使能'); return; }
  syncTargetsToPos();
  await window.bridge.send({ t: 'hold' });
  await window.bridge.send({ t: 'targets', pos: S.targets });
  await window.bridge.send({ t: 'stream', on: true });
  log('', '开始下发：目标已对齐当前角度，可拖动滑杆');
};

$('btnHold').onclick = async () => {
  syncTargetsToPos();
  await window.bridge.send({ t: 'hold' });
  log('', '所有目标已设为当前角度');
};

$('btnEstop').onclick = async () => {
  chainRunning = false; chainArmed = 0;
  await window.bridge.send({ t: 'stream', on: false });
  log('e', '紧急停止：已停止下发关节指令');
};

$('slew').onchange = () => {
  const v = parseFloat($('slew').value);
  if (isFinite(v)) { S.slew = v; window.bridge.send({ t: 'slew', v }); log('', `斜率限制 = ${v} rad/s`); }
};

$('btnApplyGains').onclick = async () => {
  const kp = [...S.kp], kd = [...S.kd];
  for (const { g, kpI, kdI } of S.gainInputs) {
    const kv = parseFloat(kpI.value), dv = parseFloat(kdI.value);
    for (let i = g.from; i < g.to; i++) {
      if (isFinite(kv)) kp[i] = kv;
      if (isFinite(dv)) kd[i] = dv;
    }
  }
  S.kp = kp; S.kd = kd;
  await window.bridge.send({ t: 'gains', kp, kd });
  log('', '已应用增益（按组）');
};

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $('btnEstop').click();
});

setInterval(() => {
  if (S.connected && S.dirty) {
    S.dirty = false;
    window.bridge.send({ t: 'targets', pos: S.targets });
  }
}, 50);

setInterval(() => { if (S.connected) window.bridge.send({ t: 'ping' }); }, 2000);

/* 启动：用内置模型立即渲染 43 关节，并自动连接中转 */
if (Array.isArray(window.T800_JOINTS) && window.T800_JOINTS.length === NJ) {
  S.joints = window.T800_JOINTS;
  buildJoints();
}
log('', '面板就绪：需先在机器人侧进入 joint_bridge 模式才能下发关节指令');
setTimeout(() => { if (!S.connected) $('btnConnect').click(); }, 600);
