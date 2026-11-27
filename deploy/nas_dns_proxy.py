#!/usr/bin/env python3
"""Minimal UDP/TCP DNS proxy from a hass network container to Docker DNS."""
import socket
import threading

UPSTREAM = ("127.0.0.11", 53)


def udp_loop() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 53))
    while True:
        data, addr = server.recvfrom(4096)
        upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        upstream.settimeout(5)
        try:
            upstream.sendto(data, UPSTREAM)
            response, _ = upstream.recvfrom(4096)
            server.sendto(response, addr)
        except OSError:
            pass
        finally:
            upstream.close()


def handle_tcp(client: socket.socket) -> None:
    try:
        length = client.recv(2)
        if len(length) < 2:
            return
        remaining = int.from_bytes(length, "big")
        data = b""
        while len(data) < remaining:
            chunk = client.recv(remaining - len(data))
            if not chunk:
                return
            data += chunk
        upstream = socket.create_connection(UPSTREAM, timeout=5)
        try:
            upstream.sendall(len(data).to_bytes(2, "big") + data)
            reply_len = upstream.recv(2)
            if len(reply_len) < 2:
                return
            reply_remaining = int.from_bytes(reply_len, "big")
            reply = b""
            while len(reply) < reply_remaining:
                chunk = upstream.recv(reply_remaining - len(reply))
                if not chunk:
                    break
                reply += chunk
            client.sendall(len(reply).to_bytes(2, "big") + reply)
        finally:
            upstream.close()
    finally:
        client.close()


def tcp_loop() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 53))
    server.listen(20)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle_tcp, args=(client,), daemon=True).start()


threading.Thread(target=udp_loop, daemon=True).start()
threading.Thread(target=tcp_loop, daemon=True).start()
threading.Event().wait()
