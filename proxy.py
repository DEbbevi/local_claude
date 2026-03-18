#!/usr/bin/env python3
"""
Thin middleware between Claude Code and LM Studio (requires Python 3.14+).

Handles known issues that cause hangs:
1. Haiku background requests → fake instant responses
2. Token counting endpoint → fake responses
3. Unknown/beta endpoints → graceful handling
4. Image tool_results → relocated to user message (LM Studio rejects images inside tool_result arrays)

LM Studio's native /v1/messages endpoint handles the actual API translation.
LM Studio handles its own request queuing — no serialization lock needed.
"""

import json
import http.server
import urllib.request
import sys
import uuid
import threading
from datetime import datetime
from typing import Any

# --- Type aliases ---
JsonDict = dict[str, Any]

# --- Constants ---
LM_STUDIO = "http://localhost:1234"
PORT = 4000
PROXY_TIMEOUT_GET: int = 30
PROXY_TIMEOUT_POST: int = 600
STREAM_CHUNK_SIZE: int = 4096
DEFAULT_MODEL: str = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS: int = 4096
MIN_BOOSTED_TOKENS: int = 16384
# Claude Code expects token counts — these are ignored but must be present
FAKE_INPUT_TOKENS: int = 10
FAKE_OUTPUT_TOKENS: int = 1
MAX_TOKENS_MULTIPLIER: int = 3


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    sys.stderr.write(f"[{ts()}] {msg}\n")
    sys.stderr.flush()


def rewrite_image_tool_results(body: JsonDict) -> int:
    """Move images from inside tool_result.content arrays to the surrounding user message.

    LM Studio rejects image blocks inside tool_result arrays but accepts them
    as standalone content blocks in the same user message."""
    count: int = 0
    for msg in body.get("messages", []):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        new_content: list[JsonDict] = []
        for block in msg["content"]:
            if block.get("type") != "tool_result":
                new_content.append(block)
                continue
            inner = block.get("content")
            if not isinstance(inner, list):
                new_content.append(block)
                continue
            extracted: list[JsonDict] = []
            kept: list[JsonDict] = []
            for item in inner:
                if item.get("type") == "image":
                    extracted.append(item)
                    count += 1
                else:
                    kept.append(item)
            if extracted:
                kept.append({"type": "text", "text": "See the image(s) below."})
                block = {**block, "content": kept}
            new_content.append(block)
            for img in extracted:
                new_content.append({"type": "text", "text": "Here is the image you wanted to look at:"})
                new_content.append(img)
        msg["content"] = new_content
    return count


def is_housekeeping_model(model: str | None) -> bool:
    if not model:
        return False
    return "haiku" in model.lower()


def fake_haiku_response(model: str) -> tuple[str, JsonDict]:
    """Build a fake Anthropic-shaped response for haiku background requests.

    Claude Code fires haiku requests for housekeeping (summaries, titles, etc.).
    These would queue behind real requests in LM Studio, so we fake them instantly."""
    msg_id: str = f"msg_{uuid.uuid4().hex[:24]}"

    return "json", {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": FAKE_INPUT_TOKENS, "output_tokens": FAKE_OUTPUT_TOKENS},
    }


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress default logging, we use our own

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
            return
        log(f"GET {self.path} → LM Studio")
        try:
            url: str = f"{LM_STUDIO}{self.path}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_GET) as resp:
                data: bytes = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            log(f"GET {self.path} error: {e}")
            self._json_response(404, {"error": str(e)})

    def do_POST(self) -> None:
        length: int = int(self.headers.get("Content-Length", 0))
        raw: bytes = self.rfile.read(length)
        path: str = self.path

        # Token counting → fake
        if "count_tokens" in path:
            log("Token count → fake")
            self._json_response(200, {"input_tokens": 100})
            return

        try:
            body: JsonDict = json.loads(raw)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "Invalid JSON"})
            return

        model: str = body.get("model", "")
        is_stream: bool = body.get("stream", False)

        # Haiku → always fake as non-streaming JSON (streaming fake hangs Claude Code)
        if is_housekeeping_model(model):
            log(f"Haiku ({model}) → fake JSON")
            _, data = fake_haiku_response(model or DEFAULT_MODEL)
            self._json_response(200, data)
            self.close_connection = True
            return

        # Relocate images out of tool_results (LM Studio compat)
        img_count: int = rewrite_image_tool_results(body)
        if img_count:
            log(f"Relocated {img_count} image(s) from tool_result → user message")

        # Triple max_tokens — local models undercount tokens vs Claude, so they
        # hit the limit and truncate output well before the logical end
        orig_max: int = body.get("max_tokens", DEFAULT_MAX_TOKENS)
        body["max_tokens"] = max(orig_max * MAX_TOKENS_MULTIPLIER, MIN_BOOSTED_TOKENS)
        raw = json.dumps(body).encode()

        log(f"→ LM Studio | model={model} stream={is_stream} "
            f"msgs={len(body.get('messages', []))} "
            f"max_tokens={orig_max}→{body['max_tokens']}")

        # Forward to LM Studio
        url: str = f"{LM_STUDIO}{path}"
        req = urllib.request.Request(url, data=raw, method="POST")
        req.add_header("Content-Type", "application/json")
        for k in self.headers:
            if k.lower() not in ("host", "content-length", "transfer-encoding"):
                req.add_header(k, self.headers[k])

        try:
            resp = urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_POST)
        except Exception as e:
            log(f"LM Studio error: {e}")
            self._json_response(502, {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            })
            return

        if is_stream:
            self.send_response(200)
            ct: str = resp.headers.get("Content-Type", "text/event-stream")
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    chunk: bytes = resp.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except BrokenPipeError, ConnectionResetError:
                log("Client disconnected during stream")
            except Exception as e:
                log(f"Stream error: {e}")
            finally:
                resp.close()
                log("Stream complete")
        else:
            data = resp.read()
            resp.close()
            self.send_response(200)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            log("Response complete")

    def _json_response(self, code: int, data: JsonDict) -> None:
        body: bytes = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(http.server.HTTPServer):
    allow_reuse_address: bool = True

    def process_request(self, request: Any, client_address: tuple[str, int]) -> None:
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request: Any, client_address: tuple[str, int]) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    print(f"Claude Code ↔ LM Studio middleware | :{PORT} → {LM_STUDIO}")
    print(f"  Haiku interception: ON")
    print(f"  Token counting: ON")
    print(f"  No serialization lock (LM Studio handles queuing)")
    print(f"\n  export ANTHROPIC_BASE_URL=http://localhost:{PORT}")
    print("  export ANTHROPIC_AUTH_TOKEN=lmstudio")
    print("  claude --model qwen3.5-35b-a3b-mlx\n")

    server = ThreadedHTTPServer(("0.0.0.0", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
