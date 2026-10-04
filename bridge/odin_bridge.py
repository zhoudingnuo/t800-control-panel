#!/usr/bin/env python3
"""
T800 Pro Odin2 雷达相机 ROS2 <-> TCP bridge（运行在机器人应用计算单元 .162 上）

把 Odin2 驱动的点云与压缩相机图像推给远端面板（robot_control/app）。

运行环境（.162 上，注意：驱动与设备同网段，只能在 .162 跑）:
  export ROS_DOMAIN_ID=69 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  source /opt/ros/humble/setup.bash
  source ~/odin_latest/install/setup.bash        # 厂商驱动（系统自带的 2.0.3 发现不了设备，见笔记 06）
  python3 odin_bridge.py --port 9802

协议（TCP，混合文本头 + 二进制负载）:
  服务端 -> 客户端（每帧 = 一行 JSON 头 + 紧随其后的 len 字节负载）:
    {"t":"hello","proto":1,"cloud_topic":...,"cam0_topic":...,"cam1_topic":...,
     "stride":4,"cloud_hz":10,"image_hz":15,"cloud":true,"images":true}
    {"t":"status","cloud_hz":..,"cam0_hz":..,"cam1_hz":..,"clients":..,"points":..,
     "cloud_on":..,"images_on":..}                       # 1Hz 广播
    {"t":"cloud","n":<点数>,"stride":..,"len":<字节>,"frame_id":"..","stamp":<秒>}\n + f4 xyz 负载
    {"t":"image","cam":0|1,"fmt":"jpeg","len":<字节>,"stamp":<秒>}\n + JPEG 负载
  客户端 -> 服务端（行分隔 JSON）:
    {"t":"cfg","cloud":bool,"images":bool,"stride":int(1..16),"cloud_hz":float,"image_hz":float}
    {"t":"ping"}                                          # -> {"t":"pong"}

负载说明:
  - 点云只发有效点（过滤 NaN 与原点），按 stride 抽稀后转 float32 xyz（雷达系：x 前 / y 左 / z 上）；
  - 图像直接转发设备 JPEG（cameraX/compressed），客户端自行解码。
"""
import argparse
import json
import socket
import socketserver
import threading
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import (QoSProfile, ReliabilityPolicy, DurabilityPolicy,
                       HistoryPolicy)
from sensor_msgs.msg import PointCloud2, CompressedImage

# 驱动发布端 QoS：RELIABLE / KEEP_LAST(5) / VOLATILE（ros2 topic info -v 实测）
QOS = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE,
                 durability=DurabilityPolicy.VOLATILE,
                 history=HistoryPolicy.KEEP_LAST)


class OdinBridge(Node):
    def __init__(self, args):
        super().__init__('t800_odin_bridge')
        self.args = args
        self.lock = threading.Lock()

        # 最新帧缓存
        self.cloud_xyz = None          # np.float32 (N,3)，已过滤
        self.cloud_frame_id = ''
        self.cloud_stamp = 0.0
        self.cloud_seq = 0
        self.images = {0: None, 1: None}     # cam -> jpeg bytes
        self.image_stamp = {0: 0.0, 1: 0.0}

        # 统计（1s 窗口）
        self.cnt = {'cloud': 0, 'cam0': 0, 'cam1': 0}
        self.hz = {'cloud': 0.0, 'cam0': 0.0, 'cam1': 0.0}
        self._last_stat = time.time()

        # 客户端可动态改的发送配置（全局，单面板使用）
        self.cfg = {'cloud': True, 'images': True,
                    'stride': args.stride, 'cloud_hz': args.cloud_hz,
                    'image_hz': args.image_hz}
        self.tcp_clients = []

        self.create_subscription(PointCloud2, args.cloud_topic, self.on_cloud, QOS)
        self.create_subscription(CompressedImage, args.cam0_topic,
                                 lambda m: self.on_image(m, 0), QOS)
        self.create_subscription(CompressedImage, args.cam1_topic,
                                 lambda m: self.on_image(m, 1), QOS)
        self.create_timer(1.0, self.on_stat)
        self.get_logger().info('odin bridge node up: cloud=%s cam0=%s cam1=%s'
                               % (args.cloud_topic, args.cam0_topic, args.cam1_topic))

    # ---------- ROS2 回调 ----------
    def on_cloud(self, msg: PointCloud2):
        try:
            raw = np.frombuffer(msg.data, dtype=np.uint8)
            ps = msg.point_step
            n = raw.size // ps
            if n <= 0:
                return
            arr = raw[:n * ps].reshape(n, ps)
            # fields: x@0 f4, y@4 f4, z@8 f4（其余 intensity/confidence/offset_time 不用）
            xyz = arr[:, :12].copy().view('<f4').reshape(-1, 3)
            mask = np.isfinite(xyz).all(axis=1)
            if self.args.drop_zero:
                mask &= np.abs(xyz).sum(axis=1) > 1e-6
            pts = xyz[mask]
            with self.lock:
                self.cloud_xyz = pts
                self.cloud_frame_id = msg.header.frame_id
                self.cloud_stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
                self.cloud_seq = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
                self.cnt['cloud'] += 1
        except Exception as e:      # 单帧坏数据不该拖垮整个 bridge
            self.get_logger().warn('cloud parse failed: %s' % e, throttle_duration_sec=5.0)

    def on_image(self, msg: CompressedImage, cam: int):
        try:
            data = bytes(msg.data)
        except Exception:
            return
        with self.lock:
            self.images[cam] = data
            self.image_stamp[cam] = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
            self.cnt['cam%d' % cam] += 1

    def on_stat(self):
        now = time.time()
        dt = max(1e-6, now - self._last_stat)
        with self.lock:
            for k in self.cnt:
                self.hz[k] = self.cnt[k] / dt
                self.cnt[k] = 0
        self._last_stat = now

    def snapshot_cloud(self, stride):
        """返回 (header_extra, payload) 或 (None, None)"""
        with self.lock:
            pts = self.cloud_xyz
            fid, stamp = self.cloud_frame_id, self.cloud_stamp
        if pts is None or len(pts) == 0:
            return None, None
        if stride > 1:
            pts = pts[::stride]
        if self.args.max_points > 0 and len(pts) > self.args.max_points:
            pts = pts[:self.args.max_points]
        payload = np.ascontiguousarray(pts, dtype='<f4').tobytes()
        extra = {'n': int(len(pts)), 'stride': int(stride), 'len': len(payload),
                 'frame_id': fid, 'stamp': round(stamp, 6)}
        return extra, payload

    def snapshot_image(self, cam):
        with self.lock:
            data = self.images.get(cam)
            stamp = self.image_stamp.get(cam, 0.0)
        if not data:
            return None, None
        return {'cam': cam, 'fmt': 'jpeg', 'len': len(data),
                'stamp': round(stamp, 6)}, data

    def set_cfg(self, msg):
        with self.lock:
            for k, cast in (('cloud', bool), ('images', bool),
                            ('stride', int), ('cloud_hz', float), ('image_hz', float)):
                if k in msg:
                    try:
                        self.cfg[k] = cast(msg[k])
                    except (TypeError, ValueError):
                        pass
            self.cfg['stride'] = min(max(self.cfg['stride'], 1), 16)
            self.cfg['cloud_hz'] = min(max(self.cfg['cloud_hz'], 0.5), 20.0)
            self.cfg['image_hz'] = min(max(self.cfg['image_hz'], 0.5), 30.0)
        return dict(self.cfg)


class Client:
    def __init__(self, sock, addr, bridge: OdinBridge):
        self.sock = sock
        self.addr = addr
        self.bridge = bridge
        self.dead = False
        self.send_lock = threading.Lock()
        self.last = {'cloud': 0.0, 'cam0': 0.0, 'cam1': 0.0}
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send_frame(self, header: dict, payload: bytes | None = None):
        if self.dead:
            return False
        blob = json.dumps(header, separators=(',', ':')).encode() + b'\n'
        if payload:
            blob += payload
        try:
            with self.send_lock:
                self.sock.settimeout(2.0)
                self.sock.sendall(blob)
            return True
        except OSError as e:
            print('[bridge] client %s send failed: %s -> drop' % (self.addr, e), flush=True)
            self.dead = True
            return False

    def send_line(self, obj):
        return self.send_frame(obj, None)

    def pump(self):
        """按配置的速率推送最新帧（丢帧策略：只发最新）"""
        while not self.dead:
            cfg = dict(self.bridge.cfg)
            now = time.time()
            if cfg['cloud'] and now - self.last['cloud'] >= 1.0 / cfg['cloud_hz']:
                self.last['cloud'] = now
                extra, payload = self.bridge.snapshot_cloud(cfg['stride'])
                if extra:
                    self.send_frame({'t': 'cloud', **extra}, payload)
            if cfg['images']:
                for cam in (0, 1):
                    key = 'cam%d' % cam
                    if now - self.last[key] >= 1.0 / cfg['image_hz']:
                        self.last[key] = now
                        extra, payload = self.bridge.snapshot_image(cam)
                        if extra:
                            self.send_frame({'t': 'image', **extra}, payload)
            time.sleep(0.01)
        try:
            self.sock.close()
        except OSError:
            pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        br: OdinBridge = self.server.bridge
        c = Client(self.request, self.client_address, br)
        br.tcp_clients.append(c)
        print('[bridge] client connected: %s (total %d)'
              % (self.client_address, len(br.tcp_clients)), flush=True)
        c.send_frame({'t': 'hello', 'proto': 1,
                      'cloud_topic': br.args.cloud_topic,
                      'cam0_topic': br.args.cam0_topic,
                      'cam1_topic': br.args.cam1_topic,
                      **{k: br.cfg[k] for k in ('stride', 'cloud_hz', 'image_hz',
                                                'cloud', 'images')}})
        threading.Thread(target=c.pump, daemon=True).start()
        # 读客户端命令（行分隔 JSON）。注意：发送线程会给 socket 设 2s 超时，
        # 这里必须显式吃掉 socket.timeout（OSError 子类），否则空闲 2s 即断连。
        buf = b''
        try:
            while not c.dead:
                try:
                    data = self.request.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = msg.get('t')
                    if t == 'ping':
                        c.send_line({'t': 'pong'})
                    elif t == 'cfg':
                        cfg = br.set_cfg(msg)
                        c.send_line({'t': 'cfg_ok', **cfg})
                        print('[bridge] cfg <- %s: %s' % (self.client_address, cfg), flush=True)
        except OSError:
            pass
        finally:
            c.dead = True
            try:
                br.tcp_clients.remove(c)
            except ValueError:
                pass
            print('[bridge] client gone: %s (total %d)'
                  % (self.client_address, len(br.tcp_clients)), flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def stat_loop(br: OdinBridge):
    while True:
        time.sleep(1.0)
        if not br.tcp_clients:
            continue
        with br.lock:
            st = {'t': 'status', 'clients': len(br.tcp_clients),
                  'cloud_hz': round(br.hz['cloud'], 2),
                  'cam0_hz': round(br.hz['cam0'], 2),
                  'cam1_hz': round(br.hz['cam1'], 2),
                  'points': int(len(br.cloud_xyz)) if br.cloud_xyz is not None else 0,
                  'cloud_on': br.cfg['cloud'], 'images_on': br.cfg['images'],
                  'stride': br.cfg['stride']}
        for c in list(br.tcp_clients):
            c.send_line(st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=9802)
    ap.add_argument('--cloud-topic', default='/manifold/ODIN2/device0/cloud/raw')
    ap.add_argument('--cam0-topic', default='/manifold/ODIN2/device0/camera0/compressed')
    ap.add_argument('--cam1-topic', default='/manifold/ODIN2/device0/camera1/compressed')
    ap.add_argument('--stride', type=int, default=4, help='点云抽稀步长（1..16，4≈1.2万点/帧）')
    ap.add_argument('--cloud-hz', type=float, default=10.0)
    ap.add_argument('--image-hz', type=float, default=15.0)
    ap.add_argument('--max-points', type=int, default=20000)
    ap.add_argument('--drop-zero', action='store_true', default=True,
                    help='过滤 (0,0,0) 无效点（默认开）')
    args = ap.parse_args()

    rclpy.init()
    br = OdinBridge(args)
    srv = Server((args.host, args.port), Handler)
    srv.bridge = br
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=stat_loop, args=(br,), daemon=True).start()
    print('[bridge] listening on %s:%d' % (args.host, args.port), flush=True)

    try:
        rclpy.spin(br)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
