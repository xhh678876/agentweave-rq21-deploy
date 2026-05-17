"""Minimal Python proxy: Codex → DeepSeek with thinking disabled.

Codex 0.45 sends OpenAI Chat Completions requests but can't add the
extra body fields DeepSeek V4 thinking mode requires. This proxy
sits on localhost:8765, takes Codex's request, injects
``thinking: {type: "disabled"}``, and forwards to DeepSeek.

Run:
    python deepseek_proxy.py

Then configure Codex with:
    base_url = "http://127.0.0.1:8765/v1"
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import urllib.request
import urllib.error

UPSTREAM = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
LISTEN_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8765"))


class Handler(BaseHTTPRequestHandler):
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _forward(self, method: str) -> None:
        body = self._read_body()
        # If JSON body, inject thinking:disabled
        if body and self.headers.get("Content-Type", "").startswith("application/json"):
            try:
                payload = json.loads(body.decode("utf-8"))
                # Only inject for chat completions (where thinking matters)
                if "messages" in payload and "thinking" not in payload:
                    payload["thinking"] = {"type": "disabled"}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                # Re-set Content-Length
                self.headers.replace_header("Content-Length", str(len(body)))
            except json.JSONDecodeError:
                pass

        # Avoid double /v1 when UPSTREAM already ends with /v1 and path begins with /v1
        upstream = UPSTREAM.rstrip("/")
        path = self.path
        if upstream.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]  # drop leading "/v1"
        url = upstream + path

        # Forward headers (drop Host so urllib uses upstream host)
        fwd_headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in {"host", "content-length"}
        }
        fwd_headers["Content-Length"] = str(len(body))

        req = urllib.request.Request(url, data=body if body else None, headers=fwd_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in {"transfer-encoding", "content-encoding", "connection"}:
                        continue
                    self.send_header(k, v)
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            err_body = e.read()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            import traceback
            err_msg = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            print(f"[proxy ERROR] {err_msg}", flush=True)
            err = json.dumps({"error": {"message": err_msg, "type": "proxy_error"}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def do_GET(self) -> None: self._forward("GET")
    def do_POST(self) -> None: self._forward("POST")
    def do_PUT(self) -> None: self._forward("PUT")
    def do_DELETE(self) -> None: self._forward("DELETE")

    def log_message(self, format: str, *args) -> None:  # pragma: no cover
        super().log_message(format, *args)


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[deepseek-proxy] listening on http://{LISTEN_HOST}:{LISTEN_PORT}  -> {UPSTREAM}")
    print(f"[deepseek-proxy] auto-injecting thinking:{{type:disabled}} into all chat requests")
    server.serve_forever()


if __name__ == "__main__":
    main()
