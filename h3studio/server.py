"""Authenticated loopback transport shared by the native window and desktop checks."""
import hmac
import json
import mimetypes
import re
import secrets

import httpx
import shutil
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .assistant_bridge import AssistantBridge, UnknownTool

STATIC = Path(__file__).resolve().parent / 'web'


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, studio, port=0):
        self.studio = studio
        self.bootstrap = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.assistant = AssistantBridge(self)

    def server_close(self):
        if assistant := getattr(self, 'assistant', None):
            with self.studio.lock:
                assistant.revoke()
        super().server_close()

    @property
    def url(self):
        return self.origin + '/start?key=' + self.bootstrap


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def authorized(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            return hmac.compare_digest(cookies['h3_session'].value, self.server.cookie)
        except (KeyError, ValueError):
            return False

    def reply(self, code, data=b'', mime='application/json', headers=None):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        if requested := self.headers.get('Range'):
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
            if not match:
                return self.reply(416, headers={'Content-Range':f'bytes */{size}'})
            start = int(match[1])
            end = min(int(match[2]), size-1) if match[2] else size-1
            if start > end:
                return self.reply(416, headers={'Content-Range':f'bytes */{size}'})
            code = 206
        self.send_response(code)
        self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(end-start+1))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','private, no-cache')
        if code == 206:
            self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as f:
            f.seek(start)
            remain = end-start+1
            while remain:
                block = f.read(min(256*1024, remain))
                if not block:
                    break
                self.wfile.write(block)
                remain -= len(block)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/start' and hmac.compare_digest(parse_qs(url.query).get('key',[''])[0], self.server.bootstrap):
            return self.reply(303, headers={'Location':'/', 'Set-Cookie':'h3_session='+self.server.cookie+'; HttpOnly; SameSite=Strict; Path=/','Referrer-Policy':'no-referrer'})
        if not self.authorized():
            return self.reply(403)
        try:
            if url.path.startswith(('/asset/', '/revision/', '/take/')):
                kind, ident = url.path.strip('/').split('/')
                return self.send_file(self.server.studio.file(kind, ident))
            if url.path in ('/', '/icons.svg', '/fonts/BarlowSemiCondensed-SemiBold.ttf') or re.fullmatch(r'/[a-z][a-z0-9-]*\.(?:js|css)', url.path):
                return self.send_file(STATIC / ('index.html' if url.path=='/' else url.path[1:]))
            return self.reply(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (OSError, ValueError, KeyError):
            return self.reply(404)

    def do_POST(self):
        if self.path == '/assistant':
            return self.assistant_request()
        if self.path != '/api' or not self.authorized() or self.headers.get('Origin',self.server.origin) != self.server.origin:
            return self.reply(403)
        try:
            size = int(self.headers.get('Content-Length','0'))
            if size < 1 or size > 8*1024*1024:
                return self.reply(413)
            data = json.loads(self.rfile.read(size))
            op = data.pop('op')
            result = (self.server.assistant.desktop(op, data) if op.startswith('assistant_')
                      else self.server.studio.action(op, data))
            return self.reply(200, json.dumps(result, default=str, allow_nan=False).encode())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except httpx.ConnectError:
            return self.reply(400, json.dumps({'error':'The local engine is offline. Open Engines to start the renderer or set its connection address, then try again.'}).encode())
        except Exception as error:
            return self.reply(400, json.dumps({'error':str(error) or type(error).__name__}).encode())

    def assistant_request(self):
        # Stdio adapters use a distinct bearer. Browsers and renderer workers use separate routes.
        auth = self.headers.get('Authorization', '')
        if self.headers.get('Origin') is not None or not auth.startswith('Bearer ') or not auth.isascii():
            return self.reply(403, b'{"error":"Use the paired Studio client."}')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 1 <= size <= 160 * 1024:
                return self.reply(413, b'{"error":"Message too large."}')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Expected a Studio tool request.')
            result = self.server.assistant.request(auth[7:], data.get('method'), data.get('params'))
            return self.reply(200, json.dumps(result, allow_nan=False).encode())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except PermissionError as error:
            return self.reply(403, json.dumps({'error': str(error)}).encode())
        except UnknownTool as error:
            return self.reply(400, json.dumps({'error': str(error), 'rpc_code': -32602}).encode())
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            return self.reply(400, json.dumps({'error': str(error)}).encode())
