#!/usr/bin/env python3
"""
TCP 中转（运行在 Orin 上）：把本机(desk) 的连接转发到机器人 .163 的 bridge。

  desk ──▶ 192.168.1.50:9801 (本脚本) ──▶ 192.168.0.163:9801 (bridge.py)

用法（Orin 上）:
  nohup python3 relay.py > relay.log 2>&1 &
"""
import argparse
import socket
import threading
import time


def pipe(src, dst, tag):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                print("[relay] %s: EOF" % tag, flush=True)
                break
            dst.sendall(data)
    except OSError as e:
        print("[relay] %s: error %s" % (tag, e), flush=True)
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            src.close()
        except OSError:
            pass


def handle(client, target_host, target_port):
    peer = client.getpeername()
    print("[relay] downstream %s connected" % (peer,), flush=True)
    up = None
    last_err = None
    for attempt in (1, 2, 3):
        try:
            up = socket.create_connection((target_host, target_port), timeout=5)
            break
        except OSError as e:
            last_err = e
            time.sleep(0.2)
    if up is None:
        print("[relay] upstream connect failed: %s" % last_err, flush=True)
        client.close()
        return
    # 关键：连上后取消超时，否则空闲 5 秒后 recv 会抛 timeout 把连接关掉
    up.settimeout(None)
    try:
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    print("[relay] upstream -> %s:%d ok" % (target_host, target_port), flush=True)
    threading.Thread(target=pipe, args=(client, up, "%s->bridge" % (peer,)),
                     daemon=True).start()
    threading.Thread(target=pipe, args=(up, client, "bridge->%s" % (peer,)),
                     daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--listen', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=9801)
    ap.add_argument('--target', default='192.168.0.163')
    ap.add_argument('--target-port', type=int, default=9801)
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.listen, args.port))
    srv.listen(16)
    print("relay %s:%d -> %s:%d" % (args.listen, args.port,
                                    args.target, args.target_port), flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle,
                         args=(client, args.target, args.target_port),
                         daemon=True).start()


if __name__ == '__main__':
    main()
