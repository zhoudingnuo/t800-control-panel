'use strict';
// Electron 主进程：创建窗口 + 维护到 Orin relay 的 TCP 连接
//   关节控制：9801，行分隔 JSON
//   雷达/相机：9802，JSON 头行 + 定长二进制负载（帧格式见 bridge/odin_bridge.py）
//   麦克风分贝：9803，行分隔 JSON（{"t":"level","db":..}）
const { app, BrowserWindow, ipcMain } = require('electron');
const net = require('net');
const path = require('path');

let win = null;
let sock = null;
let rxbuf = '';
let odinSock = null;
let odinBuf = Buffer.alloc(0);
let micSock = null;
let micBuf = '';

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

// odin 流：按 “一行 JSON 头（含 len）+ len 字节负载” 解帧
function handleOdinData(d) {
  odinBuf = Buffer.concat([odinBuf, d]);
  for (;;) {
    const idx = odinBuf.indexOf(0x0a);
    if (idx < 0) break;
    let header;
    try {
      header = JSON.parse(odinBuf.slice(0, idx).toString('utf8'));
    } catch (e) {
      odinBuf = odinBuf.slice(idx + 1);
      continue;
    }
    const len = Number(header && header.len) || 0;
    if (len > 0) {
      if (odinBuf.length < idx + 1 + len) break;   // 负载未到齐
      const payload = odinBuf.slice(idx + 1, idx + 1 + len);
      odinBuf = odinBuf.slice(idx + 1 + len);
      send('odin:frame', { header, payload });
    } else {
      odinBuf = odinBuf.slice(idx + 1);
      send('odin:frame', { header, payload: null });
    }
  }
  if (odinBuf.length > 8 * 1024 * 1024) odinBuf = Buffer.alloc(0);   // 保护
}

function createWindow() {
  win = new BrowserWindow({
    width: 1680,
    height: 1020,
    title: 'T800 Pro 控制面板',
    backgroundColor: '#0d1117',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.on('closed', () => { win = null; });
}

ipcMain.handle('bridge:connect', async (ev, { host, port }) => {
  return new Promise((resolve) => {
    if (sock) { try { sock.destroy(); } catch (e) {} sock = null; }
    rxbuf = '';
    let settled = false;
    const s = net.createConnection({ host, port }, () => {
      sock = s;
      settled = true;
      send('bridge:status', { connected: true, host, port });
      resolve({ ok: true });
    });
    s.setNoDelay(true);
    s.on('data', (d) => {
      rxbuf += d.toString('utf8');
      let idx;
      while ((idx = rxbuf.indexOf('\n')) >= 0) {
        const line = rxbuf.slice(0, idx);
        rxbuf = rxbuf.slice(idx + 1);
        if (!line.trim()) continue;
        try { send('bridge:msg', JSON.parse(line)); } catch (e) {}
      }
      if (rxbuf.length > 4 * 1024 * 1024) rxbuf = '';
    });
    s.on('error', (err) => {
      const msg = String((err && err.message) || err);
      if (sock === s) sock = null;
      send('bridge:status', { connected: false, error: msg });
      if (!settled) { settled = true; resolve({ ok: false, error: msg }); }
    });
    s.on('close', () => {
      if (sock === s) sock = null;
      send('bridge:status', { connected: false });
    });
  });
});

ipcMain.handle('bridge:send', async (ev, obj) => {
  if (!sock) return { ok: false, error: '未连接' };
  try {
    sock.write(JSON.stringify(obj) + '\n');
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle('bridge:disconnect', async () => {
  if (sock) { try { sock.destroy(); } catch (e) {} sock = null; }
  send('bridge:status', { connected: false });
  return { ok: true };
});

/* ---------------- 雷达/相机（Odin2，端口 9802） ---------------- */

ipcMain.handle('odin:connect', async (ev, { host, port }) => {
  return new Promise((resolve) => {
    if (odinSock) { try { odinSock.destroy(); } catch (e) {} odinSock = null; }
    odinBuf = Buffer.alloc(0);
    let settled = false;
    const s = net.createConnection({ host, port }, () => {
      odinSock = s;
      settled = true;
      send('odin:status', { connected: true, host, port });
      resolve({ ok: true });
    });
    s.setNoDelay(true);
    s.on('data', handleOdinData);
    s.on('error', (err) => {
      const msg = String((err && err.message) || err);
      if (odinSock === s) odinSock = null;
      send('odin:status', { connected: false, error: msg });
      if (!settled) { settled = true; resolve({ ok: false, error: msg }); }
    });
    s.on('close', () => {
      if (odinSock === s) odinSock = null;
      send('odin:status', { connected: false });
    });
  });
});

ipcMain.handle('odin:send', async (ev, obj) => {
  if (!odinSock) return { ok: false, error: '未连接' };
  try {
    odinSock.write(JSON.stringify(obj) + '\n');
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle('odin:disconnect', async () => {
  if (odinSock) { try { odinSock.destroy(); } catch (e) {} odinSock = null; }
  send('odin:status', { connected: false });
  return { ok: true };
});

/* ---------------- 麦克风分贝（.162:9803，行分隔 JSON） ---------------- */

ipcMain.handle('mic:connect', async (ev, { host, port }) => {
  return new Promise((resolve) => {
    if (micSock) { try { micSock.destroy(); } catch (e) {} micSock = null; }
    micBuf = '';
    let settled = false;
    const s = net.createConnection({ host, port }, () => {
      micSock = s;
      settled = true;
      send('mic:status', { connected: true, host, port });
      resolve({ ok: true });
    });
    s.setNoDelay(true);
    s.on('data', (d) => {
      micBuf += d.toString('utf8');
      let idx;
      while ((idx = micBuf.indexOf('\n')) >= 0) {
        const line = micBuf.slice(0, idx);
        micBuf = micBuf.slice(idx + 1);
        if (!line.trim()) continue;
        try { send('mic:msg', JSON.parse(line)); } catch (e) {}
      }
      if (micBuf.length > 1024 * 1024) micBuf = '';
    });
    s.on('error', (err) => {
      const msg = String((err && err.message) || err);
      if (micSock === s) micSock = null;
      send('mic:status', { connected: false, error: msg });
      if (!settled) { settled = true; resolve({ ok: false, error: msg }); }
    });
    s.on('close', () => {
      if (micSock === s) micSock = null;
      send('mic:status', { connected: false });
    });
  });
});

ipcMain.handle('mic:disconnect', async () => {
  if (micSock) { try { micSock.destroy(); } catch (e) {} micSock = null; }
  send('mic:status', { connected: false });
  return { ok: true };
});

app.whenReady().then(createWindow);
app.on('window-all-closed', () => {
  if (sock) { try { sock.destroy(); } catch (e) {} }
  if (odinSock) { try { odinSock.destroy(); } catch (e) {} }
  if (micSock) { try { micSock.destroy(); } catch (e) {} }
  app.quit();
});
