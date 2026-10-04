#!/usr/bin/env python3
"""
手指"虚握拳 → 张开"示例（走 robot_control 的 TCP bridge，可从本机运行）

前置：机器人处于 joint_bridge 模式、电机已使能，bridge+relay 已启动。
用法：
  python3 finger_demo.py                # 默认连 192.168.1.50:9801
  python3 finger_demo.py --host 127.0.0.1 --port 9801
"""
import argparse
import json
import socket
import time

LEFT = list(range(20, 27))
RIGHT = list(range(34, 41))
FIST = [-0.85, 0.62, 0.48, -0.80, -0.58, -0.80, -0.58,
         0.85, 0.62, 0.48,  0.80,  0.58,  0.80,  0.58]
IDX = LEFT + RIGHT


class BridgeClient:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=8)
        self.s.setblocking(False)
        self.buf = b''
        self.state = None

    def send(self, obj):
        self.s.sendall((json.dumps(obj) + '\n').encode())

    def pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            try:
                d = self.s.recv(65536)
                if not d:
                    raise SystemExit('bridge closed connection')
                self.buf += d
            except BlockingIOError:
                time.sleep(0.01)
                continue
            while b'\n' in self.buf:
                line, self.buf = self.buf.split(b'\n', 1)
                if not line.strip():
                    continue
                m = json.loads(line)
                if m['t'] == 'state':
                    self.state = m
                elif m['t'] == 'log':
                    print('  [bridge]', m['msg'])
        return self.state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='192.168.1.50')
    ap.add_argument('--port', type=int, default=9801)
    args = ap.parse_args()

    c = BridgeClient(args.host, args.port)
    st = None
    t0 = time.time()
    while time.time() - t0 < 5 and st is None:
        st = c.pump(0.3)
    print('模式: %s | 电机: %s | 可用切换: %s'
          % (st['mode'], st['enable'], st['avail']))
    if st['mode'] != 'joint_bridge':
        print('请先切到 joint_bridge（面板的「可切换目标」或 set_motion_state）再运行本示例')
        raise SystemExit(1)

    # 开始下发：目标 = 当前角度（保持）
    c.send({'t': 'hold'})
    c.pump(0.2)
    base = list(st['pos'])
    c.send({'t': 'targets', 'pos': base})
    c.send({'t': 'stream', 'on': True})
    c.pump(0.5)
    print('下发已开启，开始手指动作')

    # 握拳
    tgt = list(base)
    for k, i in enumerate(IDX):
        tgt[i] = FIST[k]
    c.send({'t': 'targets', 'pos': tgt})
    time.sleep(2.0)
    st = c.pump(0.2)
    print('握拳: L=%s R=%s'
          % ([round(st['pos'][i], 2) for i in LEFT],
             [round(st['pos'][i], 2) for i in RIGHT]))
    time.sleep(1.0)

    # 张开
    c.send({'t': 'targets', 'pos': base})
    time.sleep(2.0)
    st = c.pump(0.2)
    print('张开: L=%s R=%s'
          % ([round(st['pos'][i], 2) for i in LEFT],
             [round(st['pos'][i], 2) for i in RIGHT]))

    # 保持当前，停止下发
    c.send({'t': 'hold'})
    c.pump(0.3)
    c.send({'t': 'stream', 'on': False})
    print('完成，已停止下发（机器人保持最后姿态）')


if __name__ == '__main__':
    main()
