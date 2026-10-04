#!/usr/bin/env python3
"""
T800 Pro ROS2 <-> TCP bridge（运行在机器人运控单元 .163 上）

把 ROS2 控制接口封装成一行一条 JSON 的 TCP 服务，供远端控制面板（Electron）使用。

协议（每行一个 JSON 对象，UTF-8）:
  服务端 -> 客户端:
    {"t":"hello", "joints":[{"i","name","lo","hi"}...], "rate": Hz, "slew_default": rad/s}
    {"t":"state", "mode":str, "avail":[str...], "enable":bool, "stream":bool,
                  "pos":[43], "vel":[43], "tau":[43], "tmo":[43], "tmos":[43],
                  "battery":{...}, "clients":int}
    # tmo/tmos = 电机/驱动MOS温度（℃，/hardware/motor_debug），未收到话题时为 null
    {"t":"ack",  "what":str}
    {"t":"log",  "level":"info|warn|error", "msg":str}
  客户端 -> 服务端:
    {"t":"targets", "pos":[43]}           # 更新目标角（自动限幅），受 stream 开关控制
    {"t":"stream",  "on":bool}            # 开启/停止向 /hardware/joint_command 持续下发
    {"t":"hold"}                          # 所有目标 = 当前实际位置
    {"t":"mode",    "name":str}           # 请求切换运动状态（/motion/set_motion_state 发一条）
    {"t":"gains",   "kp":[43], "kd":[43]} # 可选：修改每关节刚度/阻尼（0..2000 限幅）
    {"t":"slew",    "v":float}            # 目标角斜率限制（rad/s），默认 3.0
    {"t":"ping"}                          # -> {"t":"pong"}

安全行为:
  - 只有收到 {"t":"stream","on":true} 之后才会向 /hardware/joint_command 发布；
  - 目标角始终被限幅到 URDF 关节范围，并以 slew rad/s 的斜率逼近（无跳变）；
  - 客户端断开后默认继续以最后目标保持（joint_bridge 下机器人保持姿态），
    并在状态里标记 stream 不变；如需释放，请切模式（如 pd_stand）或按急停。
运行环境（.163 上）:
  source /opt/ros/humble/setup.bash
  source /apps/engineai_robotics/setup_board_ros_env.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp ROS_LOCALHOST_ONLY=0
  python3 bridge.py --port 9801 --rate 200
"""
import argparse
import json
import os
import socketserver
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import (QoSProfile, ReliabilityPolicy, DurabilityPolicy,
                       HistoryPolicy)
from interface_protocol.msg import (JointCommand, JointState, MotorDebug,
                                    MotionState, MotionStateRequest, PowerInfo)

BE = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.VOLATILE,
                history=HistoryPolicy.KEEP_LAST)
REL = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                 durability=DurabilityPolicy.VOLATILE,
                 history=HistoryPolicy.KEEP_LAST)

# 官方 config/t800pro/joint_test.yaml 的组别增益（手部阻尼按 loose_fist 实测值 0.2）
# 注意：JointCommand 的 float64[] setter 断言每个元素必须是 float（int 会被拒绝）
KP = [float(x) for x in (
      [1080, 480, 880, 1000, 800, 100] * 2          # 腿 J00-J11
      + [200]                                        # 腰 J12
      + [120, 120, 120, 120, 120] + [30, 30]         # 左臂 J13-17 + 左腕 J18-19
      + [20] * 7                                     # 左手 J20-26
      + [120, 120, 120, 120, 120] + [30, 30]         # 右臂 J27-31 + 右腕 J32-33
      + [20] * 7                                     # 右手 J34-40
      + [100, 100])]                                 # 头 J41-42
KD = [float(x) for x in (
      [25, 25, 25, 25, 2, 2] * 2
      + [1]
      + [1.8, 1.5, 1.5, 1.8, 1.2] + [0.8, 0.8]
      + [0.2] * 7
      + [1.8, 1.5, 1.5, 1.8, 1.2] + [0.8, 0.8]
      + [0.2] * 7
      + [1, 1])]

DEFAULT_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "t800pro_joints.json")


class Client:
    """线程安全的一行一 JSON 发送包装。"""
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.lock = threading.Lock()

    def send(self, data: bytes):
        with self.lock:
            self.sock.sendall(data)


class Bridge(Node):
    def __init__(self, joints, rate, slew, port):
        super().__init__('t800_control_bridge')
        self.joints = joints
        self.n = len(joints)
        assert self.n == 43, "expect 43 joints, got %d" % self.n
        self.lo = [j['lo'] for j in joints]
        self.hi = [j['hi'] for j in joints]
        self.rate = rate
        self.slew = slew
        self.port = port

        self.lock = threading.Lock()
        self.pos = [0.0] * self.n
        self.vel = [0.0] * self.n
        self.tau = [0.0] * self.n
        self.tmo = None   # 电机温度 [n]，None = 尚未收到 motor_debug
        self.tmos = None  # 驱动 MOS 温度 [n]
        self.have_state = False
        self.mode = ""
        self.avail = []
        self.enable = False
        self.battery = {}
        self.stream = False
        self.user_target = [0.0] * self.n
        self.cmd_target = [0.0] * self.n
        self.kp = list(KP)
        self.kd = list(KD)
        self._last_tick = time.time()
        self._tcp_clients = []

        self.create_subscription(JointState, '/hardware/joint_state', self.on_js, BE)
        self.create_subscription(MotorDebug, '/hardware/motor_debug', self.on_md, BE)
        self.create_subscription(MotionState, '/motion/motion_state', self.on_ms, BE)
        self.create_subscription(PowerInfo, '/hardware/power_info', self.on_pi, BE)
        self.pub_cmd = self.create_publisher(JointCommand, '/hardware/joint_command', BE)
        self.pub_mode = self.create_publisher(MotionStateRequest,
                                              '/motion/set_motion_state', REL)

        self.create_timer(1.0 / self.rate, self.tick)
        self.create_timer(0.05, self.broadcast_state)   # 20 Hz
        self.log('info', 'bridge started: rate=%d Hz, slew=%.2f rad/s, port=%d'
                 % (rate, slew, port))

    # ---------- ROS2 callbacks ----------
    def on_js(self, msg):
        if len(msg.position) != self.n:
            return
        with self.lock:
            first = not self.have_state
            self.pos = list(msg.position)
            self.vel = list(msg.velocity)
            self.tau = list(msg.torque)
            self.have_state = True
            if first:
                self.user_target = list(self.pos)
                self.cmd_target = list(self.pos)

    def on_md(self, msg):
        # 部分电机（如未装配的手指）可能不上报，长度不足时按 0 占位，保证与关节一一对应
        def fit(arr):
            a = [float(x) for x in (arr or [])[:self.n]]
            return a + [0.0] * (self.n - len(a))
        with self.lock:
            self.tmo = fit(msg.motor_temperature)
            self.tmos = fit(msg.mos_temperature)

    def on_ms(self, msg):
        with self.lock:
            self.mode = msg.current_motion_task
            self.avail = list(msg.available_transition_motions)

    def on_pi(self, msg):
        with self.lock:
            self.enable = bool(msg.enable)
            self.battery = {
                "pct": float(msg.percentage),
                "volt": float(msg.voltage),
                "current": float(msg.current),
                "limit": float(msg.current_limit),
                "err": int(msg.error_code),
            }

    # ---------- command streaming ----------
    def tick(self):
        now = time.time()
        dt = now - self._last_tick
        self._last_tick = now
        with self.lock:
            if not self.stream or not self.have_state:
                return
            step = self.slew * dt
            for i in range(self.n):
                d = self.user_target[i] - self.cmd_target[i]
                if d > step:
                    self.cmd_target[i] += step
                elif d < -step:
                    self.cmd_target[i] -= step
                else:
                    self.cmd_target[i] = self.user_target[i]
            cmd = JointCommand()
            cmd.header.stamp = self.get_clock().now().to_msg()
            cmd.position = list(self.cmd_target)
            cmd.velocity = [0.0] * self.n
            cmd.feed_forward_torque = [0.0] * self.n
            cmd.torque = [0.0] * self.n
            cmd.stiffness = list(self.kp)
            cmd.damping = list(self.kd)
            cmd.parallel_parser_type = 0  # CLASSIC_PARSER
        self.pub_cmd.publish(cmd)

    # ---------- TCP ----------
    def log(self, level, msg):
        print("[%s] %s" % (level, msg), flush=True)
        self.broadcast({"t": "log", "level": level, "msg": msg})

    def add_client(self, c):
        with self.lock:
            self._tcp_clients.append(c)

    def remove_client(self, c):
        with self.lock:
            if c in self._tcp_clients:
                self._tcp_clients.remove(c)

    def broadcast(self, obj):
        data = (json.dumps(obj, separators=(',', ':')) + "\n").encode()
        with self.lock:
            clients = list(self._tcp_clients)
        for c in clients:
            try:
                c.send(data)
            except Exception:
                pass

    def broadcast_state(self):
        with self.lock:
            obj = {"t": "state", "mode": self.mode, "avail": self.avail,
                   "enable": self.enable, "stream": self.stream,
                   "battery": self.battery,
                   "pos": [round(v, 4) for v in self.pos],
                   "vel": [round(v, 3) for v in self.vel],
                   "tau": [round(v, 2) for v in self.tau],
                   "tmo": [round(v, 1) for v in self.tmo] if self.tmo else None,
                   "tmos": [round(v, 1) for v in self.tmos] if self.tmos else None,
                   "clients": len(self._tcp_clients)}
        self.broadcast(obj)

    def handle_command(self, msg):
        t = msg.get("t")
        with self.lock:
            if t == "targets":
                pos = msg.get("pos") or []
                if len(pos) != self.n:
                    return "targets: expect %d values" % self.n
                for i, v in enumerate(pos):
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    self.user_target[i] = min(max(fv, self.lo[i]), self.hi[i])
                return None
            if t == "stream":
                on = bool(msg.get("on"))
                if on and not self.have_state:
                    return "no joint_state yet, cannot stream"
                self.stream = on
                return None
            if t == "hold":
                if self.have_state:
                    self.user_target = list(self.pos)
                return None
            if t == "mode":
                req = MotionStateRequest()
                req.target_motion_name = str(msg.get("name", ""))
                self.pub_mode.publish(req)
                return None
            if t == "gains":
                kp = msg.get("kp")
                kd = msg.get("kd")
                if kp and len(kp) == self.n:
                    self.kp = [min(max(float(x), 0.0), 2000.0) for x in kp]
                if kd and len(kd) == self.n:
                    self.kd = [min(max(float(x), 0.0), 2000.0) for x in kd]
                return None
            if t == "slew":
                self.slew = min(max(float(msg.get("v", 3.0)), 0.05), 20.0)
                return None
            return "unknown command: %r" % t


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        bridge = self.server.bridge
        client = Client(self.request, self.client_address)
        bridge.add_client(client)
        hello = {"t": "hello", "joints": bridge.joints,
                 "rate": bridge.rate, "slew_default": bridge.slew,
                 "kp": bridge.kp, "kd": bridge.kd}
        client.send((json.dumps(hello, separators=(',', ':')) + "\n").encode())
        bridge.log('info', 'client connected: %s' % (self.client_address,))
        try:
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    bridge.log('warn', 'bad json from client')
                    continue
                if msg.get("t") == "ping":
                    client.send(b'{"t":"pong"}\n')
                    continue
                err = bridge.handle_command(msg)
                if err:
                    bridge.log('warn', err)
                else:
                    client.send((json.dumps({"t": "ack", "what": msg.get("t")},
                                            separators=(',', ':')) + "\n").encode())
        except Exception as e:
            bridge.log('warn', 'client error: %s' % e)
        finally:
            bridge.remove_client(client)
            bridge.log('warn', 'client disconnected; holding last targets '
                               '(stream=%s)' % bridge.stream)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=9801)
    ap.add_argument('--rate', type=int, default=200, help='joint_command 下发频率 Hz')
    ap.add_argument('--slew', type=float, default=3.0, help='目标角斜率 rad/s')
    ap.add_argument('--model', default=DEFAULT_MODEL)
    args = ap.parse_args()

    with open(args.model) as f:
        joints = json.load(f)
    joints = sorted(joints, key=lambda j: j['i'])

    rclpy.init()
    bridge = Bridge(joints, args.rate, args.slew, args.port)

    srv = Server(('0.0.0.0', args.port), Handler)
    srv.bridge = bridge
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        rclpy.spin(bridge)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
