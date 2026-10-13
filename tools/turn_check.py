#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从外部真正发一次 TURN Allocate，验证 coturn 的 external-ip 配对没有。

为什么值得单独写一个：腾讯云 CVM/轻量 的网卡上是 VPC 内网 IP，公网是 NAT 映射的。
如果 coturn 没配 external-ip，Allocate 会成功、但回报的 XOR-RELAYED-ADDRESS 是 10.x ——
客户端把这个地址告诉对端，对端根本连不上，表现为"ICE 通了但零媒体"。
这个错误在 coturn 日志里**没有任何报错**，只能靠看返回的地址发现。

只用标准库：本地没有 turnutils_uclient（装它要 sudo 密码），
而 TURN 的 Allocate 握手用 struct + hmac 就能拼出来。
"""
import base64
import hashlib
import hmac
import os
import socket
import struct
import sys

MAGIC = 0x2112A442
ALLOCATE_REQ = 0x0003
ALLOCATE_OK = 0x0103
ALLOCATE_ERR = 0x0113

A_MAPPED = 0x0001          # MAPPED-ADDRESS
A_USERNAME = 0x0006
A_MESSAGE_INTEGRITY = 0x0008
A_ERROR_CODE = 0x0009
A_REALM = 0x0014
A_NONCE = 0x0015
A_XOR_RELAYED = 0x0016     # XOR-RELAYED-ADDRESS
A_REQUESTED_TRANSPORT = 0x0019
A_XOR_MAPPED = 0x0020


def attr(atype, value):
    pad = (4 - len(value) % 4) % 4
    return struct.pack("!HH", atype, len(value)) + value + b"\x00" * pad


def parse(data):
    mtype, mlen = struct.unpack("!HH", data[:4])
    out = {}
    off = 20
    while off + 4 <= 20 + mlen and off + 4 <= len(data):
        atype, alen = struct.unpack("!HH", data[off:off + 4])
        out.setdefault(atype, []).append(data[off + 4:off + 4 + alen])
        off += 4 + alen + ((4 - alen % 4) % 4)
    return mtype, out


def decode_addr(value):
    """解 MAPPED / XOR-MAPPED / XOR-RELAYED。

    XOR 系属性里**地址和端口都要异或**：端口异或 magic cookie 的高 16 位
    (0x2112)，地址异或完整的 magic cookie。
    只异或地址、忘了端口，会得到一个看起来完全合理、但根本不存在的中继端口 ——
    排查时会被这个假端口带偏很久（我就在这里绕过一圈）。
    """
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0] ^ (MAGIC >> 16)
    raw = value[4:8]
    if family == 0x02:                      # IPv6 的 XOR 还要异或 transaction id
        return None
    ip = bytes(b ^ m for b, m in zip(raw, struct.pack("!I", MAGIC)))
    return socket.inet_ntoa(ip), port


def error_text(value):
    return "class=%d number=%d" % (value[2], value[3])


def build_allocate(tid, username=None, realm=None, nonce=None, key=None):
    body = attr(A_REQUESTED_TRANSPORT, struct.pack("!BBH", 17, 0, 0))   # 17 = UDP
    if username is not None:
        body += attr(A_USERNAME, username.encode())
        body += attr(A_REALM, realm)
        body += attr(A_NONCE, nonce)
        # MESSAGE-INTEGRITY 的坑：算 HMAC 时头里的 length 必须**已经包含**
        # 这条 MI 属性本身（4 字节头 + 20 字节值 = 24），否则对端验签必然失败。
        final_len = len(body) + 24
        msg = struct.pack("!HHI", ALLOCATE_REQ, final_len, MAGIC) + tid + body
        mac = hmac.new(key, msg, hashlib.sha1).digest()
        body += attr(A_MESSAGE_INTEGRITY, mac)
    return struct.pack("!HHI", ALLOCATE_REQ, len(body), MAGIC) + tid + body


def main():
    if len(sys.argv) != 5:
        sys.exit("用法: turn_check.py <host> <username> <credential> <port>")
    host, username, credential, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(8)

    # ---- 第一轮：不带认证，逼服务器回 401 并把 realm/nonce 给我们 ----
    tid = os.urandom(12)
    sock.sendto(build_allocate(tid), (host, port))
    try:
        data, _ = sock.recvfrom(4096)
    except socket.timeout:
        sys.exit("  !! 超时：UDP %d 不通" % port)

    mtype, attrs = parse(data)
    if mtype != ALLOCATE_ERR:
        sys.exit("  !! 预期 401，收到 0x%04x" % mtype)
    if A_ERROR_CODE in attrs:
        print("  第一轮被拒（预期如此）: %s" % error_text(attrs[A_ERROR_CODE][0]))
    realm = attrs[A_REALM][0]
    nonce = attrs[A_NONCE][0]
    print("  realm = %s" % realm.decode())
    print("  nonce = %s" % nonce.decode()[:32] + "…")

    # ---- 第二轮：带认证。TURN REST 的密码就是那个 base64 值本身 ----
    key = hashlib.md5(b"%s:%s:%s" % (
        username.encode(), realm, credential.encode())).digest()
    tid = os.urandom(12)
    sock.sendto(build_allocate(tid, username, realm, nonce, key), (host, port))
    try:
        data, _ = sock.recvfrom(4096)
    except socket.timeout:
        sys.exit("  !! 第二轮超时")

    mtype, attrs = parse(data)
    if mtype == ALLOCATE_ERR:
        print("  !! Allocate 被拒: %s" % error_text(attrs[A_ERROR_CODE][0]))
        print("     401 → 凭证算错了（必须 raw digest 再 base64，不是 hexdigest）")
        return 1
    if mtype != ALLOCATE_OK:
        sys.exit("  !! 意外响应 0x%04x" % mtype)

    print("  Allocate 成功 (0x%04x)" % mtype)
    if A_XOR_MAPPED in attrs:
        ip, p = decode_addr(attrs[A_XOR_MAPPED][0])
        print("  XOR-MAPPED-ADDRESS（你在这台服务器眼里的地址）= %s:%d" % (ip, p))
    if A_XOR_RELAYED in attrs:
        ip, p = decode_addr(attrs[A_XOR_RELAYED][0])
        print("  XOR-RELAYED-ADDRESS（中继地址，对端要往这里发）= %s:%d" % (ip, p))
        if ip.startswith(("10.", "172.", "192.168.", "127.")):
            print("\n  !! 中继地址是内网地址 —— coturn 的 external-ip 没配上。")
            print("     外网对端连不到这个地址，症状是 ICE 显示 connected 但零媒体，")
            print("     而且 coturn 日志里没有任何报错。")
            return 1
        print("\n  ==> external-ip 正确：中继地址是公网 IP。TURN 配置没问题。")
        return 0
    print("  !! 响应里没有 XOR-RELAYED-ADDRESS")
    return 1


if __name__ == "__main__":
    sys.exit(main())
