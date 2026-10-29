"""HTTP CONNECT and upstream connections, preserving the original network proxy."""
import base64
import http.client
import select
import socket
import ssl
from urllib.parse import unquote, urlsplit


def proxy_parts(url):
    p = urlsplit(url) if url else None
    if p and (p.scheme != 'http' or not p.hostname):
        raise ValueError('Desktop Code routing currently supports HTTP upstream proxies only')
    return p


def proxy_headers(p):
    if p is None or p.username is None:
        return {}
    value = f'{unquote(p.username)}:{unquote(p.password or "")}'.encode()
    return {'Proxy-Authorization': 'Basic ' + base64.b64encode(value).decode()}


class Transport:
    def __init__(self, proxy=None):
        self.proxy = proxy_parts(proxy)

    def connection(self, host, port, *, secure=True):
        if not secure:
            return http.client.HTTPConnection(host, port, timeout=60)
        p = self.proxy
        conn = http.client.HTTPSConnection(p.hostname if p else host,
                                          (p.port or 80) if p else port,
                                          timeout=600, context=ssl.create_default_context())
        if p:
            conn.set_tunnel(host, port, headers=proxy_headers(p))
        return conn

    def tunnel(self, host, port):
        p = self.proxy
        sock = socket.create_connection((p.hostname, p.port or 80) if p else (host, port), timeout=15)
        try:
            if p:
                value = f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n'
                value += ''.join(f'{k}: {v}\r\n' for k, v in proxy_headers(p).items()) + '\r\n'
                sock.sendall(value.encode())
                response = http.client.HTTPResponse(sock)
                response.begin()
                if response.status != 200:
                    raise RuntimeError('The upstream proxy refused the connection')
            sock.settimeout(None)
            return sock
        except Exception:
            sock.close()
            raise


def relay(left, right):
    with right:
        while True:
            ready, _, _ = select.select([left, right], [], [], 600)
            if not ready:
                return
            for src in ready:
                data = src.recv(65536)
                if not data:
                    return
                (right if src is left else left).sendall(data)
