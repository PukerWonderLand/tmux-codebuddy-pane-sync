#!/usr/bin/env python3
"""Rebuild tmux panes and resume each pane's recorded CodeBuddy conversation.

Reads the manifest written by tmux_codebuddy_pane_sync.py, recreates the missing
sessions and panes, then runs ``<launcher> -r <session-id>`` in each one so a
reboot brings back the *same* conversations rather than a set of fresh ones.

It still does not import the sync script: this runs from a service where a large
sibling's import-time failure would be fatal, and they share no logic beyond this.
It does import ``platform_compat``, which is small, dependency-free, and holds
three helpers whose two hand-maintained copies were themselves a bug — the
``boot_id()`` this program *writes* into the restore marker is compared by the
sync program to decide whether a reboot happened, so both copies had to agree
byte for byte. ``install`` copies the module alongside these scripts and
``--dry-run`` exercises the import, so a missing module fails loudly at install
time rather than silently at boot.
"""
import argparse
from collections import Counter
from datetime import datetime
import fcntl
import glob
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import platform_compat as compat

VERSION = '0.2.0'
APP = 'tmux-codebuddy-pane-sync'
MANIFEST_NAME = 'restore-manifest.json'
RESTORE_MARKER = 'last-restore.json'
DEFAULT_STAGGER_SECONDS = 3
DEFAULT_VERIFY_SECONDS = 5
#: How long a login-time restore waits before touching tmux. The equivalent on
#: Linux was ``ExecStartPre=/bin/sleep 20`` in the unit; keeping it here makes it
#: testable and lets both platforms share one code path.
DEFAULT_DELAY_SECONDS = 0


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


# Shared with the sync program via platform_compat, so the two cannot drift.
# `is_codebuddy` is consulted through codebuddy_pids() where possible: polling
# this per pid would fork once per candidate on macOS.
processes = compat.processes
descendants = compat.descendants
is_codebuddy = compat.is_codebuddy
boot_id = compat.boot_id


def pane_runs_codebuddy(pane_pid, tree, pids=None):
    """True when this pane already has a CodeBuddy process, so we must not touch it."""
    if pids is not None:
        return any(pid in pids for pid in descendants(pane_pid, tree))
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


#: What to run in a restored pane, per platform. Linux ships `workbuddy`; on
#: macOS the CLI is the `codebuddy` command. Both accept `-r <session-id>`.
DEFAULT_LAUNCHERS = {'linux': 'workbuddy', 'darwin': 'codebuddy'}
FALLBACK_LAUNCHER = 'codebuddy'


def default_launcher():
    return DEFAULT_LAUNCHERS.get(compat.PLATFORM, FALLBACK_LAUNCHER)


def find_workbuddy(configured):
    """Resolve the launcher to an absolute path so a bare login shell finds it."""
    if configured and ('/' in configured or os.sep in configured):
        candidate = Path(configured).expanduser()
        return str(candidate) if candidate.is_file() else None
    name = configured or default_launcher()
    found = shutil.which(name)
    if found:
        return found
    # Both names are tried regardless of platform: a box may have the other
    # CLI installed, and the recorded config may predate the platform default.
    names = [name] + [n for n in ('codebuddy', 'workbuddy') if n != name]
    for candidate in (
        *(Path.home() / '.local/bin' / n for n in names),
        *(p for n in names for p in sorted(Path.home().glob(f'.nvm/versions/node/*/bin/{n}'))),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def layout_pane_count(layout):
    """How many panes a tmux layout string describes.

    Leaves look like "100x50,0,0,0"; the root is "1f24,200x50,0,0{...}" with only
    two numbers after the size, so requiring four isolates the leaves. Needed
    because a layout must not be applied to a window with a different pane count.
    """
    return len(re.findall(r'\d+x\d+,\d+,\d+,\d+', layout or ''))


def launch_command(workbuddy, cwd, session_id):
    return f'cd {shlex.quote(cwd)} && {shlex.quote(workbuddy)} -r {shlex.quote(session_id)}'


def session_layout(socket, target):
    """-> ordered [(window_index, [(pane_index, pane_id), ...]), ...] or None.

    ``target`` is a tmux session id (``$N``), never a name -- see
    :func:`resolve_session` for why.

    Order, not index: tmux renumbers window and pane indexes as panes come and
    go, so only the position within the layout survives a reboot.
    """
    try:
        output = tmux_run(socket, 'list-panes', '-s', '-t', target, '-F',
                          '#{window_index} #{pane_index} #{pane_id}')
    except (subprocess.SubprocessError, OSError):
        return None
    grouped = {}
    for line in output.splitlines():
        window, pane, pane_id = line.split()
        grouped.setdefault(int(window), []).append((int(pane), pane_id))
    return [(window, sorted(grouped[window])) for window in sorted(grouped)]


def list_sessions(socket):
    """name -> tmux session id (``$N``) for every session on this server."""
    try:
        output = tmux_run(socket, 'list-sessions', '-F', '#{session_name}\t#{session_id}')
    except (subprocess.SubprocessError, OSError):
        return {}
    result = {}
    for line in output.splitlines():
        name, _, session_id = line.partition('\t')
        if name and session_id:
            result[name] = session_id
    return result


def resolve_session(socket, name, hint_id=None):
    """The id to address this session by, or ``None`` when it does not exist.

    tmux target syntax is ``session:window.pane``, so a session named
    ``deepseek4.1_work1`` parses as session ``deepseek4``, window ``1_work1``,
    and every ``-t`` against it fails with ``can't find window: deepseek4``.
    Names are therefore matched here in Python and only ids are used as targets.

    ``hint_id`` is the id recorded by the sweep that wrote the manifest, and is
    only a fallback: tmux allocates ids per server, so after a reboot it usually
    names a different session entirely. The name is authoritative because that
    is what the layout is keyed on.
    """
    sessions = list_sessions(socket)
    if name in sessions:
        return sessions[name]
    if hint_id and hint_id in sessions.values():
        return hint_id
    return None


def create_session(socket, name, cwd):
    """Create a session and return its id, or ``None`` on failure.

    ``-s`` takes a *name*, not a target, so a name containing ``.`` or ``:`` is
    fine here; only addressing is affected.
    """
    try:
        tmux_run(socket, 'new-session', '-d', '-s', name, '-c', cwd)
    except (subprocess.SubprocessError, OSError):
        return None
    return resolve_session(socket, name)


def hint_session_id(entries):
    """The recorded tmux session id from a group, if any entry carries one."""
    for entry in entries:
        value = entry.get('tmux_session_id')
        if isinstance(value, str) and value:
            return value
    return None


def ensure_position(socket, target, window_order, pane_order, cwd):
    """Make sure the layout owns a pane at that ordinal; -> (pane_id, how).

    ``target`` is a session id (``$N``); the caller has already resolved or
    created the session, so this only adds windows and panes.

    Creates only what is missing, so a missing window does not become a new
    window on every run, and the first entry of a fresh session reuses the pane
    that ``new-session`` already made instead of splitting a second one.
    """
    layout = session_layout(socket, target)
    if not layout:
        # An empty listing is as unusable as a failed one: a real session always
        # has at least one window with one pane. Treating it as usable would make
        # the loops below create windows forever.
        return None, 'session_unaddressable'
    created = False
    while len(layout) <= window_order:
        tmux_run(socket, 'new-window', '-d', '-t', f'{target}:', '-c', cwd)
        layout = session_layout(socket, target)
        created = True
    window_index, panes = layout[window_order]
    while len(panes) <= pane_order:
        # Split the LAST pane of the window. Splitting the window itself makes
        # tmux insert the new pane next to the current one, which renumbers the
        # panes already placed (measured: [%0,%1] became [%0,%2,%1]) and sends a
        # later entry to a pane an earlier entry had already taken.
        tmux_run(socket, 'split-window', '-d', '-t',
                 f'{target}:{window_index}.{panes[-1][0]}', '-c', cwd)
        layout = session_layout(socket, target)
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
        # One process-table read per poll rather than one per candidate pid: on
        # macOS every `is_codebuddy` call would otherwise spawn a `ps`, and this
        # loop runs every 0.2s.
        if pane_runs_codebuddy(pane_pid, processes(0.15), compat.codebuddy_pids(0.15)):
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

    # Group by window: a window's split geometry has to be applied after its panes
    # exist and before any conversation starts in them, so the first pass builds
    # every pane and layout, and the second pass only starts conversations.
    groups = {}
    for entry in entries:
        groups.setdefault((entry.get('session'), entry.get('window_order')), []).append(entry)

    placed = {}
    if options.apply:
        for (session, window_order), members in groups.items():
            ordered = sorted(members, key=lambda e: e.get('pane_order') or 0)
            # Resolve once per session. The name is authoritative; the recorded
            # id is only used when the name is gone, since tmux reuses ids freely
            # across servers.
            target = resolve_session(options.socket, session, hint_session_id(ordered))
            if target is None:
                target = create_session(options.socket, session, ordered[0].get('cwd'))
            if target is None:
                # Neither finding nor creating the session worked. Report it and
                # move on: one unusable session must not abort the whole restore.
                for entry in ordered:
                    counts['session_unaddressable'] += 1
                    log.write('restore', status='session_unaddressable', session=session,
                              window_order=window_order, pane_order=entry.get('pane_order'),
                              title=entry.get('title'))
                continue
            for entry in ordered:
                key = (session, window_order, entry.get('pane_order'))
                try:
                    placed[key] = ensure_position(options.socket, target, window_order,
                                                  entry['pane_order'], entry.get('cwd'))
                except (subprocess.SubprocessError, OSError) as error:
                    log.write('restore', status='error', stage='create_pane',
                              error=error_text(error), session=session,
                              window_order=window_order, pane_order=entry.get('pane_order'))
            layout = next((m.get('window_layout') for m in ordered if m.get('window_layout')), None)
            if set(placed) and layout:
                current = session_layout(options.socket, target) or []
                if window_order < len(current):
                    window_index, panes = current[window_order]
                    if layout_pane_count(layout) == len(panes):
                        # tmux maps the structure onto the window's panes by
                        # position and rewrites the stale pane ids inside it, so a
                        # layout recorded on another boot restores the real
                        # geometry (measured: rebuilt 200x25/200x12/200x11 stacked
                        # panes became the original 100x50 + 49x50 + 49x50 columns).
                        ok = tmux_ok(options.socket, 'select-layout', '-t',
                                     f'{target}:{window_index}', layout)
                        log.write('restore', status='layout_applied' if ok else 'layout_rejected',
                                  session=session, window_order=window_order,
                                  window_index=window_index, panes=len(panes), layout=layout)

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
                status = ('already_running' if pane_runs_codebuddy(pane_pid, processes(0.5),
                                                                  compat.codebuddy_pids(0.5))
                          else 'would_launch')
            counts[status] += 1
            log.write('restore', status=status, pane_id=pane_id,
                      command=launch_command(workbuddy, cwd, session_id), **base)
            continue
        try:
            pane_id, how = placed.get((session, window_order, pane_order), (None, None))
            if not pane_id:
                counts['error'] += 1
                log.write('restore', status='error', stage='pane_missing', **base)
                continue
            base.update(pane_id=pane_id, pane_how=how)
            pane_pid = int(tmux_run(options.socket, 'display-message', '-p', '-t', pane_id,
                                    '#{pane_pid}'))
            if pane_runs_codebuddy(pane_pid, processes(0.5), compat.codebuddy_pids(0.5)):
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
    parser.add_argument('--launcher-command',
                        help='Command that resumes a conversation, e.g. codebuddy')
    parser.add_argument('--workbuddy-command',
                        help='Deprecated alias of --launcher-command')
    parser.add_argument('--stagger-seconds', type=int)
    parser.add_argument('--layout', choices=('pane', 'window'),
                        help='pane (default): reproduce the recorded splits; '
                             'window: give every conversation its own window')
    parser.add_argument('--verify-seconds', type=int,
                        help='Poll this long for each launch to confirm it started (0 = do not)')
    parser.add_argument('--delay-seconds', type=int,
                        help='Wait this long before touching tmux. Used by a login service, '
                             'where the session may still be settling')
    parser.add_argument('--only-tmux-session', action='append', default=None,
                        help='Restrict to these tmux session names; repeat for several')
    parser.add_argument('--quiet', action='store_true')
    options = parser.parse_args()
    config = json.loads(options.config.read_text()) if options.config else {}
    options.state_dir = Path(options.state_dir or config.get('state_dir') or Path(
        os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / APP).expanduser().resolve()
    options.manifest = Path(options.manifest or config.get('manifest')
                            or options.state_dir / MANIFEST_NAME).expanduser().resolve()
    # The flag and config key are named for `workbuddy` because that was the only
    # CLI this followed. `launcher_command` supersedes both; the old spelling is
    # kept as a permanent alias so existing config.json files keep working.
    options.workbuddy_command = (
        options.launcher_command or options.workbuddy_command
        or config.get('launcher_command') or config.get('workbuddy_command')
        or default_launcher())
    options.stagger_seconds = (options.stagger_seconds if options.stagger_seconds is not None
                               else config.get('stagger_seconds', DEFAULT_STAGGER_SECONDS))
    options.verify_seconds = (options.verify_seconds if options.verify_seconds is not None
                              else config.get('verify_seconds', DEFAULT_VERIFY_SECONDS))
    options.layout = options.layout or config.get('restore_layout', 'pane')
    options.socket = options.socket or config.get('restore_socket') or None
    options.only_tmux_session = set(options.only_tmux_session or [])
    options.delay_seconds = (options.delay_seconds if options.delay_seconds is not None
                             else config.get('restore_delay_seconds', DEFAULT_DELAY_SECONDS))
    if options.delay_seconds < 0:
        parser.error('--delay-seconds must not be negative')
    if compat.PLATFORM not in ('linux', 'darwin') or not shutil.which('tmux'):
        parser.error('Linux or macOS with tmux in PATH is required')
    if options.delay_seconds:
        # A login service can start before the session is fully up. The wait
        # lives here rather than in the unit so it is testable and portable.
        time.sleep(options.delay_seconds)
    try:
        return run(options)
    except Exception as error:
        print(f'{APP}: {error_text(error)}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
