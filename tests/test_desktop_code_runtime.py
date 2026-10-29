"""Optional real Code-runtime probe against a synthetic local upstream.

CSWAP_TEST_CLAUDE_BINARY=/path/to/claude pytest tests/test_desktop_code_runtime.py
No real tokens, prompts or internet inference are used.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_swap.desktop_code.certificates import prepare
from claude_swap.desktop_code.cli import process_environment
from claude_swap.desktop_code.credentials import Identity
from claude_swap.desktop_code.proxy import Server

BINARY = os.environ.get('CSWAP_TEST_CLAUDE_BINARY')
pytestmark = pytest.mark.skipif(not BINARY, reason='set CSWAP_TEST_CLAUDE_BINARY for the native runtime probe')


def sse():
    parts = [
        ('message_start', {'type': 'message_start', 'message': {'id': 'msg_synthetic', 'type': 'message',
         'role': 'assistant', 'content': [], 'model': 'claude-sonnet-4-5', 'stop_reason': None,
         'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
        ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'probe OK'}}),
        ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
        ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 2}}),
        ('message_stop', {'type': 'message_stop'}),
    ]
    return ''.join(f'event: {name}\ndata: {json.dumps(data)}\n\n' for name, data in parts).encode()


def test_same_native_cli_process_continues_across_request_account_switch(tmp_path):
    context, ca = prepare(tmp_path / 'certs')
    state = {'identity': Identity('synthetic-a', '11111111-1111-4111-8111-111111111111', 'org-a'), 'tokens': []}
    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401); self.send_header('Content-Length', '0'); self.end_headers()
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if not self.path.startswith('/v1/messages'):
                self.do_GET(); return
            state['tokens'].append(self.headers.get('Authorization'))
            response = sse()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(response))); self.end_headers()
            self.wfile.write(response); self.wfile.flush()
        def log_message(self, *_): pass
    backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    backend.daemon_threads = True
    proxy = Server(context, lambda: state['identity'], 'runtime-secret')
    proxy.api_host, proxy.api_port, proxy.api_secure = '127.0.0.1', backend.server_port, False
    for server in (proxy, backend): threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('ANTHROPIC_', 'CLAUDE_', 'NODE_EXTRA_CA_CERTS')) and 'proxy' not in k.lower()}
    env = process_environment(env, f'http://cswap:runtime-secret@127.0.0.1:{proxy.server_port}', ca)
    config = tmp_path / 'config'; config.mkdir()
    (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
    env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_OAUTH_TOKEN='desktop-synthetic-token',
               ANTHROPIC_BASE_URL='https://api.anthropic.com', CLAUDE_CODE_ENTRYPOINT='claude-desktop',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_TELEMETRY='1')
    proc = subprocess.Popen([BINARY, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                             '--verbose', '--model', 'claude-sonnet-4-5', '--tools', '', '--setting-sources', 'user'],
                            env=env, cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    events = queue.Queue()
    def read():
        for line in proc.stdout:
            try: events.put(json.loads(line))
            except ValueError: pass
        events.put({'type': 'process-exit'})
    threading.Thread(target=read, daemon=True).start()
    try:
        pid = proc.pid
        for account in ('a', 'b'):
            if account == 'b':
                state['identity'] = Identity('synthetic-b', '22222222-2222-4222-8222-222222222222', 'org-b')
            proc.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'isolated routing probe'}}) + '\n')
            proc.stdin.flush()
            while True:
                event = events.get(timeout=30)
                assert event['type'] != 'process-exit'
                if event['type'] == 'result':
                    assert event.get('is_error') is not True, event.get('subtype')
                    break
            assert proc.pid == pid and proc.poll() is None
        assert state['tokens'] == ['Bearer synthetic-a', 'Bearer synthetic-b']
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        for server in (proxy, backend): server.shutdown(); server.server_close()
