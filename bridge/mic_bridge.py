#!/usr/bin/env python3
"""
T800 麦克风 bridge（运行在应用计算单元 .162 上，端口 9803）

功能：
  1. 采集 AIUI 环麦（16kHz），100ms 窗 RMS 声级（dBFS），10Hz 推给面板顶栏 🎙 表；
  2. 能量 VAD 分句 → 推流到本机 FunASR（ws://127.0.0.1:10095，出厂自带 engineai-asr 容器）
     → 识别文本（partial/final）实时推给面板（{"t":"asr",...}）。

运行:
  nohup setsid python3 ~/t800_mic/mic_bridge.py --port 9803 > ~/t800_mic/mic_bridge.log 2>&1 < /dev/null &

协议（行分隔 JSON，服务端 -> 客户端）:
  {"t":"hello","device":..,"rate":16000,"asr":true}
  {"t":"level","db":-32.5,"peak":-18.2,"rms":0.0237}          # 10Hz
  {"t":"asr","text":"部分/最终文本","final":false|true}        # final 时为新的一句
"""
import argparse
import asyncio
import collections
import json
import math
import re
import socket
import socketserver
import subprocess
import threading
import time

METER_FLOOR = -60.0
EMA_ALPHA = 0.35


def detect_capture_device():
    try:
        out = subprocess.run(['arecord', '-l'], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return 'default'
    for line in out.splitlines():
        low = line.lower()
        if 'card' in low and ('aiui' in low or 'mic' in low):
            name = line.split(':')[1].split('[')[0].strip()
            return 'plughw:CARD=%s,DEV=0' % name
    return 'default'


class MicBridge:
    def __init__(self, args):
        self.args = args
        self.device = args.device or detect_capture_device()
        self.lock = threading.Lock()
        self.level = METER_FLOOR
        self.peak = METER_FLOOR
        self.rms = 0.0
        self.clients = []
        # VAD / ASR 共享状态（capture 线程写，asyncio 线程读，GIL 下安全）
        self.ema_db = METER_FLOOR
        self.last_loud_ts = 0.0
        self.preroll = collections.deque(maxlen=8)     # 最近 800ms PCM（100ms/块）
        self.pcm_q = collections.deque(maxlen=400)     # 待发 PCM（仅 SPEAKING 时被取走）
        self.asr_text = ''
        self.asr_final = True

    def log(self, msg):
        print('[mic] %s' % msg, flush=True)

    # ---------------- 采集 + 分贝 + VAD 状态 ----------------
    def capture_loop(self):
        cand = [self.device] + (['default'] if self.device != 'default' else [])
        i = 0
        while True:
            dev = cand[i % len(cand)]
            i += 1
            cmd = ['arecord', '-D', dev, '-f', 'S16_LE', '-r', str(self.args.rate),
                   '-c', '1', '-t', 'raw']
            self.log('capture start: %s' % ' '.join(cmd))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
            except OSError as e:
                self.log('arecord launch failed: %s' % e)
                time.sleep(2)
                continue
            self.device_used = dev
            chunk = 1600  # 100ms @16kHz
            ema = None
            while True:
                data = proc.stdout.read(chunk * 2)
                if not data or len(data) < chunk * 2:
                    break
                n = len(data) // 2
                acc = 0
                for off in range(0, n * 2, 2):
                    v = data[off] | (data[off + 1] << 8)
                    if v >= 32768:
                        v -= 65536
                    acc += v * v
                rms = math.sqrt(acc / n) / 32768.0
                db = max(METER_FLOOR, 20.0 * math.log10(max(rms, 1e-8)))
                ema = db if ema is None else (EMA_ALPHA * db + (1 - EMA_ALPHA) * ema)
                now = time.time()
                with self.lock:
                    self.level = ema
                    self.rms = rms
                    self.peak = max(self.peak - 3.0 * 0.1, db)
                    self.ema_db = ema
                    self.preroll.append(data)
                    self.pcm_q.append(data)   # 供 ASR 发送线程取用（SPEAKING 态）
                if ema > self.args.vad_thresh:
                    self.last_loud_ts = now
            self.log('capture ended (rc=%s), retry in 2s' % proc.poll())
            try:
                proc.kill()
            except OSError:
                pass
            time.sleep(2)

    def snapshot(self):
        with self.lock:
            return {'t': 'level', 'db': round(self.level, 1),
                    'peak': round(self.peak, 1), 'rms': round(self.rms, 5)}

    def broadcast(self, obj):
        blob = (json.dumps(obj, separators=(',', ':')) + '\n').encode()
        dead = []
        for c in list(self.clients):
            try:
                c.sock.settimeout(2.0)
                c.sock.sendall(blob)
            except OSError:
                dead.append(c)
        for c in dead:
            try:
                self.clients.remove(c)
            except ValueError:
                pass
            c.dead = True
            self.log('client gone: %s' % (c.addr,))

    def push_asr(self, text, final):
        with self.lock:
            self.asr_text, self.asr_final = text, final
        self.broadcast({'t': 'asr', 'text': text, 'final': final})


# ---------------- FunASR 客户端（asyncio 线程） ----------------
async def asr_worker(br: MicBridge):
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect

    async def pcm_sender(ws, final_evt: asyncio.Event, st):
        """状态机：IDLE 静音丢弃；检测到声音 -> 新建 utterance（新 wav_name）并流 PCM；
        静音超过 flush 秒 -> is_speaking=false 收尾，等识别结果（receiver 置事件）后回 IDLE。"""
        state = 'IDLE'
        wav_name = ''
        while True:
            await asyncio.sleep(0.02)
            now = time.time()
            if state == 'IDLE':
                if (now - br.last_loud_ts) < br.args.flush_s:
                    wav_name = 'robot_mic_%d' % int(now * 1000)
                    cfg = {'mode': '2pass', 'chunk_size': [5, 10, 5],
                           'chunk_interval': 10, 'wav_name': wav_name,
                           'wav_format': 'pcm', 'audio_fs': br.args.rate,
                           'audio_type': 'pcm', 'is_speaking': True, 'itn': True}
                    await ws.send(json.dumps(cfg))
                    with br.lock:
                        pre = list(br.preroll)
                    for b in pre:
                        await ws.send(b)
                    state = 'SPEAKING'
                    st['flush_pending'] = False
                    final_evt.clear()
                    br.log('asr utterance start (%s)' % wav_name)
            elif state == 'SPEAKING':
                while br.pcm_q:
                    await ws.send(br.pcm_q.popleft())
                if (now - br.last_loud_ts) > br.args.flush_s:
                    await ws.send(json.dumps({'is_speaking': False}))
                    st['flush_pending'] = True
                    state = 'FLUSHING'
                    br.log('asr utterance flush (%s)' % wav_name)
            else:  # FLUSHING：等 receiver 收到结果；音频丢弃
                while br.pcm_q:
                    br.pcm_q.popleft()
                try:
                    await asyncio.wait_for(final_evt.wait(), timeout=5)
                    final_evt.clear()
                except asyncio.TimeoutError:
                    br.log('asr flush timeout, back to IDLE')
                state = 'IDLE'

    async def receiver(ws, final_evt: asyncio.Event, st):
        n = 0
        async for raw in ws:
            n += 1
            if n <= 8 or n % 25 == 0:
                br.log('asr raw#%d: %s' % (n, str(raw)[:220]))
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if 'text' not in msg:
                continue
            # SenseVoice 返回带 <|zh|><|EMO_UNKNOWN|><|Speech|> 等标签，剥掉
            text = re.sub(r'<\|[^|]*\|>', '', msg.get('text') or '').strip()
            final = bool(st['flush_pending'] or msg.get('is_final')
                         or msg.get('type') == 'final')
            if final:
                st['flush_pending'] = False
            if not text:
                # 空结果（纯噪声）：直接结束本轮，不卡 5 秒也不推给面板
                if final:
                    final_evt.set()
                continue
            br.log('asr [%s] %s' % ('final' if final else 'part ', text))
            br.push_asr(text, final)
            final_evt.set()

    while True:
        try:
            async with connect(br.args.asr_url, max_size=None,
                               open_timeout=5, ping_interval=None) as ws:
                br.log('asr ws connected: %s' % br.args.asr_url)
                final_evt = asyncio.Event()
                st = {'flush_pending': False}
                snd = asyncio.ensure_future(pcm_sender(ws, final_evt, st))
                try:
                    await receiver(ws, final_evt, st)
                finally:
                    snd.cancel()
        except Exception as e:
            br.log('asr ws error: %s, retry in 2s' % e)
            await asyncio.sleep(2)

    while True:
        try:
            async with connect(br.args.asr_url, max_size=None,
                               open_timeout=5, ping_interval=None) as ws:
                br.log('asr ws connected: %s' % br.args.asr_url)
                final_evt = asyncio.Event()
                snd = asyncio.ensure_future(pcm_sender(ws, final_evt))
                try:
                    await receiver(ws, final_evt)
                finally:
                    snd.cancel()
        except Exception as e:
            br.log('asr ws error: %s, retry in 2s' % e)
            await asyncio.sleep(2)


def asr_thread(br: MicBridge):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(asr_worker(br))


# ---------------- TCP 服务 ----------------
class Client:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.dead = False


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        br: MicBridge = self.server.bridge
        c = Client(self.request, self.client_address)
        br.clients.append(c)
        br.log('client connected: %s (total %d)' % (self.client_address, len(br.clients)))
        try:
            hello = json.dumps({'t': 'hello', 'device': getattr(br, 'device_used', br.device),
                                'rate': br.args.rate, 'asr': True}) + '\n'
            self.request.sendall(hello.encode())
            with br.lock:
                cur = {'t': 'asr', 'text': br.asr_text, 'final': br.asr_final}
            if cur['text']:
                self.request.sendall((json.dumps(cur) + '\n').encode())
        except OSError:
            pass
        try:
            buf = b''
            while not c.dead:
                try:
                    data = self.request.recv(4096)
                except socket.timeout:
                    continue   # 发送线程设的临时超时，忽略（防 2s 断连坑）
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    if b'"ping"' in line:
                        try:
                            self.request.sendall(b'{"t":"pong"}\n')
                        except OSError:
                            c.dead = True
        except OSError:
            pass
        finally:
            c.dead = True
            try:
                br.clients.remove(c)
            except ValueError:
                pass
            br.log('client gone: %s (total %d)' % (self.client_address, len(br.clients)))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=9803)
    ap.add_argument('--device', default=None, help='ALSA 采集设备，默认自动找 AIUI 麦阵')
    ap.add_argument('--rate', type=int, default=16000)
    ap.add_argument('--asr-url', default='ws://127.0.0.1:10095', help='FunASR WebSocket')
    ap.add_argument('--vad-thresh', type=float, default=-45.0, help='语音能量门限 dBFS')
    ap.add_argument('--flush-s', type=float, default=1.2, help='静音多少秒后收尾一句')
    args = ap.parse_args()

    br = MicBridge(args)
    threading.Thread(target=br.capture_loop, daemon=True).start()
    threading.Thread(target=asr_thread, args=(br,), daemon=True).start()

    srv = Server((args.host, args.port), Handler)
    srv.bridge = br
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    br.log('listening on %s:%d, device=%s, asr=%s' % (args.host, args.port, br.device, args.asr_url))

    while True:
        time.sleep(0.1)   # 10Hz 分贝推送
        br.broadcast({'t': 'level', **{k: br.snapshot()[k] for k in ('db', 'peak', 'rms')}})


if __name__ == '__main__':
    main()
