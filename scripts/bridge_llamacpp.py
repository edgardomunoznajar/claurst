#!/usr/bin/env python3
"""HTTP-aware forwarder: 172.17.0.1:8000 -> 127.0.0.1:8000

llama-server is bound to 127.0.0.1:8000 with `--api-key local` required.
Simon's llamacpp provider (crates/api/src/registry.rs) does NOT propagate
a configured API key into the OpenAiCompatProvider for llama_cpp() — the
api_base is set but with_api_key() is never called. Rather than patch
simon's Rust registry, this bridge:

  1. Listens on the docker-bridge IP (172.17.0.1:8000) so containers
     reaching host.docker.internal (host-gateway mapping) can hit it.
  2. Parses incoming HTTP request headers and injects
     `Authorization: Bearer local` when absent, so llama-server accepts
     the request.
  3. Forwards to 127.0.0.1:8000 transparently, streaming bytes in both
     directions after the first header chunk.

No third-party deps (no socat, no mitmproxy). Created 2026-04-19 for
the Gemma-4-drives-simon experiment.
"""
import socket
import threading
import sys

BIND = ("172.17.0.1", 8000)
UPSTREAM = ("127.0.0.1", 8000)
INJECT_AUTH = b"Authorization: Bearer local"


def read_http_headers(sock):
    """Read bytes until \r\n\r\n. Returns (header_bytes, remaining_buffer)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return buf, b""
        buf += chunk
        if len(buf) > 65536:
            # Pathological header length — bail.
            return buf, b""
    idx = buf.find(b"\r\n\r\n")
    return buf[: idx + 4], buf[idx + 4 :]


def inject_auth(headers: bytes) -> bytes:
    """Add an Authorization header if none is present."""
    lower = headers.lower()
    if b"authorization:" in lower:
        return headers
    # Insert just before the terminating CRLFCRLF.
    return headers[:-2] + INJECT_AUTH + b"\r\n" + headers[-2:]


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client):
    try:
        headers, leftover = read_http_headers(client)
        if not headers:
            client.close()
            return
        headers = inject_auth(headers)
        upstream = socket.create_connection(UPSTREAM, timeout=10)
        upstream.sendall(headers)
        if leftover:
            upstream.sendall(leftover)
    except OSError as e:
        print(f"handle error: {e}", file=sys.stderr)
        client.close()
        return

    t = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
    t.start()
    pipe(upstream, client)
    try:
        client.close()
    except OSError:
        pass
    try:
        upstream.close()
    except OSError:
        pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(BIND)
    srv.listen(32)
    print(
        f"bridge listening on {BIND[0]}:{BIND[1]} -> {UPSTREAM[0]}:{UPSTREAM[1]} "
        f"(auto-injecting Authorization: Bearer local)",
        flush=True,
    )
    while True:
        try:
            client, _ = srv.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
