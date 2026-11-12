"""Servidor HTTP en thread para webhooks entrantes (n8n/Asana).

Extraído de pomogrowth_v8_ultra/app_ultra.py (extract_from_v8.py, una sola vez).
A partir de aquí este archivo es fuente normal de v9.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import queue
from typing import Optional


class WebhookServer:
    def __init__(self, webhook_queue: queue.Queue, port: int):
        self.queue = webhook_queue; self.port = port; self.server: Optional[HTTPServer] = None; self.thread: Optional[threading.Thread] = None
        class _WebhookRequestHandler(BaseHTTPRequestHandler):
            def _send_response(self, code, message): self.send_response(code); self.send_header('Content-type', 'application/json'); self.end_headers(); self.wfile.write(json.dumps({'status': message}).encode('utf-8'))
            def do_POST(handler):
                if handler.path == '/webhook':
                    try: content_length = int(handler.headers['Content-Length']); post_data = handler.rfile.read(content_length); data = json.loads(post_data.decode('utf-8')); webhook_queue.put(data); handler._send_response(200, 'Task received')
                    except Exception as e: handler._send_response(400, f'Bad Request: {e}')
                else: handler._send_response(404, 'Not Found')
            def log_message(self, format, *args): return
        self.server = HTTPServer(('0.0.0.0', port), _WebhookRequestHandler)
    def start(self):
        if not self.thread: self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
    def stop(self):
        if self.server: threading.Thread(target=self.server.shutdown, daemon=True).start()

