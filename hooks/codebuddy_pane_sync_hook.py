#!/usr/bin/env python3
"""CodeBuddy hook: re-assert the pane title for the session that raised the event.

Registered on UserPromptSubmit and Stop so a ``/rename`` or a freshly generated
``aiTitle`` reaches the tmux pane as soon as the turn ends, instead of waiting
for the next timer run. Exactly one session is synchronized, so the per-run cost
and the journal growth stay proportional to one pane rather than all of them.

This hook must never block or disturb a conversation: every failure is swallowed
and the process always exits 0 without writing to stdout.
"""
import json
import subprocess
import sys
from pathlib import Path

SYNC = Path.home() / '.local/bin/tmux-codebuddy-pane-sync.py'
EVENTS = {'UserPromptSubmit', 'Stop'}
TIMEOUT_SECONDS = 20


def main():
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(event, dict) or event.get('hook_event_name') not in EVENTS:
        return 0
    session_id = event.get('session_id')
    if not isinstance(session_id, str) or not session_id.strip() or not SYNC.is_file():
        return 0
    try:
        subprocess.run([sys.executable, str(SYNC), '--apply', '--quiet',
                        '--only-session', session_id],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
