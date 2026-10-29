"""Desktop tests only touch temporary profiles; never access real app/Keychain."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from claude_swap.desktop import DesktopManager, sync_after_switch
from claude_swap.exceptions import SwitchError

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'


@pytest.fixture
def desktop(tmp_path):
    manager = DesktopManager(tmp_path / 'backup', tmp_path / 'Claude')
    manager.running = Mock(return_value=False)
    manager.quit = Mock()
    manager.launch = Mock()
    return manager


def login(manager, uuid, cookie):
    manager.user_data.mkdir(parents=True, exist_ok=True)
    (manager.user_data / 'config.json').write_text(json.dumps({
        'lastKnownAccountUuid': uuid, 'oauth:tokenCacheV2': 'encrypted-' + uuid,
        'theme': 'dark',
    }))
    (manager.user_data / 'Cookies').write_text(cookie)
    storage = manager.user_data / 'Local Storage'
    storage.mkdir(exist_ok=True)
    (storage / 'account').write_text(uuid)


def test_save_and_switch_preserves_machine_data(desktop):
    login(desktop, A, 'cookie-a')
    desktop.save(A)
    login(desktop, B, 'cookie-b')
    desktop.save(B)
    work = desktop.user_data / 'claude-code-sessions' / 'work.json'
    work.parent.mkdir(); work.write_text('session')
    desktop.switch(A)
    assert desktop.account() == A
    assert (desktop.user_data / 'Cookies').read_text() == 'cookie-a'
    assert (desktop.user_data / 'Local Storage/account').read_text() == A
    assert json.loads((desktop.user_data / 'config.json').read_text())['theme'] == 'dark'
    assert work.read_text() == 'session'
    assert desktop._profile(B).joinpath('Cookies').read_text() == 'cookie-b'
    desktop.launch.assert_not_called()


def test_wrong_identity_never_overwrites_snapshot(desktop):
    login(desktop, A, 'cookie-a')
    with pytest.raises(SwitchError, match='does not match'):
        desktop.save(B)
    assert not desktop._profile(B).exists()


def test_missing_profile_does_not_quit_or_change_login(desktop):
    login(desktop, A, 'cookie-a')
    with pytest.raises(SwitchError, match='No verified'):
        desktop.switch(B)
    desktop.quit.assert_not_called()
    assert desktop.account() == A


def test_switch_rollback_restores_outgoing_login(desktop, monkeypatch):
    login(desktop, A, 'cookie-a'); desktop.save(A)
    login(desktop, B, 'cookie-b')
    restore = desktop._restore
    def fail_target(path):
        restore(path)
        if path == desktop._profile(A):
            raise OSError('simulated write failure')
    monkeypatch.setattr(desktop, '_restore', fail_target)
    with pytest.raises(OSError):
        desktop.switch(A)
    assert desktop.account() == B
    assert (desktop.user_data / 'Cookies').read_text() == 'cookie-b'
    assert (desktop.root / 'recovery').exists()


def test_running_app_restarts_but_same_account_does_not(desktop):
    login(desktop, A, 'a'); desktop.save(A)
    login(desktop, B, 'b')
    desktop.running.return_value = True
    desktop.switch(A)
    desktop.launch.assert_called_once()
    desktop.quit.reset_mock(); desktop.launch.reset_mock()
    desktop.switch(A)
    desktop.quit.assert_not_called(); desktop.launch.assert_not_called()


def test_desktop_failure_does_not_masquerade_as_cli_failure():
    manager = Mock()
    manager.switch.side_effect = RuntimeError('restart failed')
    warnings = []
    sync_after_switch(manager, A, warnings)
    assert 'CLI switched, but Desktop sync failed' in warnings[0]


def test_login_parks_session_without_server_logout(desktop):
    login(desktop, A, 'cookie-a')
    desktop.login()
    assert desktop.account() is None
    assert not (desktop.user_data / 'Cookies').exists()
    desktop.validate(A)
    desktop.launch.assert_called_once()


def test_snapshot_permissions(desktop):
    login(desktop, A, 'cookie-a'); desktop.save(A)
    for path in desktop._profile(A).rglob('*'):
        assert path.stat().st_mode & 0o077 == 0


def test_switcher_shared_entry_covers_all_callers(tmp_path, monkeypatch):
    from claude_swap.switcher import ClaudeAccountSwitcher
    manager = Mock()
    manager.root = tmp_path
    manager.enabled.return_value = True
    monkeypatch.setattr('claude_swap.desktop.DesktopManager', lambda _: manager)
    switcher = object.__new__(ClaudeAccountSwitcher)
    switcher.backup_dir = tmp_path
    switcher._get_sequence_data = lambda: {'accounts': {'2': {'uuid': B}}}
    core = Mock(return_value={'from': {'number': 1}, 'to': {'number': 2}, 'warnings': []})
    switcher._perform_switch_core = core
    result = switcher._perform_switch('2', emit_output=False)
    manager.validate.assert_called_once_with(B)
    manager.switch.assert_called_once_with(B)
    assert not result['warnings']


def test_missing_desktop_login_fails_before_cli_mutation(tmp_path, monkeypatch):
    from claude_swap.switcher import ClaudeAccountSwitcher
    manager = Mock()
    manager.root = tmp_path
    manager.enabled.return_value = True
    manager.validate.side_effect = SwitchError('missing desktop profile')
    monkeypatch.setattr('claude_swap.desktop.DesktopManager', lambda _: manager)
    switcher = object.__new__(ClaudeAccountSwitcher)
    switcher.backup_dir = tmp_path
    switcher._get_sequence_data = lambda: {'accounts': {'2': {'uuid': B}}}
    switcher._perform_switch_core = Mock()
    with pytest.raises(SwitchError):
        switcher._perform_switch('2', emit_output=False)
    switcher._perform_switch_core.assert_not_called()


def test_recovery_command_restores_interrupted_switch(desktop):
    login(desktop, A, 'cookie-a')
    desktop.root.mkdir(parents=True)
    desktop._capture(desktop.root / 'recovery')
    login(desktop, B, 'cookie-b')
    desktop.recover()
    assert desktop.account() == A
    assert not (desktop.root / 'recovery').exists()


def test_incomplete_capture_is_not_published_as_recovery(desktop, monkeypatch):
    login(desktop, A, 'a')
    desktop.root.mkdir(parents=True)
    def fail(source, destination, *args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr('claude_swap.desktop.shutil.copy2', fail)
    with pytest.raises(OSError):
        desktop._capture(desktop.root / 'recovery')
    assert not (desktop.root / 'recovery').exists()
    assert desktop.account() == A
