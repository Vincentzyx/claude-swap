"""Research probe: native stdio credential updates, without any request proxy.

Run explicitly with CSWAP_TEST_CLAUDE_BINARY. The HTTP server stands in for the
vendor and returns synthetic responses; it never forwards requests anywhere.
"""
import json
import os
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_desktop_code_runtime import sse

BINARY = os.environ.get('CSWAP_TEST_CLAUDE_BINARY')
pytestmark = pytest.mark.skipif(not BINARY, reason='native runtime research probe requires an explicit executable')


def test_config_change_is_ignored_but_owned_stdio_can_update_token(tmp_path):
    captured = []
    class Vendor(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            captured.append(self.headers.get('Authorization'))
            response = sse()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(response))); self.end_headers(); self.wfile.write(response)
        def log_message(self, *_): pass
    vendor = ThreadingHTTPServer(('127.0.0.1', 0), Vendor)
    vendor.daemon_threads = True
    threading.Thread(target=vendor.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('ANTHROPIC_', 'CLAUDE_')) and 'proxy' not in k.lower()}
    config = tmp_path / 'config'; config.mkdir()
    (config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True}))
    env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_OAUTH_TOKEN='launch-token-a',
               ANTHROPIC_BASE_URL=f'http://127.0.0.1:{vendor.server_port}',
               CLAUDE_CODE_ENTRYPOINT='claude-desktop', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               DISABLE_TELEMETRY='1')
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
    def send(message):
        proc.stdin.write(json.dumps(message) + '\n'); proc.stdin.flush()
    def wait(kind, request_id=None):
        while True:
            event = events.get(timeout=20)
            assert event['type'] != 'process-exit'
            if event['type'] == kind and (request_id is None or event.get('response', {}).get('request_id') == request_id):
                return event
    def prompt():
        send({'type': 'user', 'message': {'role': 'user', 'content': 'isolated direct credential probe'}})
        assert wait('result').get('is_error') is not True
    try:
        pid = proc.pid
        prompt()
        (config / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'config-token-b', 'expiresAt': (time.time() + 3600) * 1000}}))
        prompt()
        send({'type': 'update_environment_variables', 'variables': {'CLAUDE_CODE_OAUTH_TOKEN': 'control-token-b'},
              'request_id': 'credential-probe'})
        ack = wait('control_response', 'credential-probe')
        assert ack['response']['subtype'] == 'success'
        prompt()
        assert proc.pid == pid and proc.poll() is None
        assert captured == ['Bearer launch-token-a', 'Bearer launch-token-a', 'Bearer control-token-b']
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        vendor.shutdown(); vendor.server_close()
