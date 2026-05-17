#!/usr/bin/env python3
"""多 key DeepSeek proxy
- 监听 127.0.0.1:8765
- 拿多个 DeepSeek API key 轮换
- 注入 thinking: {type: disabled} 给 V4 系列模型
"""
import http.server
import http.client
import json
import os
import threading
import urllib.request
import socketserver

DEEPSEEK_HOST = "api.deepseek.com"
DEEPSEEK_PORT = 443

KEYS = []
_idx_lock = threading.Lock()
_idx = 0

def next_key():
    global _idx
    with _idx_lock:
        k = KEYS[_idx % len(KEYS)]
        _idx += 1
    return k

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except:
            data = {}
        # 注入 thinking disabled
        if "deepseek-v4" in data.get("model", ""):
            data.setdefault("thinking", {"type": "disabled"})
        body = json.dumps(data).encode()
        
        # forward
        conn = http.client.HTTPSConnection(DEEPSEEK_HOST, DEEPSEEK_PORT, timeout=600)
        headers = {
            "Authorization": f"Bearer {next_key()}",
            "Content-Type": "application/json",
        }
        conn.request("POST", self.path, body=body, headers=headers)
        resp = conn.getresponse()
        self.send_response(resp.status)
        for h, v in resp.getheaders():
            if h.lower() in ('transfer-encoding', 'connection'): continue
            self.send_header(h, v)
        self.end_headers()
        while True:
            chunk = resp.read(8192)
            if not chunk: break
            try: self.wfile.write(chunk)
            except: break
        conn.close()
    
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - - [{self.log_date_time_string()}] {fmt % args}")

if __name__ == "__main__":
    keys_file = os.environ.get("DEEPSEEK_KEYS_FILE", os.path.expanduser("~/.deepseek_keys"))
    with open(keys_file) as f:
        KEYS = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    print(f"加载 {len(KEYS)} 个 DeepSeek key")
    port = int(os.environ.get("PROXY_PORT", "8765"))
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as srv:
        srv.allow_reuse_address = True
        print(f"proxy 监听 127.0.0.1:{port}")
        srv.serve_forever()
