"""Read a coherent live CLI identity and token, using the existing refresh gate."""
from dataclasses import dataclass
import json
import threading

from claude_swap import oauth
from claude_swap.claude_locks import claude_config_lock, claude_credentials_lock
from claude_swap.locking import FileLock
from claude_swap.paths import get_global_config_path


@dataclass(frozen=True, repr=False)
class Identity:
    token: str
    account_uuid: str
    organization_uuid: str


class ActiveCredentials:
    def __init__(self):
        self.lock = threading.Lock()

    def __call__(self) -> Identity:
        # Only serialize local snapshot/refresh work; never hold this lock while
        # forwarding a model response or the independent auto-switch loop.
        with self.lock:
            from claude_swap.switcher import ClaudeAccountSwitcher
            switcher = ClaudeAccountSwitcher()
            for attempt in range(2):
                with FileLock(switcher.lock_file), claude_credentials_lock(), claude_config_lock():
                    data = switcher._get_sequence_data() or {}
                    number = str(data.get('activeAccountNumber', ''))
                    account = data.get('accounts', {}).get(number, {})
                    live = json.loads(get_global_config_path().read_text()).get('oauthAccount', {})
                    if not account.get('uuid') or live.get('accountUuid') != account['uuid']:
                        raise RuntimeError('The live CLI login does not match the active managed account')
                    if (live.get('organizationUuid') and account.get('organizationUuid')
                            and live['organizationUuid'] != account['organizationUuid']):
                        raise RuntimeError('The live CLI organization does not match the active managed account')
                    raw = switcher._read_credentials()
                    auth = oauth.extract_oauth_data(raw) if raw else None
                    if not auth or not auth.get('accessToken'):
                        raise RuntimeError('Desktop Code routing requires a managed OAuth login')
                    expired = oauth.is_oauth_token_expired(auth.get('expiresAt'))
                    if not expired:
                        return Identity(auth['accessToken'], account['uuid'], account.get('organizationUuid', ''))
                if attempt == 0:
                    # Reuse consumption locks and persistence, never refresh a
                    # copied refresh token independently or revive an old slot.
                    switcher._fetch_active_usage(number, account.get('email', ''), raw,
                                                 account.get('organizationUuid', ''))
            raise RuntimeError('The active CLI credential is expired; refresh it before retrying')
