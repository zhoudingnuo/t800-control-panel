#!/bin/bash
# T800 Pro 控制面板启动器（桌面快捷方式入口）
# 单实例：面板已在跑则不重复启动（避免两个面板同时下发）
cd "$(dirname "$0")"
if pgrep -f "node_modules/electron/dist/[e]lectron \." >/dev/null 2>&1; then
  echo "[$(date +%T)] panel already running, skip" >> /tmp/t800_panel.log
  exit 0
fi
exec ./node_modules/.bin/electron . >> /tmp/t800_panel.log 2>&1
