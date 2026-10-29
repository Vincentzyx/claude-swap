"""macOS Desktop login snapshots. Tokens remain encrypted by Electron safeStorage.

Only authentication state is swapped. Code transcripts, MCP configuration, VM
images and worktrees stay in place. A desktop login must be captured separately
from CLI OAuth; these credentials are not interchangeable.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from claude_swap.exceptions import SwitchError
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock

AUTH_KEYS = ("oauth:tokenCache", "oauth:tokenCacheV2", "lastKnownAccountUuid")
AUTH_FILES = ("Cookies", "Cookies-journal", "Network/Cookies", "Network/Cookies-journal",
              "Local Storage", "Session Storage", "IndexedDB", "WebStorage")
LAUNCHERS = ("default", "ccg", "ccgu", "ccgr")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


class DesktopManager:
    def __init__(self, backup_dir: Path, user_data: Path | None = None):
        self.root = backup_dir / "desktop"
        self.user_data = user_data or Path.home() / "Library/Application Support/Claude"
        self.settings_path = self.root / "settings.json"

    def settings(self) -> dict:
        return read_json(self.settings_path)

    def configure(self, **values) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        atomic_write_text(self.settings_path, json.dumps(self.settings() | values))

    def enabled(self) -> bool:
        return self.settings().get("sync", False) is True

    def _profile(self, uuid: str) -> Path:
        # Account UUIDs, never slot numbers/emails: aliases and slot moves do not
        # silently reassign another account's desktop login.
        import uuid as uuid_module
        try:
            identity = str(uuid_module.UUID(uuid))
        except (ValueError, TypeError, AttributeError) as exc:
            raise SwitchError("Desktop login requires a verified account UUID") from exc
        return self.root / "profiles" / identity

    def account(self) -> str | None:
        cfg = read_json(self.user_data / "config.json")
        return cfg.get("lastKnownAccountUuid")

    def validate(self, uuid: str) -> None:
        path = self._profile(uuid)
        meta = read_json(path / "auth.json")
        if meta.get("lastKnownAccountUuid") != uuid or not any(meta.get(k) for k in AUTH_KEYS[:2]):
            raise SwitchError("No verified Desktop login for this account. Log in in Desktop, then run cswap desktop save <account>.")

    @staticmethod
    def running() -> bool:
        return subprocess.run(["/usr/bin/pgrep", "-x", "Claude"], capture_output=True).returncode == 0

    def quit(self) -> None:
        if not self.running():
            return
        subprocess.run(["/usr/bin/osascript", "-e", 'tell application "Claude" to quit'],
                       check=True, capture_output=True, timeout=10)
        deadline = time.monotonic() + 30
        while self.running():
            if time.monotonic() >= deadline:
                raise SwitchError("Claude Desktop did not quit; finish its confirmation dialog and retry.")
            time.sleep(0.2)

    def launch(self) -> None:
        launcher = self.settings().get("launcher", "default")
        if launcher not in LAUNCHERS:
            raise SwitchError("Unknown Desktop launcher")
        args = ["/usr/bin/open", "-a", "Claude"] if launcher == "default" else ["/bin/zsh", "-lic", launcher]
        subprocess.run(args, check=True, capture_output=True, timeout=15)

    def _capture(self, destination: Path) -> None:
        # An interrupted capture must not look like a complete recovery image.
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            staged = Path(tmp) / "snapshot"
            self._capture_into(staged)
            staged.rename(destination)

    def _capture_into(self, destination: Path) -> None:
        destination.mkdir(parents=True, mode=0o700)
        cfg = read_json(self.user_data / "config.json")
        atomic_write_text(destination / "auth.json", json.dumps({k: cfg[k] for k in AUTH_KEYS if k in cfg}))
        for name in AUTH_FILES:
            src = self.user_data / name
            if src.exists():
                dst = destination / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
        # Snapshot content is credential-bearing, even when encrypted.
        for parent, dirs, files in os.walk(destination):
            os.chmod(parent, 0o700)
            for name in files:
                os.chmod(Path(parent) / name, 0o600)

    def _restore(self, source: Path) -> None:
        cfg = read_json(self.user_data / "config.json")
        for key in AUTH_KEYS:
            cfg.pop(key, None)
        cfg.update(read_json(source / "auth.json"))
        self.user_data.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_text(self.user_data / "config.json", json.dumps(cfg))
        for name in AUTH_FILES:
            dst = self.user_data / name
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink(missing_ok=True)
            src = source / name
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

    def _save_closed(self, uuid: str) -> None:
        target = self._profile(uuid)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            staged = Path(tmp) / "snapshot"
            self._capture(staged)
            old = target.with_name(target.name + ".old")
            if old.exists():
                # Never overwrite the only recoverable snapshot left by a crash.
                raise SwitchError(f"Interrupted Desktop save: recover {old} before retrying")
            if target.exists():
                target.rename(old)
            staged.rename(target)
            if old.exists():
                shutil.rmtree(old)

    def save(self, uuid: str) -> None:
        with FileLock(self.root / "desktop.lock"):
            was_running = self.running()
            self.quit()
            try:
                cfg = read_json(self.user_data / "config.json")
                if self.account() != uuid or not any(cfg.get(k) for k in AUTH_KEYS[:2]):
                    raise SwitchError("Desktop identity does not match this CLI account; no login saved.")
                self._save_closed(uuid)
            finally:
                if was_running:
                    self.launch()

    def switch(self, uuid: str, *, launch: bool = False) -> None:
        with FileLock(self.root / "desktop.lock"):
            self.validate(uuid)
            if self.account() == uuid:
                if launch and not self.running():
                    self.launch()
                return
            was_running = self.running()
            self.quit()
            recovery = self.root / "recovery"
            if recovery.exists():
                raise SwitchError(f"An interrupted Desktop switch needs recovery at {recovery}")
            self._capture(recovery)
            try:
                current = self.account()
                if current:
                    self._save_closed(current)
                self._restore(self._profile(uuid))
            except Exception:
                self._restore(recovery)
                if was_running:
                    self.launch()
                raise
            else:
                shutil.rmtree(recovery)
                if was_running or launch:
                    self.launch()

    def recover(self) -> None:
        with FileLock(self.root / "desktop.lock"):
            recovery = self.root / "recovery"
            if not (recovery / "auth.json").exists():
                raise SwitchError("No interrupted Desktop operation to recover")
            was_running = self.running()
            self.quit()
            self._restore(recovery)
            shutil.rmtree(recovery)
            if was_running:
                self.launch()

    def login(self) -> None:
        """Park the current login without sending logout/revoking it upstream."""
        with FileLock(self.root / "desktop.lock"):
            self.quit()
            current = self.account()
            if current:
                self._save_closed(current)
            # Preserve rollback state even for an unrecognised/old login.
            recovery = self.root / "recovery"
            if recovery.exists():
                raise SwitchError(f"An interrupted Desktop switch needs recovery at {recovery}")
            self._capture(recovery)
            try:
                with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                    self._restore(Path(tmp))
            except Exception:
                self._restore(recovery)
                raise
            else:
                shutil.rmtree(recovery)
            self.launch()


def sync_after_switch(manager: DesktopManager, uuid: str, warnings: list[str]) -> None:
    try:
        manager.switch(uuid)
    except Exception as exc:
        # The CLI transaction has committed. Never report it as failed or
        # retry/rotate another account just because Desktop could not restart.
        warnings.append(f"CLI switched, but Desktop sync failed: {exc}")


def main(argv: list[str]) -> None:
    if sys.platform != "darwin":
        raise SwitchError("Desktop integration currently supports macOS only")
    if argv and argv[0] == "code":
        from claude_swap.desktop_code.cli import main as code_main
        code_main(argv[1:])
        return
    from claude_swap.switcher import ClaudeAccountSwitcher
    parser = argparse.ArgumentParser(prog="cswap desktop")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("save", "switch"):
        sub.add_parser(name).add_argument("account")
    for name in ("list", "login", "recover"):
        sub.add_parser(name)
    sub.add_parser("sync").add_argument("mode", choices=("on", "off"))
    sub.add_parser("launcher").add_argument("name", choices=LAUNCHERS)
    sub.add_parser("code", help="Request-only native Code routing; see cswap desktop code --help")
    args = parser.parse_args(argv)
    switcher = ClaudeAccountSwitcher()
    manager = DesktopManager(switcher.backup_dir)
    if args.command in ("save", "switch"):
        num, _, _ = switcher.resolve_account(args.account)
        uuid = switcher._get_sequence_data()["accounts"][num].get("uuid")
        if not uuid:
            raise SwitchError("This slot has no verified account UUID; re-add its OAuth login first")
        if args.command == "save":
            manager.save(uuid)
        else:
            manager.switch(uuid, launch=True)
        print(f"Desktop {args.command}: Account-{num}")
    elif args.command == "recover":
        manager.recover()
        print("Desktop login recovered; verify CLI/Desktop identity before enabling sync.")
    elif args.command == "login":
        manager.login()
        print("Log in in Desktop, then run cswap desktop save <account>.")
    elif args.command == "launcher":
        manager.configure(launcher=args.name)
    elif args.command == "sync":
        if args.mode == "on":
            if manager.settings().get("codeRouting"):
                raise SwitchError("Turn off request-only routing before enabling desktop login sync")
            for account in (switcher._get_sequence_data() or {}).get("accounts", {}).values():
                manager.validate(account.get("uuid", ""))
        manager.configure(sync=args.mode == "on")
        print(f"Desktop sync: {args.mode}")
    else:
        print(f"Desktop sync: {'on' if manager.enabled() else 'off'}")
        for num, account in (switcher._get_sequence_data() or {}).get("accounts", {}).items():
            try:
                manager.validate(account.get("uuid", ""))
                state = "saved"
            except (ValueError, SwitchError):
                state = "not saved"
            print(f"Account-{num}: {state}")
