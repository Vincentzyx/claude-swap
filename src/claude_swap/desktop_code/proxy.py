"""Authenticated loopback proxy; only native Desktop Code inference is rerouted.

Each request captures one account. Streams retain that account through completion;
there are no retries, replay, process kills or desktop-account mutations.
"""
import base64
import hmac
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from urllib.parse import urlsplit

from .transport import Transport, relay

HOP = {'connection', 'proxy-connection', 'proxy-authorization', 'keep-alive',
       'transfer-encoding', 'te', 'trailer', 'upgrade', 'content-length', 'host'}
MODEL_PATHS = {'/v1/messages', '/v1/messages/count_tokens'}


def is_code(headers, path):
    agent = headers.get('User-Agent', '')
    return (agent.startswith('claude-cli/') and agent.endswith(', claude-desktop)')
            and urlsplit(path).path in MODEL_PATHS)


def rewrite(body, headers, identity):
    encoding = next((v for k, v in headers.items() if k.lower() == 'content-encoding'), '').lower()
    if encoding not in ('', 'identity', 'gzip'):
        raise ValueError('Unsupported Code request encoding')
    data = json.loads(gzip.decompress(body) if encoding == 'gzip' else body)
    user = data.get('metadata', {}).get('user_id')
    if isinstance(user, str):
        try:
            metadata = json.loads(user)
        except ValueError:
            # Do not guess legacy identity formats or leave a stale account id.
            raise ValueError('Unsupported Code metadata identity format')
        if not isinstance(metadata, dict):
            raise ValueError('Unsupported Code metadata identity format')
        metadata['account_uuid'] = identity.account_uuid
        data['metadata']['user_id'] = json.dumps(metadata, separators=(',', ':'))
    result = {k: v for k, v in headers.items()
              if k.lower() not in HOP | {'authorization', 'x-api-key', 'x-organization-uuid',
                                         'anthropic-organization-id'}}
    result['Authorization'] = 'Bearer ' + identity.token
    if identity.organization_uuid:
        result['x-organization-uuid'] = identity.organization_uuid
    beta_value = next((v for k, v in result.items() if k.lower() == 'anthropic-beta'), '')
    result = {k: v for k, v in result.items() if k.lower() != 'anthropic-beta'}
    beta = [v.strip() for v in beta_value.split(',') if v.strip()]
    if 'oauth-2025-04-20' not in beta:
        beta.append('oauth-2025-04-20')
    result['anthropic-beta'] = ','.join(beta)
    payload = json.dumps(data, separators=(',', ':')).encode()
    return (gzip.compress(payload) if encoding == 'gzip' else payload), result


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, context, identity, secret, transport=None, address=('127.0.0.1', 0)):
        super().__init__(address, ConnectHandler)
        self.context, self.identity = context, identity
        self.expected_auth = 'Basic ' + base64.b64encode(('cswap:' + secret).encode()).decode()
        self.transport = transport or Transport()
        self.api_host, self.api_port, self.api_secure = 'api.anthropic.com', 443, True
        self.record = lambda identity: None

    def handle_error(self, request, client_address):
        # Never dump exception context/headers containing credentials.
        pass


class ConnectHandler(BaseHTTPRequestHandler):
    rbufsize = 0
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        if not hmac.compare_digest(self.headers.get('Proxy-Authorization', ''), self.server.expected_auth):
            self.send_error(407)
            return
        try:
            host, port = self.path.rsplit(':', 1)
            port = int(port)
            if host.lower() == 'api.anthropic.com' and port == 443:
                self.send_response(200, 'Connection Established')
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                with self.server.context.wrap_socket(self.connection, server_side=True) as tls:
                    InferenceHandler(tls, self.client_address, self.server)
            else:
                upstream = self.server.transport.tunnel(host, port)
                self.send_response(200, 'Connection Established')
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                relay(self.connection, upstream)
        except (OSError, ValueError, RuntimeError):
            self.close_connection = True


class InferenceHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def forward(self):
        upstream = None
        started = False
        try:
            if self.headers.get('Transfer-Encoding'):
                self.send_error(411, 'A Content-Length is required')
                return
            length = int(self.headers.get('Content-Length', 0))
            if not 0 <= length <= 128 * 1024 * 1024:
                self.send_error(413)
                return
            path = urlsplit(self.path)
            if path.scheme or not self.path.startswith('/'):
                self.send_error(400)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                return
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            if self.command == 'POST' and is_code(self.headers, self.path):
                identity = self.server.identity()
                body, headers = rewrite(body, headers, identity)
                self.server.record(identity)
            else:
                self.server.record(None)
            upstream = self.server.transport.connection(self.server.api_host, self.server.api_port,
                                                        secure=self.server.api_secure)
            upstream.request(self.command, self.path, body=body or None, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status)
            for k, v in response.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            started = True
            while chunk := response.read1(65536):
                self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except Exception:
            if not started:
                # No token values, request bodies or upstream exception strings.
                self.send_error(502, 'Desktop Code routing failed; check cswap account status')
            self.close_connection = True
        finally:
            if upstream:
                upstream.close()

    do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = forward
