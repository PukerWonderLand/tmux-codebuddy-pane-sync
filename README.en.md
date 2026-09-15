# tmux-codebuddy-pane-sync

[中文](README.md) · [How it works](docs/design.md) · [MIT License](LICENSE)

**Sync CodeBuddy conversation names to tmux pane titles: back up first, compare, then
update — every 30 minutes, plus once at the end of every turn.**

Built for people running several CodeBuddy CLI sessions at once, so panes read
`公司法了解` or `全球同步-Windterm` instead of an interchangeable label.

```
before                                       after
%67  ✳ Load machine and storage info        %67  全球同步-Windterm
%77  ✳ List China Company Law chapters      %77  公司法了解
%71  codex                                  %71  codex (not CodeBuddy, untouched)
```

Names come from what CodeBuddy saves:

1. the name you set with `/rename` (a `custom-title` event) — **preferred**;
2. otherwise the title CodeBuddy generated for you (an `ai-title` event).

Sibling project: [tmux-codex-pane-sync](https://github.com/PukerWonderLand/tmux-codex-pane-sync)
(same architecture, for Codex).

## Install

On the **Linux server that runs tmux and the CodeBuddy CLI**, as the user who owns
them:

```
git clone https://github.com/PukerWonderLand/tmux-codebuddy-pane-sync.git
cd tmux-codebuddy-pane-sync
./install.sh
```

The installer backs up any previous installation, installs a per-user systemd
service and timer, **appends** two hooks to `settings.json`, syncs once
immediately, and then runs every 30 minutes.

**No sudo, no pip, no API key, and no model requests.** Standard library only.

Requires Linux, Python **3.9+**, tmux, and a working systemd user manager. Running
the script directly needs no systemd.

```
# Preview what would change; rename nothing
python3 tmux_codebuddy_pane_sync.py --dry-run --verbose

# Run a single sync
python3 tmux_codebuddy_pane_sync.py --apply
```

## Two triggers

| Trigger | When | Why |
|---|---|---|
| CodeBuddy hook | every `UserPromptSubmit` / `Stop` | a `/rename` or a fresh title lands **as the turn ends**; only the current session is touched |
| systemd timer | every 30 minutes by default | backstop, and covers CodeBuddy writing its own title back |

`install.sh` appends the hooks to `~/.codebuddy/settings.json` and **never edits or
removes** hooks you already have (such as a usage/archive hook). Uninstall removes
exactly what it added.

> **CodeBuddy snapshots hooks at startup.** After `settings.json` changes, sessions
> that are already running keep the old snapshot: the hooks apply in a new session,
> or after reviewing them in the `/hooks` menu. The timer covers the gap for running
> sessions. The hook writes nothing to stdout — `UserPromptSubmit` stdout is added to
> the conversation as context.

## Sync rules

1. Discover the current user's tmux sockets and walk every session/window/pane.
2. Map each pane to the CodeBuddy process in its subtree and read the conversation
   id from `~/.codebuddy/sessions/<pid>.json`.
3. Read that conversation's transcript and take the last `custom-title` (falling
   back to `ai-title`).
4. **Write every pane's original title, conversation name and identity to the
   journal and fsync it.**
5. Compare; record `unchanged`, or update only when they differ.
6. Re-check process identity, conversation and original title immediately before
   writing; skip and retry next run if anything moved.
7. Read the title back and record success, overwrite or failure.

Comparison strips the status glyph CodeBuddy prints in front of its title
(`✳`, `⠴`, `⠇`, …). Otherwise "`✳ name`" and "`name`" would look different and the
tool would rewrite the pane every cycle, fighting CodeBuddy's own renderer.

Panes that cannot be matched uniquely, and names containing terminal control
characters, are recorded but never renamed. Two CodeBuddy sessions under one pane
are skipped conservatively (`ambiguous_sessions`). The first user prompt is **never**
treated as a name. A pane shared by several tmux sessions is processed once.

## Files

Defaults follow `CODEBUDDY_HOME`, `XDG_CONFIG_HOME` and `XDG_STATE_HOME`, and can be
overridden at install time.

| Item | Default path |
|---|---|
| Installed script | `~/.local/bin/tmux-codebuddy-pane-sync.py` |
| CodeBuddy hook | `~/.codebuddy/hooks/codebuddy_pane_sync_hook.py` |
| Configuration | `~/.config/tmux-codebuddy-pane-sync/config.json` |
| Backup and result log | `~/.local/state/tmux-codebuddy-pane-sync/pane-names.jsonl` |
| Previous install backups | `~/.local/state/tmux-codebuddy-pane-sync/install-backups/` |
| systemd units | `~/.config/systemd/user/tmux-codebuddy-pane-sync.{service,timer}` |

The log is JSONL, one JSON object per line, with time, run id, socket, session,
pane id, CodeBuddy conversation id, previous title, conversation name and result.
`backup` records precede any rename in that run; `result` records hold the outcome.
The log is created with mode `0600`.

```
# Recent backups and results
tail -n 20 ~/.local/state/tmux-codebuddy-pane-sync/pane-names.jsonl

# Only the runs that actually renamed something
grep '"status": "updated"' ~/.local/state/tmux-codebuddy-pane-sync/pane-names.jsonl

# Next run
systemctl --user list-timers tmux-codebuddy-pane-sync.timer

# Service output
journalctl --user -u tmux-codebuddy-pane-sync.service -n 30 --no-pager

# Run now
systemctl --user start tmux-codebuddy-pane-sync.service
```

The log **stays on this machine**; nothing is uploaded anywhere. It contains your
conversation names, so do not commit it to a public repository. It is never rotated
or deleted automatically.

## Configuration

```
./install.sh --interval-minutes 30
./install.sh --codebuddy-home /path/to/.codebuddy --state-dir /path/to/log-directory
./install.sh --name-source ai        # auto (default) / custom / ai
./install.sh --no-hook               # timer only; settings.json untouched
./install.sh --socket /path/to/tmux.sock --socket /another/tmux.sock
```

Without explicit sockets, the tool discovers `/tmp/tmux-UID/*`, sockets under
`TMUX_TMPDIR`, and the socket `TMUX` points at. systemd does not always inherit an
interactive shell's environment, so pin non-standard locations with `--socket`.
Only sockets owned by the current user are used.

Reinstalling keeps the previous interval, data paths, sockets and `name_source`.
Change the interval by reinstalling, since the timer is regenerated. `--no-start`
writes files without calling systemd. Without systemd, schedule
`python3 tmux_codebuddy_pane_sync.py --apply` yourself.

### SSH logout and reboot

The user timer is enabled and starts with the user's systemd manager. To keep it
running with no login session:

```
loginctl show-user "$USER" -p Linger
```

If that is `no`, consider `loginctl enable-linger "$USER"` (needs admin rights on
some systems). This tool never changes linger. tmux sessions are not restored after
a reboot, and only panes that exist at that moment are handled.

### Stopping and uninstalling

```
systemctl --user disable --now tmux-codebuddy-pane-sync.timer   # pause
systemctl --user enable --now tmux-codebuddy-pane-sync.timer    # resume
./uninstall.sh                                                  # remove code, keep logs
```

Uninstall removes only the hook entries this tool added. It does not restore titles
it changed; to do that manually, read `pane_title` from a `backup` record and set it
with tmux `select-pane -T`, after confirming the pane id still refers to the same
pane.

### Updating

```
git pull --ff-only
./install.sh
```

Old scripts, units, configuration and hook are backed up automatically; the journal
is left alone, and reinstalling is idempotent.

## Compatibility and limits

- Needs Linux `/proc`; not a native Windows/macOS installer. Usable inside WSL's
  Linux environment, subject to tmux, process visibility and systemd.
- Associates panes via `~/.codebuddy/sessions/<pid>.json`. CodeBuddy does **not**
  hold its transcript open, so this project does not scan `/proc/PID/fd` — the
  approach used by `tmux-codex-pane-sync` finds nothing on CodeBuddy.
- CodeBuddy's local layout and transcript events are internal, and may change.
  Unsupported shapes are reported (`no_saved_chat_name`, `unreadable_transcript`)
  rather than guessed.
- **CodeBuddy renders its own generated title, not your custom name.** After
  `/rename`, CodeBuddy may overwrite the custom name the next time it refreshes its
  title. The hook re-asserts it at the end of each turn, and `verification_mismatch`
  in the journal makes the overwrite visible; this tool does not promise to win.
- Processes must be visible. Permission restrictions, containers and remote
  CodeBuddy sessions all cause skips.
- It does not install or upgrade tmux, does not touch shell/CodeBuddy title
  settings, never sends keystrokes to other panes, and never renames a CodeBuddy
  conversation (it does not type `/rename`).
- tmux 3.2a has no `allow-set-title`; this project does not rely on it.
- Only tmux servers owned and reachable by the current user are scanned.
- Fully independent of `tmux-codex-pane-sync`: different app name, config
  directory, journal and systemd units, so both can be installed at once.

## Development

Standard library only, no third-party runtime dependency:

```
python3 -m unittest discover -s tests -v
```

Tests use temporary directories and an **isolated tmux socket**, and never touch
existing sessions. They cover custom-title precedence, `aiTitle` fallback, status
glyph stripping (anti-ping-pong), `#` escaping, control characters, backup failure
blocking renames, session uniqueness, identity changes, shared panes, hook
registration idempotency and uninstall preservation, and `--only-session` writing
nothing for unrelated panes. GitHub Actions runs Python 3.9 and 3.12.

Issues and PRs welcome. When reporting a reproduction, use invented conversation
names instead of attaching personal logs or CodeBuddy data.
