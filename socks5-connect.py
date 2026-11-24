#!/usr/bin/env python3
"""ssh ProxyCommand: connect HOST PORT through a SOCKS5 proxy, pipe stdio.

    ProxyCommand=python3 -I socks5-connect.py 127.0.0.1:1080 %h %p

Stands in for `nc -X 5 -x`, which the CI job images don't reliably ship.
"""
import select
import socket
import struct
import sys


def main() -> int:
    proxy, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
    phost, pport = proxy.rsplit(":", 1)
    s = socket.create_connection((phost, int(pport)), timeout=15)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        sys.stderr.write("socks5: proxy refused no-auth\n")
        return 1
    h = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
    head = s.recv(4)
    if len(head) < 4 or head[1] != 0:
        sys.stderr.write(f"socks5: connect to {host}:{port} failed (code {head[1:2].hex()})\n")
        return 1
    skip = {1: 4, 4: 16}.get(head[3])
    skip = skip if skip is not None else s.recv(1)[0]
    s.recv(skip + 2)
    s.settimeout(None)
    stdin, stdout = sys.stdin.buffer.raw, sys.stdout.buffer.raw
    while True:
        r, _, _ = select.select([s, stdin], [], [])
        if s in r:
            d = s.recv(65536)
            if not d:
                return 0
            stdout.write(d)
        if stdin in r:
            d = stdin.read(65536)
            if not d:
                return 0
            s.sendall(d)


if __name__ == "__main__":
    sys.exit(main())
