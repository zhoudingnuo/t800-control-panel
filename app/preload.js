'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('bridge', {
  connect: (host, port) => ipcRenderer.invoke('bridge:connect', { host, port }),
  send: (obj) => ipcRenderer.invoke('bridge:send', obj),
  disconnect: () => ipcRenderer.invoke('bridge:disconnect'),
  onMessage: (cb) => ipcRenderer.on('bridge:msg', (_e, m) => cb(m)),
  onStatus: (cb) => ipcRenderer.on('bridge:status', (_e, s) => cb(s)),
});

// 雷达/相机流（Odin2）：frame = { header, payload(Uint8Array|null) }
contextBridge.exposeInMainWorld('odin', {
  connect: (host, port) => ipcRenderer.invoke('odin:connect', { host, port }),
  send: (obj) => ipcRenderer.invoke('odin:send', obj),
  disconnect: () => ipcRenderer.invoke('odin:disconnect'),
  onFrame: (cb) => ipcRenderer.on('odin:frame', (_e, f) => cb(f)),
  onStatus: (cb) => ipcRenderer.on('odin:status', (_e, s) => cb(s)),
});

// 麦克风分贝（.162:9803，行分隔 JSON）
contextBridge.exposeInMainWorld('mic', {
  connect: (host, port) => ipcRenderer.invoke('mic:connect', { host, port }),
  disconnect: () => ipcRenderer.invoke('mic:disconnect'),
  onMessage: (cb) => ipcRenderer.on('mic:msg', (_e, m) => cb(m)),
  onStatus: (cb) => ipcRenderer.on('mic:status', (_e, s) => cb(s)),
});
