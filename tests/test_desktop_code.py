"""Request routing tests: local TLS, synthetic credentials, no real account use."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import threading
from unittest.mock import Mock

import pytest

from claude_swap.desktop_code.certificates import prepare
from claude_swap.desktop_code.credentials import Identity
from claude_swap.desktop_code.proxy import Server, is_code, rewrite
from claude_swap.desktop_code.cli import process_environment, run

CODE_UA = 'claude-cli/2.1.302 (external, claude-desktop)'
A = Identity('synthetic-token-a', '11111111-1111-4111-8111-111111111111', 'org-a')
B = Identity('synthetic-token-b', '22222222-2222-4222-8222-222222222222', 'org-b')


def body():
    return json.dumps({'messages': [], 'metadata': {'user_id': json.dumps({
        'account_uuid': 'desktop-login-unchanged', 'device_id': 'device', 'session_id': 'session'})}}).encode()


@pytest.fixture
def routing(tmp_path):
    context, ca = prepare(tmp_path / 'certs')
    state = {'identity': A, 'requests': [], 'hold': False}
    first = threading.Event(); finish = threading.Event()
    class Backend(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            state['requests'].append((dict(self.headers), data, self.path))
            data = b'first-last'
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(b'first'); self.wfile.flush(); first.set()
            if state['hold']:
                assert finish.wait(5)
            self.wfile.write(b'-last'); self.wfile.flush()
        def log_message(self, *_): pass
    backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    backend.daemon_threads = True
    proxy = Server(context, lambda: state['identity'], 'secret')
    proxy.api_host, proxy.api_port, proxy.api_secure = '127.0.0.1', backend.server_port, False
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (backend, proxy)]
    for t in threads: t.start()
    trust = ssl.create_default_context(cafile=str(ca))
    def connect(auth=True):
        conn = http.client.HTTPSConnection('127.0.0.1', proxy.server_port, context=trust, timeout=5)
        conn.set_tunnel('api.anthropic.com', 443, headers={'Proxy-Authorization': proxy.expected_auth} if auth else {})
        return conn
    yield state, proxy, connect, first, finish, ca
    finish.set()
    for s in (proxy, backend): s.shutdown(); s.server_close()


def test_switch_at_request_boundary_preserves_inflight_stream(routing):
    state, _, connect, first, finish, _ = routing
    conn = connect(); state['hold'] = True
    conn.request('POST', '/v1/messages?beta=true', body(), {'User-Agent': CODE_UA, 'Authorization': 'Bearer desktop-token'})
    response = conn.getresponse()
    assert first.wait(3)
    assert response.read(5) == b'first'
    state['identity'] = B  # cswap switches while A's stream is still open.
    finish.set()
    assert response.read() == b'-last'
    state['hold'] = False
    conn.request('POST', '/v1/messages', body(), {'User-Agent': CODE_UA, 'Authorization': 'Bearer desktop-token'})
    assert conn.getresponse().read() == b'first-last'
    assert [r[0]['Authorization'] for r in state['requests']] == ['Bearer synthetic-token-a', 'Bearer synthetic-token-b']
    parsed = json.loads(json.loads(state['requests'][1][1])['metadata']['user_id'])
    assert parsed['account_uuid'] == B.account_uuid and parsed['session_id'] == 'session'
    conn.close()


@pytest.mark.parametrize('ua,path', [
    ('claude-cli/2.1.302 (external, local-agent)', '/v1/messages'),
    (CODE_UA, '/api/claude_code/policy_limits'),
    ('claude-cli/2.1.302 (external, cli)', '/v1/messages'),
    ('claude-cli/2.1.302 (external, claude-desktop-3p)', '/v1/messages'),
])
def test_non_code_or_non_inference_retains_original_credentials(routing, ua, path):
    state, _, connect, *_ = routing
    conn = connect(); original = body()
    conn.request('POST', path, original, {'User-Agent': ua, 'Authorization': 'Bearer desktop-token'})
    assert conn.getresponse().read() == b'first-last'
    headers, captured, _ = state['requests'][0]
    assert headers['Authorization'] == 'Bearer desktop-token' and captured == original
    conn.close()


def test_proxy_requires_authentication(routing):
    state, _, connect, *_ = routing
    conn = connect(False)
    with pytest.raises(OSError, match='407'):
        conn.request('POST', '/v1/messages', body(), {'User-Agent': CODE_UA})
    assert not state['requests']
    conn.close()


def test_fail_closed_when_account_unavailable(routing):
    state, proxy, connect, *_ = routing
    proxy.identity = Mock(side_effect=RuntimeError('credential-secret-must-not-leak'))
    conn = connect()
    conn.request('POST', '/v1/messages', body(), {'User-Agent': CODE_UA})
    result = conn.getresponse()
    assert result.status == 502
    assert b'credential-secret' not in result.read()
    assert not state['requests']
    conn.close()


def test_does_not_overwrite_chromium_route_or_global_environment():
    original = {'HTTPS_PROXY': 'http://existing-proxy:7897', 'NODE_EXTRA_CA_CERTS': '/original.pem'}
    env = process_environment(original, 'http://cswap:local@127.0.0.1:12345', Path('/process-ca.pem'))
    assert original['HTTPS_PROXY'] == 'http://existing-proxy:7897'
    assert env['HTTPS_PROXY'].startswith('http://cswap:local@127.0.0.1:')
    assert env['NODE_EXTRA_CA_CERTS'] == '/process-ca.pem'


def test_running_desktop_is_never_stopped(tmp_path):
    manager = Mock(); manager.settings.return_value = {'codeRouting': True}; manager.enabled.return_value = False
    manager.running.return_value = True
    with pytest.raises(Exception, match='already running'):
        run(manager, ['/Applications/Claude.app/Contents/MacOS/Claude'])
    manager.quit.assert_not_called()


def test_unsupported_metadata_fails_without_guessing():
    with pytest.raises(ValueError, match='Unsupported'):
        rewrite(b'{"metadata":{"user_id":"legacy-unknown-format"}}', {}, A)


def test_live_credential_resolver_reuses_existing_refresh_gate(tmp_path, monkeypatch):
    import contextlib
    import time
    from claude_swap.desktop_code import credentials
    from claude_swap.desktop_code.credentials import ActiveCredentials
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'oauthAccount': {'accountUuid': A.account_uuid}}))
    monkeypatch.setattr(credentials, 'get_global_config_path', lambda: config)
    monkeypatch.setattr(credentials, 'claude_credentials_lock', contextlib.nullcontext)
    monkeypatch.setattr(credentials, 'claude_config_lock', contextlib.nullcontext)
    switcher = Mock(); switcher.lock_file = tmp_path / '.lock'
    switcher._get_sequence_data.return_value = {'activeAccountNumber': 1, 'accounts': {'1': {
        'uuid': A.account_uuid, 'organizationUuid': A.organization_uuid, 'email': 'synthetic@example.invalid'}}}
    values = [json.dumps({'claudeAiOauth': {'accessToken': 'expired', 'expiresAt': 1}})]
    switcher._read_credentials.side_effect = lambda: values[0]
    def refresh(*_):
        values[0] = json.dumps({'claudeAiOauth': {'accessToken': A.token, 'expiresAt': (time.time() + 3600) * 1000}})
    switcher._fetch_active_usage.side_effect = refresh
    monkeypatch.setattr('claude_swap.switcher.ClaudeAccountSwitcher', lambda: switcher)
    assert ActiveCredentials()() == A
    switcher._fetch_active_usage.assert_called_once()
    config.write_text(json.dumps({'oauthAccount': {'accountUuid': B.account_uuid}}))
    with pytest.raises(RuntimeError, match='does not match'):
        ActiveCredentials()()


def test_extra_trust_is_preserved_without_modifying_source(tmp_path):
    _, original = prepare(tmp_path / 'original')
    before = original.read_bytes()
    _, combined = prepare(tmp_path / 'routing', str(original))
    assert original.read_bytes() == before
    assert combined.read_bytes().count(b'BEGIN CERTIFICATE') == 2
    assert (combined.stat().st_mode & 0o077) == 0


def test_removes_only_model_host_proxy_bypass():
    env = process_environment({'no_proxy': 'localhost,.anthropic.com,192.168.0.0/16,*.local'}, 'local', Path('/ca'))
    assert 'anthropic.com' not in env['NO_PROXY']
    assert '192.168.0.0/16' in env['NO_PROXY'] and '*.local' in env['NO_PROXY']


def test_compressed_code_metadata_and_case_insensitive_beta():
    import gzip
    payload, headers = rewrite(gzip.compress(body()), {'Content-Encoding': 'gzip', 'ANTHROPIC-BETA': 'existing-beta'}, B)
    assert headers['anthropic-beta'] == 'existing-beta,oauth-2025-04-20'
    assert json.loads(json.loads(gzip.decompress(payload))['metadata']['user_id'])['account_uuid'] == B.account_uuid


def test_unrelated_hosts_are_opaque_tunnels(routing):
    import socket
    state, proxy, _, *_ = routing
    left, right = socket.socketpair()
    proxy.transport.tunnel = Mock(return_value=right)
    client = socket.create_connection(('127.0.0.1', proxy.server_port), timeout=3)
    try:
        client.sendall(('CONNECT claude.ai:443 HTTP/1.1\r\nHost: claude.ai\r\nProxy-Authorization: ' + proxy.expected_auth + '\r\n\r\n').encode())
        response = b''
        while not response.endswith(b'\r\n\r\n'):
            response += client.recv(1)
        assert b'200' in response
        client.sendall(b'opaque-TLS-login-data')
        assert left.recv(100) == b'opaque-TLS-login-data'
        left.sendall(b'unchanged')
        assert client.recv(100) == b'unchanged'
        assert not state['requests']
    finally:
        client.close(); left.close()


def test_launch_supervisor_scopes_proxy_and_closes_state(tmp_path, monkeypatch):
    manager = Mock()
    manager.root = tmp_path
    manager.settings.return_value = {'codeRouting': True}
    manager.enabled.return_value = False
    manager.running.return_value = False
    call = Mock(return_value=7)
    monkeypatch.setattr('claude_swap.desktop_code.cli.subprocess.call', call)
    monkeypatch.delenv('NODE_EXTRA_CA_CERTS', raising=False)
    assert run(manager, ['/synthetic/Claude', '--proxy-server=http://original:7897']) == 7
    command = call.call_args.args[0]
    env = call.call_args.kwargs['env']
    assert command[-1] == '--proxy-server=http://original:7897'
    assert env['HTTPS_PROXY'].startswith('http://cswap:')
    assert Path(env['NODE_EXTRA_CA_CERTS']).exists()
    state = json.loads((tmp_path / 'code-routing/status.json').read_text())
    assert state['running'] is False
    assert 'token' not in json.dumps(state)
    manager.quit.assert_not_called()
