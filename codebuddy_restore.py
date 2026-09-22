#!/usr/bin/env python3
"""Rebuild tmux panes and resume each pane's recorded CodeBuddy conversation.

Reads the manifest written by tmux_codebuddy_pane_sync.py, recreates the missing
sessions and panes, then runs ``workbuddy -r <session-id>`` in each one so a
reboot brings back the *same* conversations rather than a set of fresh ones.

Deliberately standalone: it does not import the sync script. This runs at boot
from a systemd unit, where a missing or renamed sibling module would be a hard
failure, and the few helpers it shares are a dozen lines each.
"""
import argparse
from collections import Counter
from datetime import datetime
import fcntl
import glob
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

VERSION = '0.2.0'
APP = 'tmux-codebuddy-pane-sync'
MANIFEST_NAME = 'restore-manifest.json'
RESTORE_MARKER = 'last-restore.json'
DEFAULT_STAGGER_SECONDS = 3
DEFAULT_VERIFY_SECONDS = 5


def error_text(error):
    return (getattr(error, 'stderr', None) or str(error)).strip()


def tmux_run(socket, *args):
    """Run tmux, optionally against an explicit socket (None = default server)."""
    command = ['tmux'] + (['-S', str(socket)] if socket else []) + [str(a) for a in args]
    return subprocess.check_output(command, text=True, stderr=subprocess.PIPE,
                                   timeout=15).removesuffix('\n')


def tmux_ok(socket, *args):
    try:
        tmux_run(socket, *args)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def processes():
    result = {}
    for path in Path('/proc').glob('[0-9]*'):
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(path.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            pass
    return result


def descendants(pid, tree):
    result = {pid}
    while True:
        expanded = result | {p for p, (parent, _) in tree.items() if parent in result}
        if expanded == result:
            return result
        result = expanded


def is_codebuddy(pid):
    try:
        args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return False
    return any(Path(os.fsdecode(a)).name in ('codebuddy', 'workbuddy') for a in args if a)


def pane_runs_codebuddy(pane_pid, tree):
    """True when this pane already has a CodeBuddy process, so we must not touch it."""
    return any(is_codebuddy(pid) for pid in sorted(descendants(pane_pid, tree)))


def load_manifest(path):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get('panes'), list):
        return None
    return document


def normalize_entry(entry):
    """Accept manifests written before layout ordinals existed.

    Older manifests stored absolute tmux indexes, which tmux renumbers freely.
    Treating them as ordinals is the best available reading of that data, and
    the recorder refreshes them on the next sweep.
    """
    if entry.get('window_order') is None:
        entry['window_order'] = entry.get('window_index')
    if entry.get('pane_order') is None:
        entry['pane_order'] = entry.get('pane_index')
    return entry


def find_workbuddy(configured):
    """Resolve the launcher to an absolute path so a bare login shell finds it."""
    if configured and ('/' in configured or os.sep in configured):
        candidate = Path(configured).expanduser()
        return str(candidate) if candidate.is_file() else None
    name = configured or 'workbuddy'
    found = shutil.which(name)
    if found:
        return found
    for fallback in (Path.home() / '.local/bin' / name,
                     Path.home() / '.local/bin/workbuddy',
                     *sorted(Path.home().glob('.nvm/versions/node/*/bin/codebuddy'))):
        if fallback.is_file():
            return str(fallback)
    return None


def launch_command(workbuddy, cwd, session_id):
    return f'cd {shlex.quote(cwd)} && {shlex.quote(workbuddy)} -r {shlex.quote(session_id)}'


def session_layout(socket, session):
    """-> ordered [(window_index, [(pane_index, pane_id), ...]), ...] or None.

    Order, not index: tmux renumbers window and pane indexes as panes come and
    go, so only the position within the layout survives a reboot.
    """
    try:
        output = tmux_run(socket, 'list-panes', '-s', '-t', session, '-F',
                          '#{window_index} #{pane_index} #{pane_id}')
    except (subprocess.SubprocessError, OSError):
        return None
    grouped = {}
    for line in output.splitlines():
        window, pane, pane_id = line.split()
        grouped.setdefault(int(window), []).append((int(pane), pane_id))
    return [(window, sorted(grouped[window])) for window in sorted(grouped)]


def ensure_position(socket, session, window_order, pane_order, cwd):
    """Make sure the layout owns a pane at that ordinal; -> (pane_id, how).

    Creates only what is missing, so a missing window does not become a new
    window on every run, and the first entry of a fresh session reuses the pane
    that ``new-session`` already made instead of splitting a second one.
    """
    layout = session_layout(socket, session)
    created = layout is None
    if created:
        tmux_run(socket, 'new-session', '-d', '-s', session, '-c', cwd)
        layout = session_layout(socket, session)
    while len(layout) <= window_order:
        tmux_run(socket, 'new-window', '-d', '-t', f'{session}:', '-c', cwd)
        layout = session_layout(socket, session)
        created = True
    window_index, panes = layout[window_order]
    while len(panes) <= pane_order:
        # Split the LAST pane of the window. Splitting the window itself makes
        # tmux insert the new pane next to the current one, which renumbers the
        # panes already placed (measured: [%0,%1] became [%0,%2,%1]) and sends a
        # later entry to a pane an earlier entry had already taken.
        tmux_run(socket, 'split-window', '-d', '-t',
                 f'{session}:{window_index}.{panes[-1][0]}', '-c', cwd)
        layout = session_layout(socket, session)
        window_index, panes = layout[window_order]
        created = True
    return panes[pane_order][1], ('created' if created else 'reused')


class Journal:
    def __init__(self, path, run_id):
        self.file = path.open('a', encoding='utf-8')
        self.run_id = run_id

    def write(self, event, **data):
        self.file.write(json.dumps(dict(time=datetime.now().astimezone().isoformat(),
                                       run_id=self.run_id, event=event, **data),
                                   ensure_ascii=False) + '\n')
        self.file.flush()
        os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


def wait_for_codebuddy(socket, pane_id, seconds):
    """Poll until the pane actually runs CodeBuddy; a launched TUI takes a moment."""
    deadline = time.monotonic() + seconds
    while True:
        pane_pid = int(tmux_run(socket, 'display-message', '-p', '-t', pane_id, '#{pane_pid}'))
        if pane_runs_codebuddy(pane_pid, processes()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def restore(document, options, log):
    """-> Counter of outcomes. Never touches a pane that already runs CodeBuddy."""
    counts = Counter()
    workbuddy = find_workbuddy(options.workbuddy_command)
    if not workbuddy:
        log.write('run_error', error=f'launcher not found: {options.workbuddy_command}')
        counts['launcher_not_found'] += 1
        return counts
    entries = [normalize_entry(dict(e)) for e in document['panes']]
    if options.only_tmux_session:
        entries = [e for e in entries if e.get('session') in options.only_tmux_session]
    if options.layout == 'window':
        # One conversation per tmux window, in manifest order. Clients that show
        # a tmux window as one visible tab then get one tab per conversation,
        # instead of several conversations hidden behind each other's splits.
        seen = {}
        for entry in entries:
            index = seen.get(entry.get('session'), 0)
            seen[entry['session']] = index + 1
            entry['window_order'], entry['pane_order'] = index, 0
    if not tmux_ok(options.socket, 'start-server'):
        log.write('run_error', error='cannot start the tmux server')
        counts['server_unavailable'] += 1
        return counts
    launched_ids = set()
    for entry in entries:
        session = entry.get('session')
        window_order, pane_order = entry.get('window_order'), entry.get('pane_order')
        session_id, cwd = entry.get('session_id'), entry.get('cwd')
        base = dict(session=session, window_order=window_order, pane_order=pane_order,
                    session_id=session_id, cwd=cwd, title=entry.get('title'),
                    recorded_window_index=entry.get('window_index'),
                    recorded_pane_index=entry.get('pane_index'))
        if not session or window_order is None or pane_order is None or not session_id:
            counts['malformed_entry'] += 1
            log.write('restore', status='malformed_entry', **base)
            continue
        if not cwd or not Path(cwd).is_dir():
            counts['missing_cwd'] += 1
            log.write('restore', status='missing_cwd', **base)
            continue
        if session_id in launched_ids:
            counts['duplicate_session_id'] += 1
            log.write('restore', status='duplicate_session_id', **base)
            continue
        if not options.apply:
            # Strictly read-only: report what would happen, create nothing.
            launched_ids.add(session_id)
            layout = session_layout(options.socket, session)
            pane_id = None
            if layout and window_order < len(layout) and pane_order < len(layout[window_order][1]):
                pane_id = layout[window_order][1][pane_order][1]
            if not pane_id:
                status = 'would_create_and_launch'
            else:
                pane_pid = int(tmux_run(options.socket, 'display-message', '-p', '-t',
                                        pane_id, '#{pane_pid}'))
                status = ('already_running' if pane_runs_codebuddy(pane_pid, processes())
                          else 'would_launch')
            counts[status] += 1
            log.write('restore', status=status, pane_id=pane_id,
                      command=launch_command(workbuddy, cwd, session_id), **base)
            continue
        try:
            pane_id, how = ensure_position(options.socket, session, window_order,
                                           pane_order, cwd)
            base.update(pane_id=pane_id, pane_how=how)
            pane_pid = int(tmux_run(options.socket, 'display-message', '-p', '-t', pane_id,
                                    '#{pane_pid}'))
            if pane_runs_codebuddy(pane_pid, processes()):
                counts['already_running'] += 1
                log.write('restore', status='already_running', **base)
                continue
            if entry.get('title'):
                # tmux expands -T as a format: escape # so names remain literal.
                tmux_ok(options.socket, 'select-pane', '-t', pane_id, '-T',
                        str(entry['title']).replace('#', '##'))
            command = launch_command(workbuddy, cwd, session_id)
            tmux_run(options.socket, 'send-keys', '-t', pane_id, '-l', command)
            tmux_run(options.socket, 'send-keys', '-t', pane_id, 'Enter')
            launched_ids.add(session_id)
            verified = (wait_for_codebuddy(options.socket, pane_id, options.verify_seconds)
                        if options.verify_seconds else True)
            status = 'launched' if verified else 'launch_unverified'
            counts[status] += 1
            log.write('restore', status=status, command=command, launcher=workbuddy, **base)
            if options.stagger_seconds:
                time.sleep(options.stagger_seconds)
        except (subprocess.SubprocessError, OSError) as error:
            counts['error'] += 1
            log.write('restore', status='error', error=error_text(error), **base)
    return counts


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8').strip()
    except OSError:
        return None


def write_restore_marker(state_dir, counts):
    """Record that this boot has been restored.

    Until this marker names the current boot, the recorder keeps manifest entries
    it cannot currently see, because those panes are still empty shells.
    """
    payload = {'boot_id': boot_id(),
               'restored_at': datetime.now().astimezone().isoformat(),
               'counts': dict(counts)}
    temporary = state_dir / (RESTORE_MARKER + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    os.replace(temporary, state_dir / RESTORE_MARKER)


def run(options):
    os.umask(0o077)
    options.state_dir.mkdir(parents=True, exist_ok=True)
    with (options.state_dir / 'restore.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another restore is running; skipped.')
            return 0
        log = Journal(options.state_dir / 'restore-log.jsonl', str(uuid.uuid4()))
        try:
            log.write('run_start', version=VERSION, dry_run=not options.apply,
                      manifest=str(options.manifest),
                      only_tmux_session=sorted(options.only_tmux_session))
            document = load_manifest(options.manifest)
            if document is None:
                log.write('run_end', counts={'no_manifest': 1})
                print(f'{APP}: no usable manifest at {options.manifest}; nothing restored')
                return 0
            counts = restore(document, options, log)
            if options.apply:
                write_restore_marker(options.state_dir, counts)
            log.write('run_end', captured_at=document.get('captured_at'), counts=dict(counts))
            if not options.quiet:
                print(json.dumps(dict(manifest_panes=len(document['panes']), counts=dict(counts)),
                                 ensure_ascii=False))
            return int(bool(counts['error'] or counts['launcher_not_found']
                            or counts['server_unavailable']))
        except Exception as error:
            log.write('run_error', error=error_text(error))
            raise
        finally:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--config', type=Path, help='JSON configuration file')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Actually launch conversations')
    mode.add_argument('--dry-run', action='store_true', help='Report the plan only (default)')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--socket', help='Explicit tmux socket; default is the default server')
    parser.add_argument('--workbuddy-command')
    parser.add_argument('--stagger-seconds', type=int)
    parser.add_argument('--layout', choices=('pane', 'window'),
                        help='pane (default): reproduce the recorded splits; '
                             'window: give every conversation its own window')
    parser.add_argument('--verify-seconds', type=int,
                        help='Poll this long for each launch to confirm it started (0 = do not)')
    parser.add_argument('--only-tmux-session', action='append', default=None,
                        help='Restrict to these tmux session names; repeat for several')
    parser.add_argument('--quiet', action='store_true')
    options = parser.parse_args()
    config = json.loads(options.config.read_text()) if options.config else {}
    options.state_dir = Path(options.state_dir or config.get('state_dir') or Path(
        os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / APP).expanduser().resolve()
    options.manifest = Path(options.manifest or config.get('manifest')
                            or options.state_dir / MANIFEST_NAME).expanduser().resolve()
    options.workbuddy_command = (options.workbuddy_command or config.get('workbuddy_command')
                                 or 'workbuddy')
    options.stagger_seconds = (options.stagger_seconds if options.stagger_seconds is not None
                               else config.get('stagger_seconds', DEFAULT_STAGGER_SECONDS))
    options.verify_seconds = (options.verify_seconds if options.verify_seconds is not None
                              else config.get('verify_seconds', DEFAULT_VERIFY_SECONDS))
    options.layout = options.layout or config.get('restore_layout', 'pane')
    options.socket = options.socket or config.get('restore_socket') or None
    options.only_tmux_session = set(options.only_tmux_session or [])
    if sys.platform != 'linux' or not shutil.which('tmux'):
        parser.error('Linux with /proc and tmux in PATH is required')
    try:
        return run(options)
    except Exception as error:
        print(f'{APP}: {error_text(error)}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
