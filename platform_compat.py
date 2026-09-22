"""Platform-specific primitives shared by the sync and restore programs.

Both programs used to carry their own copy of three Linux-only helpers. Two
copies of ``boot_id()`` in particular are a latent correctness bug: the restore
program writes the value into ``last-restore.json`` and the sync program
compares it to decide whether a reboot happened, so the two must produce a
*byte-identical* string. Centralising them makes that structural instead of
something two files have to remember to keep in step.

The module deliberately depends on nothing outside the standard library, and
the only state it holds is a short-lived cache for the process table (the
restore program polls it).

Everything Linux-specific keeps its original ``/proc`` implementation, so
existing Linux behaviour is unchanged; macOS gets ``ps`` and ``sysctl``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

# Read at call time, never captured at import, so tests can patch it.
PLATFORM = sys.platform

#: argv[0] basenames that identify the CLI this project follows.
CLI_NAMES = ('codebuddy', 'workbuddy')

PS_TIMEOUT = 15

_BOOTTIME_PATTERN = re.compile(r'sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)')


# --------------------------------------------------------------------------- #
# Subprocess helper
# --------------------------------------------------------------------------- #
def _run(argv):
    """Run a command and return stripped stdout, or ``None`` on any failure."""
    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


# --------------------------------------------------------------------------- #
# Process table
# --------------------------------------------------------------------------- #
def _linux_processes():
    """pid -> (parent pid, start ticks); tolerate process exit during a scan."""
    result = {}
    for path in Path('/proc').glob('[0-9]*'):
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(path.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            pass
    return result


def _darwin_processes():
    """pid -> (parent pid, session-leader start time).

    macOS ``ps`` has no start-ticks column, so ``lstart`` serves as the opaque
    "same process instance" token. It is stable for the life of a process at
    one-second resolution, which is all the token is used for.
    """
    output = _run(['ps', '-axo', 'pid=,ppid=,lstart='])
    if output is None:
        return {}
    result = {}
    for line in output.splitlines():
        # ``split(None, 2)`` — lstart itself contains spaces, and the day field
        # is double-padded, so field-count splitting would corrupt it.
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        result[pid] = (parent, parts[2].strip())
    return result


_PROCESS_CACHE = {'stamp': 0.0, 'value': None}


def processes(max_age=0.0):
    """pid -> (parent pid, instance token), optionally cached for ``max_age``.

    ``max_age=0`` re-reads every time, which is what a caller needs when it is
    about to act on the answer.
    """
    now = time.monotonic()
    if (max_age > 0 and _PROCESS_CACHE['value'] is not None
            and now - _PROCESS_CACHE['stamp'] <= max_age):
        return _PROCESS_CACHE['value']
    value = _linux_processes() if PLATFORM == 'linux' else _darwin_processes()
    _PROCESS_CACHE['stamp'] = now
    _PROCESS_CACHE['value'] = value
    return value


def descendants(pid, tree):
    """The pid plus every process descended from it."""
    result = {pid}
    while True:
        expanded = result | {p for p, (parent, _) in tree.items() if parent in result}
        if expanded == result:
            return result
        result = expanded


# --------------------------------------------------------------------------- #
# Command lines
# --------------------------------------------------------------------------- #
def _linux_command(pid):
    """``' '.join(argv)`` for one pid, or ``''`` if it is gone."""
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return ''
    return ' '.join(os.fsdecode(arg) for arg in raw.split(b'\0')).strip()


def _linux_argv(pid):
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return []


def _darwin_commands():
    """pid -> command line, in one ``ps`` invocation."""
    output = _run(['ps', '-axo', 'pid=,command='])
    if output is None:
        return {}
    result = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        result[pid] = parts[1].strip()
    return result


_COMMAND_CACHE = {'stamp': 0.0, 'value': None}


def process_commands(max_age=0.0):
    """pid -> command line for every visible process."""
    now = time.monotonic()
    if (max_age > 0 and _COMMAND_CACHE['value'] is not None
            and now - _COMMAND_CACHE['stamp'] <= max_age):
        return _COMMAND_CACHE['value']
    if PLATFORM == 'linux':
        value = {pid: _linux_command(pid) for pid in processes()}
    else:
        value = _darwin_commands()
    _COMMAND_CACHE['stamp'] = now
    _COMMAND_CACHE['value'] = value
    return value


def command_matches(command):
    """Whether a macOS ``ps -o command=`` line belongs to our CLI.

    macOS joins argv with spaces, so argument boundaries are lost. Tokenising
    and requiring the basename match to be either the first token or a token
    containing a path separator keeps ``node /…/bin/codebuddy`` and
    ``/bin/sh /tmp/x/bin/codebuddy`` matching, while ``vim codebuddy`` — where
    the name is merely an argument — does not.

    This is only used on platforms without ``/proc``; Linux keeps the exact
    NUL-separated argv semantics.
    """
    for index, token in enumerate((command or '').split()):
        base = token.rsplit('/', 1)[-1]
        if base in CLI_NAMES and (index == 0 or '/' in token):
            return True
    return False


def is_codebuddy(pid):
    """Whether this pid is the CLI.

    On Linux the single pid's argv is read directly, which is what the callers
    want: they ask about a handful of pids, not the whole table.
    """
    if PLATFORM == 'linux':
        return any(Path(os.fsdecode(arg)).name in CLI_NAMES
                   for arg in _linux_argv(pid) if arg)
    return command_matches(process_commands().get(pid, ''))


def codebuddy_pids(max_age=0.0):
    """Every pid running the CLI.

    On macOS this is one ``ps`` for the whole table rather than one fork per
    pid, which matters because the restore program polls.
    """
    if PLATFORM == 'linux':
        return {pid for pid in processes(max_age) if is_codebuddy(pid)}
    return {pid for pid, command in process_commands(max_age).items()
            if command_matches(command)}


# --------------------------------------------------------------------------- #
# Boot identity
# --------------------------------------------------------------------------- #
def normalize_boottime(text):
    """Turn ``sysctl -n kern.boottime`` output into a stable token.

    Input looks like ``{ sec = 1788773004, usec = 167243 } Mon Sep  7 …``.
    Returns ``None`` when it does not parse, so the caller can try a fallback
    rather than silently comparing a value that never changes meaningfully.
    """
    match = _BOOTTIME_PATTERN.search(text or '')
    if not match:
        return None
    return f'macos:{match.group(1)}.{match.group(2)}'


def boot_id():
    """An opaque token that differs across reboots, or ``None``.

    Used only for equality: the restore program records it, the sync program
    compares it to decide whether a full sweep may replace the manifest. A
    ``None`` therefore has a real consequence — the sweep is treated as a
    post-reboot merge — so both fallbacks are tried before giving up.
    """
    if PLATFORM == 'linux':
        try:
            return Path('/proc/sys/kernel/random/boot_id').read_text(
                encoding='utf-8').strip()
        except OSError:
            return None
    session_uuid = _run(['sysctl', '-n', 'kern.bootsessionuuid'])
    if session_uuid and session_uuid.strip():
        return f'macos:{session_uuid.strip()}'
    normalized = normalize_boottime(_run(['sysctl', '-n', 'kern.boottime']))
    if normalized:
        return normalized
    # Last resort: pid 1's start time. Present on every macOS.
    started = _run(['ps', '-o', 'lstart=', '-p', '1'])
    if started and started.strip():
        return f'launchd:{started.strip()}'
    return None


# --------------------------------------------------------------------------- #
# tmux sockets
# --------------------------------------------------------------------------- #
def socket_roots(env=None):
    """Directories that may contain a per-uid tmux socket directory.

    tmux's own search order is ``$TMUX_TMPDIR`` then ``$TMPDIR`` then ``/tmp``.
    Searching all of them matters because a background job and an interactive
    shell can disagree about ``TMPDIR``, and a server started by the other one
    would otherwise be invisible.
    """
    environment = os.environ if env is None else env
    roots = []
    for key in ('TMUX_TMPDIR', 'TMPDIR'):
        value = environment.get(key)
        if value:
            roots.append(Path(value))
    roots.append(Path('/tmp'))
    return roots


def tmux_env_socket(env=None):
    """The socket path named by ``$TMUX``, or ``None``.

    ``$TMUX`` is ``'<socket>,<server pid>,<session id>'``. It is the most
    reliable signal available — it is the only one that works from a hook
    invoked inside the pane — so it is tried first. ``rsplit`` is used because
    the socket path itself may contain a comma.
    """
    value = (os.environ if env is None else env).get('TMUX')
    if not value:
        return None
    return Path(value.rsplit(',', 2)[0])


def socket_candidates(explicit=(), extra=(), env=None):
    """Every path that might be a tmux server socket, deduplicated.

    ``explicit`` means "use exactly these and ignore discovery", which is what
    ``--socket`` asks for. ``extra`` is unioned into discovery so sockets
    recorded in the manifest can be retried.

    Paths are deduplicated by ``resolve()`` so ``/tmp`` and ``/private/tmp``
    (and the ``/var/folders`` pair) collapse to a single entry.
    """
    environment = os.environ if env is None else env
    if explicit:
        raw = [Path(p).expanduser() for p in explicit]
    else:
        raw = []
        named = tmux_env_socket(environment)
        if named is not None:
            raw.append(named)
        for root in socket_roots(environment):
            raw.extend((root / f'tmux-{os.getuid()}').glob('*'))
        raw.extend(Path(p).expanduser() for p in extra)

    seen, unique = set(), []
    for path in raw:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def discover_sockets(explicit=(), extra=(), env=None):
    """Existing tmux sockets owned by this user, as strings, sorted.

    Ownership is checked because the socket directory is a shared namespace:
    another user's server must never be attached to.
    """
    result = []
    for path in socket_candidates(explicit, extra, env):
        try:
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
            result.append(str(path))
    return sorted(result)
