# T800 Pro 控制面板（Electron）：全关节控制 + 雷达/相机查看

本机跑图形面板，经 **Orin 中转** 连到机器人上的两个 ROS2 bridge：

- **关节控制**（.163 运控单元，端口 9801）：43 关节实时控制，2026-10-04 实机验证通过；
- **雷达/相机**（.162 应用单元，端口 9802）：Odin2 激光雷达相机点云 + 双目图像实时查看，同日实机验证通过。

```
desk 本机                        Orin 192.168.1.50              机器人（内网 192.168.0.0/24）
┌────────────────────┐   TCP    ┌─────────────────┐   TCP(WiFi) ┌────────────────────────────┐
│ app/  Electron 面板 │ ─:9801──▶│ relay/relay.py  │────────────▶│ .163 bridge/bridge.py      │
│  「关节控制」页      │          │                 │  :9801      │   /hardware/*  /motion/*   │
│  「雷达/相机」页     │ ─:9802──▶│ (同一段代码,     │────────────▶│ .162 bridge/odin_bridge.py │
│   点云 + 双目图像    │          │  跑两个实例)     │  :9802      │   /manifold/ODIN2/*        │
└────────────────────┘          └─────────────────┘             └────────────────────────────┘
```

## 目录

| 路径 | 说明 |
|---|---|
| `bridge/bridge.py` | 机器人侧 ROS2↔TCP bridge（Python + rclpy，运行在 .163） |
| `bridge/odin_bridge.py` | Odin2 雷达相机 ROS2↔TCP bridge（运行在 **.162**，端口 9802；点云+JPEG 推流） |
| `bridge/mic_bridge.py` | 麦克风 bridge（运行在 **.162**，端口 9803；环麦采集 → 分贝 10Hz 推送 + 能量 VAD 分句 → funasr 识别文本推送，面板顶栏 🎙 表与识别 pill） |
| `bridge/t800pro_joints.json` | 43 关节名称与 URDF 限位（bridge 加载，面板也内置一份） |
| `bridge/start_bridge.sh` | 关节 bridge 启动脚本（含 ROS 环境） |
| `relay/relay.py` | Orin 侧 TCP 中转（纯标准库，无依赖；9801/9802 各跑一个实例） |
| `app/` | Electron 面板（main 进程持 TCP；关节页 43 滑杆 + 雷达页点云/图像渲染） |
| `model/t800pro_joints.json` | 从机器人 SDK 的 URDF 解析出的关节模型（生成源） |
| `examples/finger_demo.py` | 手指握拳/张开示例（经 bridge 的第三方客户端样例） |

## 启动（三步）

```bash
# 1) 机器人侧 bridge（.163）—— 已部署在 ~/t800_bridge/
sshpass -p <运控密码> ssh -J orin user@192.168.0.163 'cd ~/t800_bridge && setsid nohup ./start_bridge.sh > bridge.log 2>&1 < /dev/null &'
#    更新代码：scp bridge/bridge.py + model/t800pro_joints.json 到 ~/t800_bridge/ 后重启

# 2) Orin 中转（本仓库 relay/relay.py 已部署到 ~/t800_relay.py）
scp relay/relay.py orin:~/t800_relay.py
ssh orin 'setsid nohup python3 ~/t800_relay.py --listen 192.168.1.50 --port 9801 --target 192.168.0.163 --target-port 9801 > ~/t800_relay.log 2>&1 < /dev/null &'

# 3) 本机面板
cd app && npm start        # 已装 electron（ELECTRON_MIRROR 用 npmmirror）
```

失败的常见原因：bridge 没起（看 .163 上 `~/t800_bridge/bridge.log`）、relay 没起（`~/t800_relay.log`）、
或机器人 WiFi 掉线（Orin `nmcli` 看 `<机器人WiFi-SSID>`）。

## 雷达 / 相机查看（Odin2，端口 9802）

前置：Odin2 驱动必须在 **.162** 上用**厂商工作区** `~/odin_latest` 启动（系统自带的
engineai 打包驱动发现不了设备，详见 `docs/notes/06-T800雷达与深度相机Odin2.md`）：

```bash
# 1) Odin2 驱动（.162，若已在跑则跳过）—— 已部署 ~/odin_vendor_driver.log 为日志
ssh orin 'sshpass -p <应用单元密码> ssh ubuntu@192.168.0.162 bash -s' <<'EOF'
export ROS_DOMAIN_ID=69 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
source /opt/ros/humble/setup.bash; source ~/odin_latest/install/setup.bash
pgrep -f odin_ros_driver_node >/dev/null || \
  nohup setsid ros2 launch odin_ros_driver_rev1 driver.launch.py start_rviz:=false \
    > ~/odin_vendor_driver.log 2>&1 < /dev/null &
EOF

# 2) Odin bridge（.162）—— 已部署在 ~/t800_odin/
cat bridge/odin_bridge.py | ssh orin 'sshpass -p <应用单元密码> ssh ubuntu@192.168.0.162 "cat > ~/t800_odin/odin_bridge.py"'
ssh orin 'sshpass -p <应用单元密码> ssh ubuntu@192.168.0.162 bash -s' <<'EOF'
export ROS_DOMAIN_ID=69 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
source /opt/ros/humble/setup.bash; source ~/odin_latest/install/setup.bash
nohup setsid python3 ~/t800_odin/odin_bridge.py --port 9802 > ~/t800_odin/odin_bridge.log 2>&1 < /dev/null &
EOF

# 3) Orin 第二条中转（9802）
ssh orin 'setsid nohup python3 ~/t800_relay.py --listen 192.168.1.50 --port 9802 --target 192.168.0.162 --target-port 9802 > ~/t800_odin_relay.log 2>&1 < /dev/null &'

# 3b) 麦克风分贝（.162，9803）—— 已部署 ~/t800_mic/；Orin 中转 9803
cat bridge/mic_bridge.py | ssh orin 'sshpass -p <应用单元密码> ssh ubuntu@192.168.0.162 "cat > ~/t800_mic/mic_bridge.py"'
ssh orin 'sshpass -p <应用单元密码> ssh ubuntu@192.168.0.162 bash -s' <<'EOF'
nohup setsid python3 ~/t800_mic/mic_bridge.py --port 9803 > ~/t800_mic/mic_bridge.log 2>&1 < /dev/null &
EOF
ssh orin 'setsid nohup python3 ~/t800_relay.py --listen 192.168.1.50 --port 9803 --target 192.168.0.162 --target-port 9803 > ~/t800_mic_relay.log 2>&1 < /dev/null &'

# 4) 本机面板 → 顶部「雷达 / 相机」页：打开即自动连接（断线 3s 自动重连），无需手点
cd app && npm start
```

页面功能：左侧点云 3D 视图（拖动旋转 / 滚轮缩放 / 双击复位，按距离或高度着色，1m 网格 + XYZ 轴），
右侧相机 0/1 实时 JPEG（640×544）。工具条可调点云抽稀（1/2~1/8）、点云/图像帧率、着色方式、暂停渲染。

### 顶栏麦克风 + 语音识别（9803）

- 🎙 **分贝表**：环麦（AIUI，经 PulseAudio default 源）16kHz 采集 → 100ms RMS → dBFS，10Hz；
  电平条颜色 < -35 绿 / -35~-15 黄 / > -15 红，白色刻度为峰值保持（3dB/s 衰减）。
- **语音识别**：能量 VAD（门限 -45dBFS，静音 1.2s 收尾一句）→ 本机出厂 FunASR 容器
  （`engineai-asr`，ws://127.0.0.1:10095，SenseVoiceSmall 离线模型）→ 识别文本推给面板；
  顶栏 pill 显示当前句（final 后 8s 自动隐藏），final 追加到「日志」卡。
  注意 SenseVoice 返回带 `<|zh|><|Speech|>` 等标签，桥内已剥离；它无流式 partial，整句出结果。
- 协议补充消息：`{"t":"asr","text":..,"final":bool}`；hello 里 `asr:true` 表示识别已启用。

点云规格（2026-10-04 实测）：**192 线 × 256 点 = 49152 点/帧 @10Hz**，有效点约 70%（3.5 万/帧），
FOV 水平 133° × 垂直 86°，每点含 x/y/z + intensity + confidence + **offset_time（逐点时间，0~94ms）**。

TCP 协议（区别于 9801 的纯 JSON：这里是 **JSON 头行 + 定长二进制负载**）：

- 服务端→客户端：`hello`/`status`(1Hz)/`cloud`（头行 `{"t":"cloud","n":..,"len":..}` + f32 xyz 负载）、
  `image`（头行 + JPEG 负载，直接转发设备 `cameraX/compressed`）
- 客户端→服务端：`{"t":"cfg","stride":4,"cloud_hz":10,"image_hz":15,"cloud":true,"images":true}`、`{"t":"ping"}`

## 面板用法

1. 启动后自动连接（默认 `192.168.1.50:9801`），断开时会自动重连。
2. **前置条件**：机器人电机已使能（急停遥控器）。
   点顶栏 **「进入关节控制」**（两步确认：第一次点变红提示，5 秒内再点一次执行）自动逐级
   切换运动状态直到 `joint_bridge`——每步只从机器人实时下发的可用目标里挑
   （偏好 joint_bridge > pd_stand > lower_body_balance > rl_basic > passive），
   10s 超时自动重试一次；切换中再点一次取消，紧急停止/Esc 中断。
   也可在右侧栏「可切换目标」手动逐级点（如 `lower_body_balance → rl_basic → pd_stand → joint_bridge`）。
3. 点 **「开始发送」**：面板先把目标对齐当前角度并开始 200Hz 下发；此后拖动任意滑杆即控制对应关节。
   - 未拖动的关节**始终跟随当前角度**（不会把陈旧目标发下去）。
   - 目标角按 `slew`（默认 3 rad/s）平滑逼近，不会跳变；目标自动限幅到 URDF 关节范围。
   - 每行 `⟲` 按钮 = 该关节目标回到当前角度。
4. **「全部跟随当前」**：所有目标对齐当前实际角度（等价"就地保持"）。
5. **「紧急停止」/ Esc 键**：立即停止下发（joint_bridge 下机器人保持最后姿态）。
6. 增益可按组修改（默认取自官方 `joint_test.yaml`：腿 1080/25、臂 120/1.8、腕 30/1、手 20/0.2、腰 200/1、头 100/1）。
7. 退出时建议：点「停止发送」或把模式切到 `pd_stand`/`passive`。

## 安全须知（真机）

- 首次使用务必**吊架吊起**、周围清场，先小幅单关节验证；`joint_bridge` 模式下**全身关节都在面板控制下**。
- 面板拒绝在非 `joint_bridge` 模式或电机未使能时开启下发。
- 关掉面板/网络断开后，bridge 会**继续以最后目标保持**；要释放请切模式或按急停。

## TCP 协议（行分隔 JSON）

服务端→客户端：`hello`（关节模型+增益+rate）、`state`（20Hz：mode/avail/enable/stream/battery/pos/vel/tau/tmo/tmos）、`ack`、`log`
- `battery`：`{pct, volt, current, limit, err}`（来自 `/hardware/power_info`，面板顶部电量图标实时显示）
- `tau`：43 个关节的力矩反馈 (N·m)，面板每个关节行显示（|τ|≥25 变黄、≥60 变红），侧栏显示"最大关节力矩"
- `tmo`/`tmos`：43 个关节的电机/驱动 MOS 温度（℃，来自 `/hardware/motor_debug`，未收到话题时为 null），
  面板每个关节行力矩旁显示电机温度（≥60℃ 变黄、≥80℃ 变红，悬停看 MOS 温度），侧栏显示"最高电机温度"
客户端→服务端：`targets`(pos[43])、`stream`(on)、`hold`、`mode`(name)、`gains`(kp/kd[43])、`slew`(v)、`ping`

## 实机调试记录（2026-10-04）

| 问题 | 根因 | 修复 |
|---|---|---|
| bridge 首个客户端连接即崩溃 | rclpy Node 内部已有 `_clients`（service clients），同名属性被覆写导致执行器异常 | TCP 客户端列表改名 `_tcp_clients`（odin_bridge 同坑：`clients` → `tcp_clients`） |
| 下发关节指令时崩溃 | `JointCommand.stiffness` 的 float64[] setter 断言必须是 `float`，整数被拒 | 增益表统一 `float(x)` |
| 面板连接随机被断开 | relay 的上游 socket 继承了 `create_connection(timeout=5)` 的读超时，空闲 5s 即被关掉 | 连上后 `settimeout(None)` + 重试与日志 |
| 单滑杆拖动会带动全身 | 未触碰关节的目标初值是 0 | 面板：未触碰关节实时跟随当前角度 |
| **odin 链路每 2s 断一次重连** | odin_bridge 发送线程给 socket `settimeout(2.0)`，读线程 `recv` 复用同一 socket，空闲 2s 抛 `socket.timeout`（OSError 子类）被当成错误关闭 | 读循环显式 `except socket.timeout: continue` |
| 雷达页要手点连接 | — | 打开面板即自动连接，断线 3s 自动重连；手动「断开」后不再自动重连 |
