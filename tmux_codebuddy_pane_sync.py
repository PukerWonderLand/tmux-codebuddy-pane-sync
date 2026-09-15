#!/usr/bin/env python3
"""Durably back up tmux/CodeBuddy names, then synchronize unambiguous pane titles."""
import argparse
from collections import Counter
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid

VERSION = '0.1.0'
APP = 'tmux-codebuddy-pane-sync'

# CodeBuddy prints a spinner/status glyph immediately before the title it writes
# to the terminal. Those glyphs land in the pane title, so a literal comparison
# would judge "<glyph> name" to differ from "name" and rewrite the pane on every
# cycle, fighting CodeBuddy's own renderer. The glyph is therefore stripped
# whenever two names are compared; the stripped form is never written anywhere.
_STATUS_PREFIX = re.compile(
    '^[\u2219\u25cb\u25cf\u25d0-\u25d3\u2722-\u273a\u2800-\u28ff\\s]+'
)


def strip_status(title):
    return _STATUS_PREFIX.sub('', title)


def tmux(socket, *args):
    return subprocess.check_output(
        ['tmux', '-S', str(socket), *args], text=True,
        stderr=subprocess.PIPE, timeout=10).removesuffix('\n')


def error_text(error):
    return (getattr(error, 'stderr', None) or str(error)).strip()


def discover_sockets(explicit=()):
    if explicit:
        candidates = {Path(p).expanduser() for p in explicit}
    else:
        roots = {Path('/tmp'), Path(os.environ.get('TMUX_TMPDIR', '/tmp'))}
        candidates = {p for root in roots for p in (root / f'tmux-{os.getuid()}').glob('*')}
        if os.environ.get('TMUX'):
            candidates.add(Path(os.environ['TMUX'].rsplit(',', 2)[0]))
    result = []
    for p in sorted(candidates):
        try:
            info = p.stat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                result.append(str(p))
        except OSError:
            pass
    return result


def processes():
    """pid -> (parent pid, start ticks); tolerate process exit during a scan."""
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


class Sessions:
    """Read-only lookup of CodeBuddy's per-pid session records and saved titles.

    CodeBuddy keeps ``<home>/sessions/<pid>.json`` for every running session, so
    a pane is associated with a conversation by process id alone. Unlike Codex,
    the CLI does not hold its transcript open, so scanning /proc/PID/fd finds
    nothing and must not be used here.
    """

    def __init__(self, home, name_source='auto'):
        self.home = Path(home)
        self.sessions_dir = self.home / 'sessions'
        self.projects_dir = self.home / 'projects'
        self.name_source = name_source

    @staticmethod
    def is_codebuddy(pid):
        try:
            args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        except OSError:
            return False
        return any(Path(os.fsdecode(a)).name in ('codebuddy', 'workbuddy')
                   for a in args if a)

    def lookup(self, pid, tree):
        """-> (record, skip_reason); exactly one running session may match."""
        candidates = []
        for child in sorted(descendants(pid, tree)):
            record = self._record(child)
            if record:
                candidates.append(record)
        if not candidates:
            return None, 'no_codebuddy_session'
        if len(candidates) > 1:
            return None, 'ambiguous_sessions'
        return candidates[0], None

    def _record(self, pid):
        if not self.is_codebuddy(pid):
            return None
        try:
            data = json.loads((self.sessions_dir / f'{pid}.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        session_id = data.get('sessionId')
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        kind = data.get('kind')
        if isinstance(kind, str) and kind and kind != 'interactive':
            return None
        return {'pid': pid, 'session_id': session_id, 'cwd': data.get('cwd'),
                'last_heartbeat': data.get('lastHeartbeat')}

    def transcript(self, session_id, cwd):
        if isinstance(cwd, str) and cwd.strip():
            candidate = self.projects_dir / cwd.strip('/').replace('/', '-') / f'{session_id}.jsonl'
            if candidate.is_file():
                return str(candidate)
        matches = sorted(self.projects_dir.glob(f'*/{session_id}.jsonl'))
        return str(matches[0]) if len(matches) == 1 else None

    def title(self, path):
        """-> (name, source, skip_reason). Never falls back to a user prompt."""
        custom = ai = None
        try:
            with open(path, encoding='utf-8') as source:
                for line in source:
                    # A cheap substring gate keeps multi-megabyte transcripts fast.
                    if '"custom-title"' not in line and '"ai-title"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # A concurrent append may leave an incomplete last line.
                    if not isinstance(row, dict):
                        continue
                    if row.get('type') == 'custom-title':
                        value = row.get('customTitle')
                        if isinstance(value, str):
                            custom = value
                    elif row.get('type') == 'ai-title':
                        value = row.get('aiTitle')
                        if isinstance(value, str):
                            ai = value
        except (OSError, UnicodeDecodeError):
            return None, None, 'unreadable_transcript'
        if self.name_source == 'custom':
            name, used = custom, 'custom'
        elif self.name_source == 'ai':
            name, used = ai, 'ai'
        else:
            name, used = (custom, 'custom') if custom else (ai, 'ai')
        if not name or not name.strip():
            return None, None, 'no_saved_chat_name'
        # Keep the raw name in the backup, but never put controls into a title.
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name):
            return name, used, 'unsafe_chat_name'
        return name, used, None

    def inspect(self, pid, tree):
        """-> (info, skip_reason) for one pane's process subtree."""
        record, reason = self.lookup(pid, tree)
        if reason:
            return None, reason
        path = self.transcript(record['session_id'], record.get('cwd'))
        info = dict(record, transcript=path)
        if not path:
            return info, 'no_transcript'
        name, used, reason = self.title(path)
        info.update(name=name, name_used=used)
        return info, reason


class Journal:
    def __init__(self, path, run_id):
        self.file = path.open('a', encoding='utf-8')
        self.run_id = run_id

    def write(self, event, **data):
        self.file.write(json.dumps(dict(time=datetime.now().astimezone().isoformat(),
                                       run_id=self.run_id, event=event, **data),
                                   ensure_ascii=False) + '\n')
        self.file.flush()
        os.fsync(self.file.fileno())  # A failed backup must prevent all later writes.

    def close(self):
        self.file.close()


def snapshot(socket, sessions, tree, only_sessions=()):
    result = []
    lines = tmux(socket, 'list-panes', '-a', '-F',
                 '#{session_id} #{window_id} #{pane_id} #{pane_pid}').splitlines()
    for line in lines:
        session, window, pane, pid = line.split()
        target = f'{session}:{window}.{pane}'
        data = dict(socket=socket, session_id=session, window_id=window,
                    pane_id=pane, pane_pid=int(pid))
        try:
            data['session'] = tmux(socket, 'display-message', '-p', '-t', target, '#{session_name}')
            data['pane_title'] = tmux(socket, 'display-message', '-p', '-t', target, '#{pane_title}')
            info, reason = sessions.inspect(int(pid), tree)
            # A scoped run records nothing at all for panes it was not asked
            # about, including panes that hold no session.
            if only_sessions and (not info or info['session_id'] not in only_sessions):
                continue
            if info:
                data.update(codebuddy_pid=info['pid'], codebuddy_session_id=info['session_id'],
                            codebuddy_chat_name=info.get('name'), chat_name_source=info.get('name_used'),
                            transcript=info.get('transcript'))
            data['skip_reason'] = reason
            data['process_identity'] = tree.get(int(pid))
        except (subprocess.SubprocessError, OSError) as error:
            data.update(skip_reason='inspection_error', error=error_text(error))
        result.append(data)
    return result


def synchronize(data, sessions, apply):
    if data.get('skip_reason'):
        return data['skip_reason'], None
    name = data['codebuddy_chat_name']
    if strip_status(data['pane_title']) == name:
        return 'unchanged', data['pane_title']
    if not apply:
        return 'would_update', None
    socket, pane, pid = data['socket'], data['pane_id'], data['pane_pid']
    tree = processes()
    if not tree.get(pid) or tree.get(pid) != data['process_identity']:
        return 'identity_changed', None
    info, reason = sessions.inspect(pid, tree)
    if reason or not info or info['pid'] != data.get('codebuddy_pid') \
            or info['session_id'] != data.get('codebuddy_session_id') or info.get('name') != name:
        return 'identity_changed', None
    if tmux(socket, 'display-message', '-p', '-t', pane, '#{pane_pid}') != str(pid):
        return 'identity_changed', None
    before = tmux(socket, 'display-message', '-p', '-t', pane, '#{pane_title}')
    if before != data['pane_title']:
        return 'title_changed_since_backup', before
    # tmux expands -T as a format: escape # so names remain literal.
    tmux(socket, 'select-pane', '-t', pane, '-T', name.replace('#', '##'))
    after = tmux(socket, 'display-message', '-p', '-t', pane, '#{pane_title}')
    return ('updated' if after == name else 'verification_mismatch'), after


def run(options):
    os.umask(0o077)
    options.state_dir.mkdir(parents=True, exist_ok=True)
    with (options.state_dir / 'sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another synchronization is running; skipped.')
            return 0
        log = Journal(options.state_dir / 'pane-names.jsonl', str(uuid.uuid4()))
        counts = Counter()
        try:
            log.write('run_start', version=VERSION, dry_run=not options.apply,
                      only_sessions=sorted(options.only_session))
            sessions = Sessions(options.codebuddy_home, options.name_source)
            sockets = discover_sockets(options.socket)
            tree = processes()
            records = []
            for socket in sockets:
                try:
                    records.extend(snapshot(socket, sessions, tree, options.only_session))
                except (subprocess.SubprocessError, OSError) as error:
                    log.write('socket_error', socket=socket, error=error_text(error))
                    counts['socket_error'] += 1
            # Back up ALL discovered panes before updating ANY title.
            for data in records:
                log.write('backup', **data)
            seen = set()
            for data in records:
                key = data['socket'], data['pane_id']
                try:
                    status, after = ('shared_pane_already_processed', None) if key in seen else synchronize(data, sessions, options.apply)
                except (subprocess.SubprocessError, OSError) as error:
                    status, after = 'update_error', None
                    data = dict(data, error=error_text(error))
                seen.add(key)
                counts[status] += 1
                log.write('result', status=status, pane_title_after=after, **data)
                if options.verbose:
                    print(json.dumps(dict(status=status, **data), ensure_ascii=False))
            log.write('run_end', socket_count=len(sockets), pane_count=len(records), counts=dict(counts))
            if not options.quiet:
                print(json.dumps(dict(panes=len(records), counts=dict(counts)), ensure_ascii=False))
            return int(any(counts[k] for k in ('socket_error', 'inspection_error', 'update_error', 'verification_mismatch')))
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
    mode.add_argument('--apply', action='store_true', help='Synchronize titles after durable backup')
    mode.add_argument('--dry-run', action='store_true', help='Back up and preview only (default)')
    parser.add_argument('--codebuddy-home', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--socket', action='append', help='Explicit socket; repeat for multiple servers')
    parser.add_argument('--only-session', action='append', default=None,
                        help='Restrict to these CodeBuddy session ids; repeat for several')
    parser.add_argument('--name-source', choices=('auto', 'custom', 'ai'))
    parser.add_argument('--quiet', action='store_true', help='Suppress the summary line')
    parser.add_argument('--verbose', action='store_true')
    options = parser.parse_args()
    config = json.loads(options.config.read_text()) if options.config else {}
    home = options.codebuddy_home or config.get('codebuddy_home') or os.environ.get('CODEBUDDY_HOME')
    options.codebuddy_home = Path(home or Path.home() / '.codebuddy').expanduser().resolve()
    options.state_dir = Path(options.state_dir or config.get('state_dir') or Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / APP).expanduser().resolve()
    options.socket = options.socket if options.socket is not None else config.get('sockets', [])
    options.only_session = set(options.only_session or [])
    options.name_source = options.name_source or config.get('name_source', 'auto')
    if sys.platform != 'linux' or not shutil.which('tmux'):
        parser.error('Linux with /proc and tmux in PATH is required')
    try:
        return run(options)
    except Exception as error:
        print(f'{APP}: {error_text(error)}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
