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
from urllib.parse import urlsplit
import urllib.request

VERSION = '0.2.0'
APP = 'tmux-codebuddy-pane-sync'
MANIFEST_NAME = 'restore-manifest.json'
RESTORE_MARKER = 'last-restore.json'

# The process's own loopback API answers with the session it is *currently*
# showing, which the pid file does not after a /resume. The request header is a
# CSRF guard documented as a fixed value, not a secret.
ENDPOINT_HEADER = {'X-CodeBuddy-Request': '1'}
ENDPOINT_TIMEOUT = 2
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')

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

    def __init__(self, home, name_source='auto', endpoint_timeout=ENDPOINT_TIMEOUT):
        self.home = Path(home)
        self.sessions_dir = self.home / 'sessions'
        self.projects_dir = self.home / 'projects'
        self.name_source = name_source
        self.endpoint_timeout = endpoint_timeout

    @staticmethod
    def is_codebuddy(pid):
        try:
            args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        except OSError:
            return False
        return any(Path(os.fsdecode(a)).name in ('codebuddy', 'workbuddy')
                   for a in args if a)

    def live_session(self, url):
        """Ask the process itself which conversation it currently shows.

        ``sessions/<pid>.json`` records the session created at startup, so after
        a /resume it names the wrong (usually empty) conversation. The process's
        own loopback API stays correct, and answering it costs one local GET:
        no keystrokes, no tokens, no transcript writes.
        """
        if not isinstance(url, str) or not url:
            return None
        if urlsplit(url).hostname not in LOOPBACK_HOSTS:
            return None
        request = urllib.request.Request(f'{url.rstrip("/")}/api/v1/sessions/live',
                                         headers=dict(ENDPOINT_HEADER))
        try:
            with urllib.request.urlopen(request, timeout=self.endpoint_timeout) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except (OSError, ValueError):
            return None
        data = payload.get('data') if isinstance(payload, dict) else None
        value = data.get('sessionId') if isinstance(data, dict) else None
        return value if isinstance(value, str) and value.strip() else None

    def _record(self, pid):
        if not self.is_codebuddy(pid):
            return None
        try:
            data = json.loads((self.sessions_dir / f'{pid}.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        pid_file_id = data.get('sessionId')
        if not isinstance(pid_file_id, str) or not pid_file_id.strip():
            return None
        kind = data.get('kind')
        if isinstance(kind, str) and kind and kind != 'interactive':
            return None
        url = data.get('url') or data.get('endpoint')
        live_id = self.live_session(url)
        return {'pid': pid, 'session_id': live_id or pid_file_id,
                'pid_file_session_id': pid_file_id, 'live_session_id': live_id,
                'session_id_source': 'endpoint' if live_id else 'pid_file',
                'url': url if isinstance(url, str) else None,
                'cwd': data.get('cwd'), 'last_heartbeat': data.get('lastHeartbeat')}

    def conversation_name(self, record):
        path = self.transcript(record['session_id'], record.get('cwd'))
        if not path:
            return None
        name, _used, reason = self.title(path)
        return name if reason is None else None

    def matches_title(self, record, pane_title):
        """CodeBuddy renders the conversation name itself, so it can arbitrate."""
        if not pane_title:
            return False
        name = self.conversation_name(record)
        return bool(name) and strip_status(pane_title) == name

    def lookup(self, pid, tree, pane_title=None):
        """-> (record, skip_reason); exactly one running session may match."""
        candidates = []
        for child in sorted(descendants(pid, tree)):
            record = self._record(child)
            if record:
                candidates.append(record)
        if not candidates:
            return None, 'no_codebuddy_session'
        if len(candidates) == 1:
            return candidates[0], None
        # Several CodeBuddy processes under one pane: keep the endpoint-backed
        # ones, then let the rendered title arbitrate. Never guess between two
        # answers the process itself gave.
        answered = [c for c in candidates if c['live_session_id']]
        pool = answered or candidates
        matched = [c for c in pool if self.matches_title(c, pane_title)]
        if len(matched) == 1:
            return matched[0], None
        return None, 'ambiguous_sessions'

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

    def inspect(self, pid, tree, pane_title=None):
        """-> (info, skip_reason) for one pane's process subtree."""
        record, reason = self.lookup(pid, tree, pane_title)
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


def pane_ordinals(rows):
    """-> {pane_id: (window_order, pane_order)}.

    ``window_index`` and ``pane_index`` are renumbered by tmux as windows and
    panes come and go (observed drifting from 6/13 to 0/0 within minutes), so
    they cannot identify anything across a reboot. Only the *relative* order of
    windows within a session and panes within a window is a stable description
    of the layout, and that is what a restore has to rebuild.
    """
    by_session = {}
    for row in rows:
        by_session.setdefault(row['session_id'], []).append(row)
    result = {}
    for members in by_session.values():
        windows = sorted({row['window_index'] for row in members})
        window_order = {index: order for order, index in enumerate(windows)}
        grouped = {}
        for row in members:
            grouped.setdefault(row['window_index'], []).append(row)
        for index, panes in grouped.items():
            ordered = sorted(panes, key=lambda row: row['pane_index'])
            for order, row in enumerate(ordered):
                result[row['pane_id']] = (window_order[index], order)
    return result


def list_panes(socket):
    """-> one dict per pane, with its layout position, from a single tmux call."""
    lines = tmux(socket, 'list-panes', '-a', '-F',
                 '#{session_id} #{window_id} #{pane_id} #{pane_pid} '
                 '#{window_index} #{pane_index}').splitlines()
    rows = []
    for line in lines:
        session_id, window_id, pane_id, pid, window, pane = line.split()
        rows.append(dict(session_id=session_id, window_id=window_id, pane_id=pane_id,
                         pane_pid=int(pid), window_index=int(window), pane_index=int(pane)))
    return rows


def snapshot(socket, sessions, tree, only_sessions=()):
    result = []
    rows = list_panes(socket)
    ordinals = pane_ordinals(rows)
    for row in rows:
        session, window, pane, pid = (row['session_id'], row['window_id'],
                                      row['pane_id'], row['pane_pid'])
        target = f'{session}:{window}.{pane}'
        data = dict(socket=socket, session_id=session, window_id=window,
                    pane_id=pane, pane_pid=pid,
                    window_index=row['window_index'], pane_index=row['pane_index'])
        data['window_order'], data['pane_order'] = ordinals.get(pane, (None, None))
        try:
            data['session'] = tmux(socket, 'display-message', '-p', '-t', target, '#{session_name}')
            data['pane_title'] = tmux(socket, 'display-message', '-p', '-t', target, '#{pane_title}')
            data['pane_cwd'] = tmux(socket, 'display-message', '-p', '-t', target, '#{pane_current_path}')
            info, reason = sessions.inspect(pid, tree, data.get('pane_title'))
            # A scoped run records nothing at all for panes it was not asked
            # about, including panes that hold no session.
            if only_sessions and (not info or info['session_id'] not in only_sessions):
                continue
            if info:
                data.update(codebuddy_pid=info['pid'], codebuddy_session_id=info['session_id'],
                            codebuddy_chat_name=info.get('name'), chat_name_source=info.get('name_used'),
                            conversation_id_source=info.get('session_id_source'),
                            pid_file_session_id=info.get('pid_file_session_id'),
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
    info, reason = sessions.inspect(pid, tree, data.get('pane_title'))
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


def manifest_entries(records):
    """-> restorable pane records, in a stable order."""
    entries = []
    for data in records:
        session_id = data.get('codebuddy_session_id')
        if not session_id:
            continue
        entries.append({
            'session': data.get('session'),
            'window_order': data.get('window_order'),
            'pane_order': data.get('pane_order'),
            'window_index': data.get('window_index'),
            'pane_index': data.get('pane_index'),
            'cwd': data.get('pane_cwd'),
            'session_id': session_id,
            'session_id_source': data.get('conversation_id_source'),
            'pid_file_session_id': data.get('pid_file_session_id'),
            'title': data.get('codebuddy_chat_name'),
        })
    entries.sort(key=lambda e: (e['session'] or '', e['window_order'] or 0, e['pane_order'] or 0))
    return entries


def pane_key(entry):
    return entry.get('session'), entry.get('window_order'), entry.get('pane_order')


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text(encoding='utf-8').strip()
    except OSError:
        return None


def restore_pending(state_dir):
    """True until a restore has run for the current boot.

    Right after a reboot every pane is still an empty shell, so an ordinary full
    sweep would erase exactly the pre-reboot mapping that a restore needs. The
    restore service writes a marker when it has run; from then on sweeps replace
    the manifest as usual, which is what makes stale entries disappear.
    """
    current = boot_id()
    if not current:
        return False
    try:
        marker = json.loads((Path(state_dir) / RESTORE_MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return True
    return not isinstance(marker, dict) or marker.get('boot_id') != current


def update_manifest(path, records, scoped, state_dir=None):
    """Refresh the restore manifest.

    A full sweep replaces it -- except while a restore is still pending for this
    boot, when entries for panes that currently look empty are kept. A scoped run
    (one pane, from the hook) always merges, so it can never truncate the
    manifest down to the panes it happened to look at.
    """
    entries = manifest_entries(records)
    merge = scoped or (state_dir is not None and restore_pending(state_dir))
    if merge and path.exists():
        previous = load_manifest(path)
        covered = {pane_key(e) for e in entries}
        entries.extend(e for e in previous['panes'] if pane_key(e) not in covered)
        entries.sort(key=lambda e: (e['session'] or '', e['window_order'] or 0,
                                    e['pane_order'] or 0))
    document = {
        'version': 1,
        'captured_at': datetime.now().astimezone().isoformat(),
        'boot_id': boot_id(),
        'sockets': sorted({r['socket'] for r in records if r.get('socket')}),
        'panes': entries,
    }
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    os.replace(temporary, path)
    return document


def load_manifest(path):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'version': 1, 'panes': []}
    if not isinstance(document, dict) or not isinstance(document.get('panes'), list):
        return {'version': 1, 'panes': []}
    return document


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
            manifest = update_manifest(options.manifest, records,
                                       scoped=bool(options.only_session),
                                       state_dir=options.state_dir)
            log.write('manifest', path=str(options.manifest), scoped=bool(options.only_session),
                      pane_count=len(manifest['panes']), captured_at=manifest['captured_at'])
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
    parser.add_argument('--manifest', type=Path,
                        help='Restore manifest to refresh (default: state dir)')
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
    options.manifest = Path(options.manifest or config.get('manifest')
                            or options.state_dir / MANIFEST_NAME).expanduser().resolve()
    if sys.platform != 'linux' or not shutil.which('tmux'):
        parser.error('Linux with /proc and tmux in PATH is required')
    try:
        return run(options)
    except Exception as error:
        print(f'{APP}: {error_text(error)}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
