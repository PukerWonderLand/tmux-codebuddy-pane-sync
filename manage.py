#!/usr/bin/env python3
"""Install/uninstall the sync script, a per-user systemd timer, and CodeBuddy hooks.

No sudo and no pip required. The timer is the backstop; the hooks make a rename
visible as soon as the turn ends. Hook registration is additive and idempotent:
every other entry in ``settings.json`` is preserved byte for byte.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

APP = 'tmux-codebuddy-pane-sync'
ROOT = Path(__file__).resolve().parent
HOOK_BASENAME = 'codebuddy_pane_sync_hook.py'
HOOK_EVENTS = ('UserPromptSubmit', 'Stop')
HOOK_TIMEOUT_SECONDS = 15


def quote(value):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise ValueError('Paths may not contain newlines')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def systemctl(*args, check=True, quiet=False):
    return subprocess.run(['systemctl', '--user', *args], check=check,
                          stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None)


def is_our_hook(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('command'), str)
            and HOOK_BASENAME in entry['command'])


def add_hook(settings, event, command, timeout=HOOK_TIMEOUT_SECONDS):
    """Insert or refresh our hook for one event. Returns True if anything changed."""
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('settings.json "hooks" is not an object')
    groups = hooks.get(event)
    if not isinstance(groups, list):
        groups = []
        hooks[event] = groups
    found = False
    changed = False
    for group in groups:
        entries = group.get('hooks') if isinstance(group, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not is_our_hook(entry):
                continue
            if found or entry.get('command') != command or entry.get('timeout') != timeout:
                entry.update(command=command, timeout=timeout, type='command')
                changed = True
            found = True
    if not found:
        groups.append({'hooks': [{'type': 'command', 'command': command,
                                  'timeout': timeout}]})
        return True
    return changed


def remove_hook(settings, event):
    """Drop only our hooks for one event, keeping every other entry in order."""
    hooks = settings.get('hooks')
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return False
    kept = []
    changed = False
    for group in groups:
        entries = group.get('hooks') if isinstance(group, dict) else None
        if not isinstance(entries, list):
            kept.append(group)
            continue
        remaining = [entry for entry in entries if not is_our_hook(entry)]
        if len(remaining) == len(entries):
            kept.append(group)
            continue
        changed = True
        if remaining:
            kept.append(dict(group, hooks=remaining))
    if changed:
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return changed


def register_hooks(settings, command, events=HOOK_EVENTS):
    """Refresh every event. Deliberately not any(): that would short-circuit."""
    changed = False
    for event in events:
        if add_hook(settings, event, command):
            changed = True
    return changed


def unregister_hooks(settings, events=HOOK_EVENTS):
    changed = False
    for event in events:
        if remove_hook(settings, event):
            changed = True
    return changed


def load_settings(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise SystemExit(f'{path} is not readable JSON ({error}); refusing to modify it')
    if not isinstance(value, dict):
        raise SystemExit(f'{path} does not contain a JSON object; refusing to modify it')
    return value


def write_json(path, value, mode=None):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if mode is not None:
        temporary.chmod(mode)
    os.replace(temporary, path)


def backup(paths, state_dir):
    existing = [p for p in paths if p.exists()]
    if not existing:
        return None
    destination = state_dir / 'install-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    destination.mkdir(parents=True)
    for path in existing:
        shutil.copy2(path, destination / path.name)
    return destination


class Layout:
    def __init__(self, config, codebuddy_home):
        home = Path.home()
        self.config_root = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')).resolve()
        self.config_dir = self.config_root / APP
        self.config_file = self.config_dir / 'config.json'
        self.units = self.config_root / 'systemd/user'
        self.script = home / '.local/bin/tmux-codebuddy-pane-sync.py'
        self.restore_script = home / '.local/bin/codebuddy_restore.py'
        self.service = self.units / f'{APP}.service'
        self.timer = self.units / f'{APP}.timer'
        self.restore_service = self.units / f'{APP}-restore.service'
        self.codebuddy_home = Path(codebuddy_home or config.get('codebuddy_home')
                                  or os.environ.get('CODEBUDDY_HOME')
                                  or home / '.codebuddy').expanduser().resolve()
        self.hook = self.codebuddy_home / 'hooks' / HOOK_BASENAME
        self.settings = self.codebuddy_home / 'settings.json'
        self.state_dir = Path(config.get('state_dir') or Path(
            os.environ.get('XDG_STATE_HOME', home / '.local/state')) / APP).expanduser().resolve()


def hook_command(hook_path, python):
    return f'{shlex.quote(python)} {shlex.quote(str(hook_path))}'


def uninstall(layout, no_start):
    if not no_start:
        systemctl('disable', '--now', f'{APP}.timer', quiet=True)
        systemctl('stop', f'{APP}.service', check=False, quiet=True)
    try:
        settings = load_settings(layout.settings)
    except SystemExit as error:
        print(f'{error}\nLeft settings.json untouched.', file=sys.stderr)
        settings = None
    if settings is not None and unregister_hooks(settings):
        backup([layout.settings], layout.state_dir)
        write_json(layout.settings, settings, layout.settings.stat().st_mode & 0o777)
        print(f'Removed CodeBuddy hooks from {layout.settings}')
    if not no_start:
        systemctl('disable', '--now', f'{APP}-restore.service', quiet=True)
    for path in (layout.script, layout.hook, layout.service, layout.timer,
                 layout.restore_script, layout.restore_service):
        path.unlink(missing_ok=True)
    if not no_start:
        systemctl('daemon-reload')
    print(f'Uninstalled. Logs and configuration retained: {layout.config_file}')


def install(args, layout):
    if not shutil.which('tmux'):
        raise SystemExit('Install tmux first')
    if not args.no_start:
        subprocess.run(['systemctl', '--user', 'show-environment'], check=True,
                       stdout=subprocess.DEVNULL)
    config = json.loads(layout.config_file.read_text()) if layout.config_file.exists() else {}
    if not isinstance(config, dict):
        config = {}
    config['codebuddy_home'] = str(layout.codebuddy_home)
    config['state_dir'] = str(layout.state_dir)
    config['interval_minutes'] = args.interval_minutes or config.get('interval_minutes', 30)
    config['name_source'] = args.name_source or config.get('name_source', 'auto')
    if args.workbuddy_command:
        config['workbuddy_command'] = args.workbuddy_command
    if args.restore_layout:
        config['restore_layout'] = args.restore_layout
    config.setdefault('restore_layout', 'pane')
    if args.socket is not None:
        config['sockets'] = [str(Path(p).expanduser().resolve()) for p in args.socket]
    else:
        config.setdefault('sockets', [])
    if not isinstance(config['interval_minutes'], int) or config['interval_minutes'] < 1:
        raise SystemExit('Invalid interval in config.json')
    config['hooks'] = not args.no_hook
    config['restore'] = not args.no_restore
    if args.stagger_seconds is not None:
        if args.stagger_seconds < 0:
            raise SystemExit('--stagger-seconds must not be negative')
        config['stagger_seconds'] = args.stagger_seconds
    config.setdefault('stagger_seconds', 3)
    os.umask(0o077)
    for directory in (layout.config_dir, layout.units, layout.script.parent,
                      layout.state_dir, layout.hook.parent):
        directory.mkdir(parents=True, exist_ok=True)
    previous = backup([layout.script, layout.service, layout.timer, layout.config_file,
                       layout.hook, layout.restore_script, layout.restore_service],
                      layout.state_dir)
    if previous:
        print(f'Previous installation backed up: {previous}')
    if not args.no_start:
        # Serialize replacement with the old service, while keeping its logs intact.
        systemctl('stop', f'{APP}.timer', check=False, quiet=True)
        systemctl('stop', f'{APP}.service', check=False, quiet=True)
    shutil.copyfile(ROOT / 'tmux_codebuddy_pane_sync.py', layout.script)
    layout.script.chmod(0o700)
    shutil.copyfile(ROOT / 'hooks' / HOOK_BASENAME, layout.hook)
    layout.hook.chmod(0o700)
    shutil.copyfile(ROOT / 'codebuddy_restore.py', layout.restore_script)
    layout.restore_script.chmod(0o700)
    write_json(layout.config_file, config)
    executable = str(Path(sys.executable).resolve())
    tmux_dir = str(Path(shutil.which('tmux')).parent)
    # Explicit PATH makes installations outside /usr/bin work in the user manager.
    search_path = ':'.join(dict.fromkeys([tmux_dir, '/usr/local/bin', '/usr/bin', '/bin']))
    layout.service.write_text(
        '[Unit]\nDescription=Back up tmux/CodeBuddy names and synchronize pane titles\n\n'
        '[Service]\nType=oneshot\n'
        f'Environment={quote("PATH=" + search_path)}\n'
        f'ExecStart={quote(executable)} {quote(layout.script)} --config {quote(layout.config_file)} --apply\n'
        'TimeoutStartSec=10min\nUMask=0077\n')
    interval = config['interval_minutes']
    layout.timer.write_text(
        '[Unit]\nDescription=Periodically back up and synchronize tmux pane names\n\n'
        f'[Timer]\nOnActiveSec={interval}min\nOnUnitActiveSec={interval}min\n'
        f'AccuracySec=1s\nUnit={APP}.service\n\n[Install]\nWantedBy=timers.target\n')
    if not config['restore']:
        layout.restore_service.unlink(missing_ok=True)
    layout.restore_service.write_text(
        '[Unit]\nDescription=Restore tmux panes and resume their CodeBuddy conversations\n'
        'After=network-online.target\nWants=network-online.target\n\n'
        '[Service]\nType=oneshot\n'
        # Without this systemd kills the tmux server this unit starts, because a
        # oneshot service tears down its whole cgroup when ExecStart exits.
        'KillMode=process\n'
        f'Environment={quote("PATH=" + search_path)}\n'
        # Give the network and the user manager a moment before starting a dozen CLIs.
        'ExecStartPre=/bin/sleep 20\n'
        f'ExecStart={quote(executable)} {quote(layout.restore_script)}'
        f' --config {quote(layout.config_file)} --apply --quiet\n'
        'TimeoutStartSec=30min\nUMask=0077\n\n'
        '[Install]\nWantedBy=default.target\n') if config['restore'] else None
    if config['hooks']:
        settings = load_settings(layout.settings)
        command = hook_command(layout.hook, executable)
        if register_hooks(settings, command):
            backup([layout.settings], layout.state_dir)
            mode = layout.settings.stat().st_mode & 0o777 if layout.settings.exists() else 0o600
            write_json(layout.settings, settings, mode)
            print(f'Registered CodeBuddy hooks: {", ".join(HOOK_EVENTS)}')
        else:
            print('CodeBuddy hooks already up to date')
    else:
        print('Skipped CodeBuddy hook registration (--no-hook)')
    if not args.no_start:
        systemctl('daemon-reload')
        systemctl('enable', '--now', f'{APP}.timer')
        if config['restore']:
            # Enabled, not started: a restore only belongs at boot, and running
            # it now could launch conversations the user did not ask for.
            systemctl('enable', f'{APP}-restore.service', check=False)
        else:
            systemctl('disable', f'{APP}-restore.service', check=False, quiet=True)
        if systemctl('start', f'{APP}.service', check=False).returncode:
            print(f'Timer installed, but initial sync failed. Inspect: journalctl --user -u {APP}.service',
                  file=sys.stderr)
        systemctl('list-timers', f'{APP}.timer', '--no-pager')
    print(f'Script: {layout.script}\nHook: {layout.hook}\nConfig: {layout.config_file}\n'
          f'Log: {layout.state_dir / "pane-names.jsonl"}\nInterval: {interval} minutes')
    print(f'Manifest: {layout.state_dir / "restore-manifest.json"}')
    if config['restore']:
        print(f'Boot restore: {layout.restore_service} (enabled; test with '
              f'"{layout.restore_script} --dry-run")')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'uninstall'])
    parser.add_argument('--interval-minutes', type=int)
    parser.add_argument('--codebuddy-home', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--socket', action='append')
    parser.add_argument('--name-source', choices=('auto', 'custom', 'ai'))
    parser.add_argument('--workbuddy-command',
                        help='Launcher the restore service should resume conversations with')
    parser.add_argument('--stagger-seconds', type=int,
                        help='Delay between boot-time launches (default 3)')
    parser.add_argument('--restore-layout', choices=('pane', 'window'),
                        help='pane (default): rebuild the recorded splits; '
                             'window: one conversation per tmux window')
    parser.add_argument('--no-hook', action='store_true',
                        help='Do not register CodeBuddy hooks; timer only')
    parser.add_argument('--no-restore', action='store_true',
                        help='Do not install the boot-time restore service')
    parser.add_argument('--no-start', action='store_true', help='Only manage files, do not call systemd')
    args = parser.parse_args()
    if sys.platform != 'linux' or sys.version_info < (3, 9):
        parser.error('Linux and Python 3.9+ are required')
    if args.interval_minutes is not None and args.interval_minutes < 1:
        parser.error('--interval-minutes must be positive')
    config = {}
    config_root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')).resolve()
    config_file = config_root / APP / 'config.json'
    if config_file.exists():
        try:
            loaded = json.loads(config_file.read_text(encoding='utf-8'))
            config = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            pass
    layout = Layout(config, args.codebuddy_home)
    if args.action == 'uninstall':
        uninstall(layout, args.no_start)
    else:
        install(args, layout)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'{APP}: {error}', file=sys.stderr)
        sys.exit(1)
