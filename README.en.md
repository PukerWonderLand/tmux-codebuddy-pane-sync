# tmux-codebuddy-pane-sync

[中文](README.md) · [How it works](docs/design.md) · [MIT License](LICENSE)

**Sync CodeBuddy conversation names to tmux pane titles: back up first, compare, then
update — every 30 minutes, plus once at the end of every turn.**

It also records **which conversation each pane has open**, so after a reboot the tmux
sessions, windows and panes are rebuilt and every pane runs
`<launcher> -r <session-id>` to bring back the *same* conversation rather than a new one.
The launcher defaults to `workbuddy` on Linux and `codebuddy` on macOS.

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

On the **machine that runs tmux and the CodeBuddy CLI**, as the user who owns them:

```
git clone https://github.com/PukerWonderLand/tmux-codebuddy-pane-sync.git
cd tmux-codebuddy-pane-sync
./install.sh
```

The installer backs up any previous installation, installs a per-user periodic
service, **appends** two hooks to `settings.json`, syncs once immediately, and then
runs every 30 minutes.

**No sudo, no pip, no API key, and no model requests.** Standard library only.

Requires **Linux or macOS**, Python **3.9+**, and tmux. The periodic service uses a
systemd user manager on Linux and a LaunchAgent on macOS; running the script directly
needs neither.

`./install.sh` uses `python3`, so the service and hook record whichever interpreter
that resolves to — on macOS that may be the system 3.9. **3.9 is tested and works**
(the suite passes on both 3.9 and 3.12), but to match your other services you can name
the interpreter explicitly:

```
/Users/mac/.local/bin/python3.12 manage.py install
```

Re-running after changing it is safe: the hook command is updated in place rather than
duplicated.

### The one real difference between the platforms

| | Linux | macOS |
|:--|:--|:--|
| Periodic backstop | systemd user timer | LaunchAgent (`StartInterval`) |
| When a restore runs | **at boot** (`WantedBy=default.target`) | **at login** (`RunAtLoad`) |
| Where the units live | `~/.config/systemd/user/` | `~/Library/LaunchAgents/` |

**A true boot-time restore is not achievable on macOS.** A LaunchAgent runs at login. A
LaunchDaemon could run at boot, but it lives in the system bootstrap namespace as root
and therefore **cannot reach the user's tmux server** — that socket sits in the user's
own `TMPDIR`. So macOS gets *restore at login*.

That is not a fudge: the tmux server itself only exists for the duration of a login
session, so the two coincide. The restore agent is loaded by launchd at your **next
login** and is deliberately *not* bootstrapped during install — `RunAtLoad` would
otherwise fire a restore the moment you installed, starting conversations you never
asked for.

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
| periodic job | every 30 minutes by default | backstop, and covers CodeBuddy writing its own title back |

`install.sh` appends the hooks to `~/.codebuddy/settings.json` and **never edits or
removes** hooks you already have (such as a usage/archive hook). Uninstall removes
exactly what it added.

> **CodeBuddy snapshots hooks at startup.** After `settings.json` changes, sessions
> that are already running keep the old snapshot: the hooks apply in a new session,
> or after reviewing them in the `/hooks` menu. The timer covers the gap for running
> sessions. The hook writes nothing to stdout — `UserPromptSubmit` stdout is added to
> the conversation as context.

## Restoring conversations after a reboot

Every full sweep writes "which pane holds which conversation" to
`~/.local/state/tmux-codebuddy-pane-sync/restore-manifest.json`:

```json
{"version": 1, "captured_at": "2026-09-21T15:21:20+08:00",
 "panes": [{"session": "deepseek4_1_work5", "tmux_session_id": "$3",
            "window_order": 0, "pane_order": 2,
            "cwd": "/home/codex", "session_id": "01a09eb5-f596-7494-...",
            "session_id_source": "endpoint", "title": "TMU的pane自动更新"}]}
```

`session` is the tmux session **name**, `session_id` is the **CodeBuddy conversation
id** (unrelated), and `tmux_session_id` is tmux's `$N`. On restore the name is matched
in Python and **only `$N` is ever handed to tmux as a target** — tmux's target syntax
is `session:window.pane`, so a session literally named `deepseek4.1_work1` would parse
as session `deepseek4`, window `1_work1`, and every name-based lookup would fail.
`tmux_session_id` is only a hint: tmux allocates ids per server, so after a reboot it
usually names something else, which is why the name stays authoritative.

The restore runs from a service — `tmux-codebuddy-pane-sync-restore.service` on Linux
(`WantedBy=default.target`; your `Linger=yes` means no login is needed), or
`~/Library/LaunchAgents/local.tmux-codebuddy-pane-sync.restore.plist` on macOS (at
login). It starts the tmux server, rebuilds the missing sessions/windows/panes, and for
each pane checks **whether CodeBuddy already runs there** (skip if so, never interrupt)
before sending `tmux send-keys "cd <cwd> && <launcher> -r <id>"`.

```
codebuddy_restore.py --dry-run                                    # plan only, changes nothing
codebuddy_restore.py --apply                                      # what the restore service runs
codebuddy_restore.py --apply --only-tmux-session deepseek4_1_work5 # narrow test
```

Safety properties:

- **Idempotent**: a pane already running CodeBuddy is skipped, so re-running the service or
  having started something by hand never double-launches.
- **One writer per conversation**: a repeated `session_id` in the manifest is restored once.
- **Creates only what is missing**: windows and panes are added only up to the recorded
  layout; a fresh session reuses the pane `new-session` already made.
- **Verified**: `--verify-seconds` (default 5) polls for CodeBuddy in the pane and records
  `launch_unverified` instead of claiming success. Log:
  `~/.local/state/tmux-codebuddy-pane-sync/restore-log.jsonl`.
- A deleted working directory is skipped (`missing_cwd`); `--stagger-seconds` (default 3)
  avoids starting a dozen CLIs at once at boot.

Background sessions (`kind=bg` in `codebuddy ps`) do not live in panes and are not restored;
use the official `codebuddy respawn <idOrName>` for those.

## Sync rules

1. Discover the current user's tmux sockets and walk every session/window/pane.
2. Map each pane to the CodeBuddy process in its subtree and ask **that process's own
   loopback API** — `GET http://127.0.0.1:<port>/api/v1/sessions/live` — which conversation
   it is showing (the port comes from `url` in `~/.codebuddy/sessions/<pid>.json`; the
   `X-CodeBuddy-Request: 1` header is a documented CSRF guard, not a secret).
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
| Restore script | `~/.local/bin/codebuddy_restore.py` |
| CodeBuddy hook | `~/.codebuddy/hooks/codebuddy_pane_sync_hook.py` |
| Configuration | `~/.config/tmux-codebuddy-pane-sync/config.json` |
| Backup and result log | `~/.local/state/tmux-codebuddy-pane-sync/pane-names.jsonl` |
| Restore manifest | `~/.local/state/tmux-codebuddy-pane-sync/restore-manifest.json` |
| Restore log | `~/.local/state/tmux-codebuddy-pane-sync/restore-log.jsonl` |
| Previous install backups | `~/.local/state/tmux-codebuddy-pane-sync/install-backups/` |
| shared module | `~/.local/bin/platform_compat.py` |
| systemd units (Linux) | `~/.config/systemd/user/tmux-codebuddy-pane-sync.{service,timer}` |
| Boot restore unit (Linux) | `~/.config/systemd/user/tmux-codebuddy-pane-sync-restore.service` |
| LaunchAgents (macOS) | `~/Library/LaunchAgents/local.tmux-codebuddy-pane-sync.{sync,restore}.plist` |
| service logs (macOS) | `~/Library/Logs/tmux-codebuddy-pane-sync/` |

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
./install.sh --no-restore                                            # titles only
./install.sh --launcher-command /home/codex/.local/bin/workbuddy --stagger-seconds 3
```

Without explicit sockets, the tool discovers, in order, the socket `TMUX` points at,
sockets under `$TMUX_TMPDIR` and `$TMPDIR`, and `/tmp/tmux-UID/*` — deduplicated by
`resolve()`, so `/tmp` and `/private/tmp` count once. Sockets recorded in the previous
manifest are retried as well, because a tmux server started over SSH can have a
different `TMPDIR` from a background job. A service manager does not always inherit an
interactive shell's environment, so pin non-standard locations with `--socket`. Only
sockets owned by the current user are used.

Reinstalling keeps the previous interval, data paths, sockets and `name_source`.
Change the interval by reinstalling, since the periodic unit is regenerated.
`--no-start` writes files without calling the service manager. Without systemd or
launchd, schedule `python3 tmux_codebuddy_pane_sync.py --apply` yourself.

The flag and config key are named after `workbuddy` because that was the only CLI this
followed. `--launcher-command` / `launcher_command` supersede them; the old spellings
remain as permanent aliases so an existing `config.json` never needs editing.

### SSH logout and reboot

**Linux:** the user timer starts with the user's systemd manager. To keep it running
with no login session:

```
loginctl show-user "$USER" -p Linger
```

If that is `no`, consider `loginctl enable-linger "$USER"` (needs admin rights on
some systems). This tool never changes linger.

**macOS:** a LaunchAgent exists only while you are logged in, which matches the tmux
server's own lifetime. To inspect it:

```
launchctl print "gui/$(id -u)/local.tmux-codebuddy-pane-sync.sync"
tail -n 30 ~/Library/Logs/tmux-codebuddy-pane-sync/local.tmux-codebuddy-pane-sync.sync.err.log
```

This tool does **not** resurrect pre-reboot tmux sessions; it only handles the panes
that exist when it runs — which is what the manifest and the restore service are for.

### Stopping and uninstalling

```
# pause / resume the periodic job (Linux)
systemctl --user disable --now tmux-codebuddy-pane-sync.timer
systemctl --user enable --now tmux-codebuddy-pane-sync.timer

# pause it (macOS)
launchctl bootout "gui/$(id -u)/local.tmux-codebuddy-pane-sync.sync"

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

- **Linux and macOS are both supported.** Linux reads the process table from `/proc`;
  macOS uses `ps -axo pid=,ppid=,lstart=`. The default launcher differs by platform
  (`workbuddy` / `codebuddy`). Windows is not a native target; use WSL's Linux
  environment.
- **macOS can only restore at login, never at boot** — see the table in *Install*.
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
  directory, journal and service units, so both can be installed at once.
- **Session names may contain `.` or `:`** — both are tmux target separators, which
  is why addressing by name used to fail. Names are now matched in Python and only
  tmux's `$N` is ever used as a target. A session that can neither be resolved nor
  created is reported as `session_unaddressable` and skipped; the rest of the restore
  continues.

## Development

Standard library only, no third-party runtime dependency:

```
python3 tests/test_platform_compat.py
python3 tests/test_sync.py
python3 tests/test_restore.py
```

Tests use temporary directories and an **isolated tmux socket**, and never touch
existing sessions. The cases that would write to `~/Library/LaunchAgents` or
`~/.codebuddy` redirect those paths into a temp directory instead of touching the real
ones, and platform branches are driven by patching `platform_compat.PLATFORM`, so all
three OS code paths are exercised on any host.

They cover custom-title precedence, `aiTitle` fallback, status glyph stripping
(anti-ping-pong), `#` escaping, control characters, backup failure blocking renames,
session uniqueness, identity changes, shared panes, hook registration idempotency and
uninstall preservation, `--only-session` writing nothing for unrelated panes,
`ps`/`sysctl` output parsing, command-token matching in both directions,
socket-candidate deduplication, restoring a session whose name contains `.` or `:`,
manifest backward compatibility, plist rendering and `launchctl` arguments, and
"install must never bootstrap the restore agent". GitHub Actions runs Python 3.9 and
3.12.

Issues and PRs welcome. When reporting a reproduction, use invented conversation
names instead of attaching personal logs or CodeBuddy data.
