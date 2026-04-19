#!/usr/bin/env python3
"""OpenAI-compatible HTTP shim around `simon --print`.

Aider (or any OpenAI SDK client) posts /v1/chat/completions; this shim flattens
the chat history into a single prompt and shells out to `docker exec simon
simon --print` inside the running simon container, then returns the agent's
final reply in OpenAI shape. Streaming is faked: one SSE chunk + [DONE].

Usage:
    python3 scripts/simon_openai_shim.py            # listen on 127.0.0.1:8600

Point Aider with:
    aider --openai-api-base http://127.0.0.1:8600/v1 \\
          --openai-api-key simon \\
          --model openai/simon-agent
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_LINE_RE = re.compile(r"^\s*(INFO|WARN|ERROR|DEBUG|TRACE)\b")


def clean_output(raw: str) -> str:
    stripped = ANSI_RE.sub("", raw)
    kept = [ln for ln in stripped.splitlines() if not LOG_LINE_RE.match(ln)
            and "OIDC login" not in ln and "Plugins loaded" not in ln]
    return "\n".join(kept).strip()

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8600
CONTAINER = "simon"
MAX_TURNS = "10"
EXEC_TIMEOUT_SEC = 600


def flatten_messages(messages: list[dict]) -> tuple[str | None, str]:
    system_parts: list[str] = []
    transcript_parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            transcript_parts.append(f"USER: {content}")
        elif role == "assistant":
            transcript_parts.append(f"ASSISTANT: {content}")
    system = "\n\n".join(system_parts) if system_parts else None
    prompt = "\n\n".join(transcript_parts)
    return system, prompt


def run_simon(system: str | None, prompt: str) -> str:
    cmd = [
        "docker", "exec", "-i",
        "-e", "RUST_LOG=error",
        "-e", "NO_COLOR=1",
        CONTAINER,
        "simon", "--print",
        "--permission-mode", "accept-edits",
        "--max-turns", MAX_TURNS,
        "--output-format", "text",
    ]
    if system:
        cmd += ["--append-system-prompt", system]
    cmd.append(prompt)
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=EXEC_TIMEOUT_SEC
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"simon exited {result.returncode}: {result.stderr[-500:]}"
        )
    return clean_output(result.stdout)


def openai_completion(content: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "simon-agent",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def sse_stream_payload(content: str) -> bytes:
    cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())
    first = {
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": "simon-agent",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
    }
    final = {
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": "simon-agent",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return (
        f"data: {json.dumps(first)}\n\n"
        f"data: {json.dumps(final)}\n\n"
        "data: [DONE]\n\n"
    ).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[shim] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/v1/models", "/models"):
            self._json(200, {
                "object": "list",
                "data": [{"id": "simon-agent", "object": "model", "owned_by": "simon"}],
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            self._json(400, {"error": f"bad json: {e}"})
            return
        messages = req.get("messages", [])
        stream = bool(req.get("stream", False))
        system, prompt = flatten_messages(messages)
        if not prompt:
            self._json(400, {"error": "no user messages"})
            return
        try:
            reply = run_simon(system, prompt)
        except subprocess.TimeoutExpired:
            self._json(504, {"error": "simon timed out"})
            return
        except Exception as e:
            self._json(500, {"error": str(e)})
            return
        if stream:
            body = sse_stream_payload(reply)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json(200, openai_completion(reply))


def main():
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[shim] simon OpenAI shim on http://{LISTEN_HOST}:{LISTEN_PORT}/v1", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


if __name__ == "__main__":
    main()
