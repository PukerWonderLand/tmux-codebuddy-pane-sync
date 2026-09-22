#!/usr/bin/env python3
"""Install/uninstall the sync script, a per-user timer, and CodeBuddy hooks.

No sudo and no pip required. The timer is the backstop; the hooks make a rename
visible as soon as the turn ends. Hook registration is additive and idempotent:
every other entry in ``settings.json`` is preserved byte for byte.

Linux is driven by systemd --user units; macOS by LaunchAgents. The two differ
in one way that matters and cannot be papered over: systemd can act at boot,
while a LaunchAgent runs at *login*. A LaunchDaemon would run at boot but lives
in the system bootstrap namespace as root and so cannot reach the user's tmux
server, whose socket sits in that user's own TMPDIR. Login-time restore is
therefore the honest maximum on macOS — and it matches tmux, whose server also
only exists while the user is logged in.
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
import xml.sax.saxutils

import platform_compat as compat

APP = 'tmux-codebuddy-pane-sync'
ROOT = Path(__file__).resolve().parent
HOOK_BASENAME = 'codebuddy_pane_sync_hook.py'
HOOK_EVENTS = ('UserPromptSubmit', 'Stop')
HOOK_TIMEOUT_SECONDS = 15

#: launchd labels. The `local.` prefix matches the convention already used for
#: this user's other LaunchAgents and keeps them grouped in `launchctl list`.
LAUNCHD_PREFIX = f'local.{APP}'
SYNC_LABEL = f'{LAUNCHD_PREFIX}.sync'
RESTORE_LABEL = f'{LAUNCHD_PREFIX}.restore'
#: How long the login-time restore waits before touching tmux. Ten seconds is
#: enough once the user session exists, where the Linux unit needed twenty.
RESTORE_DELAY_SECONDS = 10


def quote(value):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise ValueError('Paths may not contain newlines')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def systemctl(*args, check=True, quiet=False):
    return subprocess.run(['systemctl', '--user', *args], check=check,
                          stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None)


def launchctl(*args, check=True, quiet=False):
    return subprocess.run(['launchctl', *args], check=check,
                          stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None)


def gui_domain():
    return f'gui/{os.getuid()}'


def plist_document(label, arguments, environment, logs_dir,
                   interval_seconds=None, abandon_process_group=False):
    """Render a LaunchAgent plist.

    Pure, so the shape can be asserted without launchd. ``StartInterval`` is the
    equivalent of systemd's ``OnActiveSec``/``OnUnitActiveSec``;
    ``AbandonProcessGroup`` is the equivalent of ``KillMode=process`` -- without
    it launchd would not kill the tmux server anyway, but the two are declared
    together so the intent survives a future edit.
    """
    def escape(value):
        return xml.sax.saxutils.escape(str(value))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"',
        '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        '<dict>',
        '  <key>Label</key>',
        f'  <string>{escape(label)}</string>',
        '  <key>ProgramArguments</key>',
        '  <array>',
    ]
    lines.extend(f'    <string>{escape(argument)}</string>' for argument in arguments)
    lines.append('  </array>')
    lines.append('  <key>EnvironmentVariables</key>')
    lines.append('  <dict>')
    for key, value in environment.items():
        lines.append(f'    <key>{escape(key)}</key>')
        lines.append(f'    <string>{escape(value)}</string>')
    lines.append('  </dict>')
    lines.append('  <key>RunAtLoad</key>')
    lines.append('  <true/>')
    if interval_seconds is not None:
        lines.append('  <key>StartInterval</key>')
        lines.append(f'  <integer>{int(interval_seconds)}</integer>')
    if abandon_process_group:
        lines.append('  <key>AbandonProcessGroup</key>')
        lines.append('  <true/>')
    lines.append('  <key>ProcessType</key>')
    lines.append('  <string>Background</string>')
    lines.append('  <key>StandardOutPath</key>')
    lines.append(f'  <string>{escape(logs_dir / (label + ".out.log"))}</string>')
    lines.append('  <key>StandardErrorPath</key>')
    lines.append(f'  <string>{escape(logs_dir / (label + ".err.log"))}</string>')
    lines += ['</dict>', '</plist>', '']
    return '\n'.join(lines)


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
        # Shared by both programs. Installed alongside them, because a bare
        # `python3 <script>` puts the script's own directory on sys.path.
        self.platform_module = home / '.local/bin/platform_compat.py'
        self.service = self.units / f'{APP}.service'
        self.timer = self.units / f'{APP}.timer'
        self.restore_service = self.units / f'{APP}-restore.service'
        self.agents = home / 'Library/LaunchAgents'
        self.sync_agent = self.agents / f'{SYNC_LABEL}.plist'
        self.restore_agent = self.agents / f'{RESTORE_LABEL}.plist'
        self.logs = home / 'Library/Logs' / APP
        self.codebuddy_home = Path(codebuddy_home or config.get('codebuddy_home')
                                  or os.environ.get('CODEBUDDY_HOME')
                                  or home / '.codebuddy').expanduser().resolve()
        self.hook = self.codebuddy_home / 'hooks' / HOOK_BASENAME
        self.settings = self.codebuddy_home / 'settings.json'
        self.state_dir = Path(config.get('state_dir') or Path(
            os.environ.get('XDG_STATE_HOME', home / '.local/state')) / APP).expanduser().resolve()

    def artifacts(self):
        """Every file this installer owns on this platform.

        Both platforms' paths are listed so that moving a machine between them
        (or a stale unit from an older install) still gets cleaned up.
        """
        return [self.script, self.restore_script, self.platform_module,
                self.hook, self.config_file,
                self.service, self.timer, self.restore_service,
                self.sync_agent, self.restore_agent]


def hook_command(hook_path, python):
    return f'{shlex.quote(python)} {shlex.quote(str(hook_path))}'


def verify_installed_scripts(layout, executable):
    """Fail at install time if the shared module did not land beside the scripts.

    ``platform_compat`` is imported at module scope by both programs, so a
    missing copy is not a degraded mode -- it is a hard failure, and at login or
    in a timer nobody is watching. ``--help`` exercises the import and returns
    before any work happens.
    """
    if not layout.platform_module.is_file():
        raise SystemExit(f'{layout.platform_module} was not installed')
    for script in (layout.script, layout.restore_script):
        result = subprocess.run([executable, str(script), '--help'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, check=False)
        if result.returncode != 0:
            raise SystemExit(f'{script} does not run: {result.stderr.strip()}')


def write_systemd_units(layout, config, executable, search_path):
    """Linux: one oneshot service, one timer, one boot-time restore service."""
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
        return
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
        '[Install]\nWantedBy=default.target\n')


def write_launch_agents(layout, config, executable, search_path):
    """macOS: a periodic agent and a login-time restore agent.

    ``PATH`` must be spelled out: launchd's default omits /usr/local/bin, which
    is where tmux lives on this kind of machine.
    """
    environment = {'PATH': search_path}
    layout.sync_agent.write_text(plist_document(
        SYNC_LABEL,
        [executable, str(layout.script), '--config', str(layout.config_file), '--apply'],
        environment, layout.logs,
        interval_seconds=int(config['interval_minutes']) * 60), encoding='utf-8')
    layout.sync_agent.chmod(0o600)
    if not config['restore']:
        layout.restore_agent.unlink(missing_ok=True)
        return
    layout.restore_agent.write_text(plist_document(
        RESTORE_LABEL,
        [executable, str(layout.restore_script), '--config', str(layout.config_file),
         '--apply', '--quiet', '--delay-seconds', str(RESTORE_DELAY_SECONDS)],
        environment, layout.logs, abandon_process_group=True), encoding='utf-8')
    layout.restore_agent.chmod(0o600)


def bootstrap_agent(plist_path, label):
    """Load a LaunchAgent, replacing any previous definition.

    bootstrap refuses to take over an already-loaded job, and the bootout that
    fixes that can transiently fail, so it is retried once -- the same shape the
    sibling dashboard installer needed on this OS.
    """
    domain = gui_domain()
    launchctl('bootout', f'{domain}/{label}', check=False, quiet=True)
    if not launchctl('bootstrap', domain, str(plist_path), check=False, quiet=True).returncode:
        return True
    launchctl('bootout', f'{domain}/{label}', check=False, quiet=True)
    return not launchctl('bootstrap', domain, str(plist_path), check=False, quiet=True).returncode


def stop_periodic(layout):
    """Stop the periodic job before its files are replaced; logs are untouched.

    Narrower than :func:`deactivate` on purpose: an install must not disturb the
    restore job, which on Linux is deliberately enabled-but-not-started.
    """
    if compat.PLATFORM == 'darwin':
        launchctl('bootout', f'{gui_domain()}/{SYNC_LABEL}', check=False, quiet=True)
    else:
        systemctl('stop', f'{APP}.timer', check=False, quiet=True)
        systemctl('stop', f'{APP}.service', check=False, quiet=True)


def deactivate(layout):
    """Stop and disable whatever this platform uses, tolerating absence."""
    if compat.PLATFORM == 'darwin':
        domain = gui_domain()
        for label in (SYNC_LABEL, RESTORE_LABEL):
            launchctl('bootout', f'{domain}/{label}', check=False, quiet=True)
    else:
        systemctl('disable', '--now', f'{APP}.timer', check=False, quiet=True)
        systemctl('stop', f'{APP}.service', check=False, quiet=True)
        systemctl('disable', '--now', f'{APP}-restore.service', check=False, quiet=True)


def uninstall(layout, no_start):
    if not no_start:
        deactivate(layout)
    try:
        settings = load_settings(layout.settings)
    except SystemExit as error:
        print(f'{error}\nLeft settings.json untouched.', file=sys.stderr)
        settings = None
    if settings is not None and unregister_hooks(settings):
        backup([layout.settings], layout.state_dir)
        write_json(layout.settings, settings, layout.settings.stat().st_mode & 0o777)
        print(f'Removed CodeBuddy hooks from {layout.settings}')
    for path in layout.artifacts():
        if path.name == 'config.json':
            continue  # configuration is retained on purpose
        path.unlink(missing_ok=True)
    if not no_start and compat.PLATFORM != 'darwin':
        systemctl('daemon-reload')
    print(f'Uninstalled. Logs and configuration retained: {layout.config_file}')


def install(args, layout):
    if not shutil.which('tmux'):
        raise SystemExit('Install tmux first')
    if not args.no_start:
        # Fail before touching anything if this platform's service manager is
        # not reachable. On macOS that means the GUI domain for this uid.
        if compat.PLATFORM == 'darwin':
            launchctl('print', gui_domain(), check=False, quiet=True)
        else:
            subprocess.run(['systemctl', '--user', 'show-environment'], check=True,
                           stdout=subprocess.DEVNULL)
    config = json.loads(layout.config_file.read_text()) if layout.config_file.exists() else {}
    if not isinstance(config, dict):
        config = {}
    config['codebuddy_home'] = str(layout.codebuddy_home)
    config['state_dir'] = str(layout.state_dir)
    config['interval_minutes'] = args.interval_minutes or config.get('interval_minutes', 30)
    config['name_source'] = args.name_source or config.get('name_source', 'auto')
    # New installs get the neutral key; the old one stays as a permanent alias so
    # an existing config.json is never rewritten just to rename a key.
    launcher = getattr(args, 'launcher_command', None) or args.workbuddy_command
    if launcher:
        config['launcher_command'] = launcher
        config.pop('workbuddy_command', None)
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
    directories = [layout.config_dir, layout.script.parent, layout.state_dir,
                   layout.hook.parent]
    directories.append(layout.agents if compat.PLATFORM == 'darwin' else layout.units)
    if compat.PLATFORM == 'darwin':
        directories.append(layout.logs)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    previous = backup(layout.artifacts(), layout.state_dir)
    if previous:
        print(f'Previous installation backed up: {previous}')
    if not args.no_start:
        # Serialize replacement with the old service, while keeping its logs intact.
        stop_periodic(layout)
    shutil.copyfile(ROOT / 'tmux_codebuddy_pane_sync.py', layout.script)
    layout.script.chmod(0o700)
    shutil.copyfile(ROOT / 'hooks' / HOOK_BASENAME, layout.hook)
    layout.hook.chmod(0o700)
    shutil.copyfile(ROOT / 'codebuddy_restore.py', layout.restore_script)
    layout.restore_script.chmod(0o700)
    shutil.copyfile(ROOT / 'platform_compat.py', layout.platform_module)
    layout.platform_module.chmod(0o600)
    write_json(layout.config_file, config)
    executable = str(Path(sys.executable).resolve())
    # A missing shared module breaks both programs at import, so prove they run
    # before declaring success.
    verify_installed_scripts(layout, executable)
    tmux_dir = str(Path(shutil.which('tmux')).parent)
    # Explicit PATH makes installations outside /usr/bin work in the service
    # manager (systemd --user and launchd both ship a minimal default).
    search_path = ':'.join(dict.fromkeys([tmux_dir, '/usr/local/bin', '/usr/bin', '/bin']))
    if compat.PLATFORM == 'darwin':
        write_launch_agents(layout, config, executable, search_path)
    else:
        write_systemd_units(layout, config, executable, search_path)
    interval = config['interval_minutes']
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
        if compat.PLATFORM == 'darwin':
            if bootstrap_agent(layout.sync_agent, SYNC_LABEL):
                launchctl('enable', f'{gui_domain()}/{SYNC_LABEL}', check=False, quiet=True)
                # Equivalent of `systemctl start`: run one sweep now so a
                # misconfiguration shows up during install.
                launchctl('kickstart', '-k', f'{gui_domain()}/{SYNC_LABEL}',
                          check=False, quiet=True)
            else:
                print(f'Could not load {layout.sync_agent}; load it with:\n'
                      f'  launchctl bootstrap {gui_domain()} "{layout.sync_agent}"',
                      file=sys.stderr)
            if config['restore']:
                # Deliberately NOT bootstrapped: RunAtLoad would fire a restore
                # during install, launching conversations the user did not ask
                # for. launchd loads ~/Library/LaunchAgents at each login, which
                # is exactly "enabled, but not started now".
                launchctl('bootout', f'{gui_domain()}/{RESTORE_LABEL}', check=False, quiet=True)
        else:
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
        if compat.PLATFORM == 'darwin':
            print(f'Login restore: {layout.restore_agent} '
                  f'(loads at your next login; test with "{layout.restore_script} --dry-run")')
        else:
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
    parser.add_argument('--launcher-command',
                        help='Launcher the restore service should resume conversations with '
                             '(default: workbuddy on Linux, codebuddy on macOS)')
    parser.add_argument('--workbuddy-command',
                        help='Deprecated alias of --launcher-command')
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
    if sys.platform not in ('linux', 'darwin') or sys.version_info < (3, 9):
        parser.error('Linux or macOS, and Python 3.9+ are required')
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
