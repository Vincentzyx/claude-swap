"""Configure routing and supervise its lifetime around a normal Desktop launch."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading

from claude_swap.desktop import DesktopManager
from claude_swap.exceptions import SwitchError
from claude_swap.paths import get_backup_root
from .certificates import prepare
from .credentials import ActiveCredentials
from .proxy import Server
from .transport import Transport
from .state import State


def process_environment(env, proxy, ca):
    result = env.copy()
    for key in ('https_proxy', 'HTTPS_PROXY'):
        result[key] = proxy
    # Model API traffic must reach this proxy even if an inherited bypass
    # includes anthropic.com. Original desktop Chromium routing stays in its
    # --proxy-server command line, and other CONNECT hosts are forwarded.
    bypass = env.get('no_proxy') or env.get('NO_PROXY') or ''
    kept = []
    for item in bypass.split(','):
        domain = item.strip().lower().split(':')[0].lstrip('*.')
        if item.strip() == '*' or (domain and 'api.anthropic.com'.endswith(domain)):
            continue
        if item.strip():
            kept.append(item.strip())
    result['no_proxy'] = result['NO_PROXY'] = ','.join(dict.fromkeys(['localhost', '127.0.0.1', '::1', *kept]))
    result['NODE_EXTRA_CA_CERTS'] = str(ca)
    return result


def run(manager, command):
    if not manager.settings().get('codeRouting', False):
        os.execvpe(command[0], command, os.environ)
    if manager.enabled():
        raise SwitchError('Turn off desktop login sync before using request-only routing')
    if manager.running():
        raise SwitchError('Desktop is already running. Finish your work and reopen it normally; no task was stopped.')
    root = manager.root / 'code-routing'
    context, ca = prepare(root, os.environ.get('NODE_EXTRA_CA_CERTS'))
    upstream = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY')
    transport = Transport(upstream)
    secret = secrets.token_urlsafe(32)
    with Server(context, ActiveCredentials(), secret, transport) as server:
        state = State(root / 'status.json')
        server.record = state.record
        proxy = f'http://cswap:{secret}@127.0.0.1:{server.server_port}'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            return subprocess.call(command, env=process_environment(os.environ, proxy, ca))
        finally:
            server.shutdown()
            state.close()


def main(argv):
    parser = argparse.ArgumentParser(prog='cswap desktop code')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('on', 'off', 'status', 'install-launcher'):
        sub.add_parser(name)
    sub.add_parser('exec').add_argument('app_command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        raise SwitchError('Desktop Code integration currently supports macOS only')
    manager = DesktopManager(get_backup_root())
    if args.command == 'exec':
        if not args.app_command:
            parser.error('exec requires an application executable')
        sys.exit(run(manager, args.app_command))
    elif args.command in ('on', 'off'):
        if args.command == 'on' and manager.enabled():
            raise SwitchError('Run cswap desktop sync off before enabling request-only routing')
        manager.configure(codeRouting=args.command == 'on')
        print(f'Desktop Code routing: {args.command}; applies on the next normal Desktop launch. No running task was stopped.')
    elif args.command == 'install-launcher':
        # This shim also works behind the existing authenticated proxy bridge:
        # that bridge passes Chromium flags through, independently of Code's
        # process-scoped Node proxy and CA.
        path = Path.home() / '.local/lib/claude-desktop/launch'
        path.parent.mkdir(parents=True, exist_ok=True)
        cswap = Path(sys.executable).parent / 'cswap'
        import shlex
        path.write_text('#!/bin/sh\nexec ' + shlex.quote(str(cswap)) +
                        ' desktop code exec /Applications/Claude.app/Contents/MacOS/Claude "$@"\n')
        path.chmod(0o700)
        print(f'Launcher installed: {path}')
    else:
        print('Desktop Code routing: ' + ('on' if manager.settings().get('codeRouting') else 'off'))
        print('Certificate trust: process only; system Keychain is unchanged')
        from claude_swap.desktop import read_json
        state = read_json(manager.root / 'code-routing/status.json')
        if state:
            alive = False
            if state.get('running'):
                try:
                    os.kill(state['pid'], 0)
                    alive = True
                except ProcessLookupError:
                    pass
            print(f"Routing process: {'running' if alive else 'stopped'}")
            print(f"Last run: {state.get('routedRequests', 0)} routed Code requests, "
                  f"{state.get('unchangedRequests', 0)} unchanged API requests")
            if state.get('lastAccount'):
                print(f"Last selected account UUID prefix: {state['lastAccount']}")
        else:
            print('Not connected yet: finish existing work and reopen Desktop through the installed launcher')
