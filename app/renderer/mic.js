'use strict';
/* 顶栏麦克风分贝表 —— 机器人 .162 环麦实时声级（bridge/mic_bridge.py，9803）
   显示：电平条（-60..0 dBFS）+ 峰值保持刻度 + 数值。打开面板自动连接，断线 3s 重连。 */

const MIC_HOST = '192.168.1.50';
const MIC_PORT = 9803;
const MIC = { connected: false, userOff: false, timer: null };

function micSetUI(on, err) {
  MIC.connected = on;
  const text = document.getElementById('micText');
  if (on) {
    text.textContent = '…';
  } else {
    document.getElementById('micBar').style.width = '0';
    document.getElementById('micPeak').style.left = '0';
    text.textContent = err ? 'ERR' : '--';
  }
}

function micSchedule(reconnect) {
  if (MIC.userOff) return;
  clearTimeout(MIC.timer);
  MIC.timer = setTimeout(() => { if (!MIC.connected) micConnect(); },
                        reconnect ? 3000 : 800);
}

async function micConnect() {
  const r = await window.mic.connect(MIC_HOST, MIC_PORT);
  if (!r.ok) micSetUI(false, r.error);
}

window.mic.onMessage((m) => {
  if (m.t === 'hello') { MIC.connected = true; micSetUI(true); }
  if (m.t === 'asr') { showAsr(m.text, m.final); return; }
  if (m.t !== 'level') return;
  if (!MIC.connected) { MIC.connected = true; micSetUI(true); }
  const db = Math.max(-60, Math.min(0, m.db));
  const peak = Math.max(-60, Math.min(0, m.peak));
  const frac = (db + 60) / 60;                       // -60..0 -> 0..1
  const bar = document.getElementById('micBar');
  const pk = document.getElementById('micPeak');
  bar.style.width = (frac * 100).toFixed(1) + '%';
  bar.style.background = db > -15 ? 'var(--bad)' : db > -35 ? 'var(--warn)' : 'var(--ok)';
  pk.style.left = ((peak + 60) / 60 * 100).toFixed(1) + '%';
  document.getElementById('micText').textContent = m.db.toFixed(0) + ' dB';
});

/* 语音识别结果：顶栏 pill 常驻显示最近一句（partial 实时刷新，final 高亮保留），
   final 同时追加到「日志」卡。 */
function showAsr(text, final) {
  const pill = document.getElementById('pillAsr');
  const el = document.getElementById('asrText');
  if (!text) return;
  pill.style.display = '';
  pill.classList.toggle('final', !!final);
  pill.title = text;
  el.textContent = (final ? '识别: ' : '') + (text.length > 26 ? text.slice(0, 26) + '…' : text);
  if (final && typeof window.log === 'function') window.log('asr', `🎙 识别: ${text}`);
}

window.mic.onStatus((s) => {
  if (!s.connected) {
    micSetUI(false, s.error);
    if (!MIC.userOff) micSchedule(true);
  }
});

micSchedule(false);   // 打开面板即自动连接
