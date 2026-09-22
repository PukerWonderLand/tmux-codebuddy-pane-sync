#!/usr/bin/env python3
"""Tests for codebuddy_restore.py.

Integration tests run their own tmux server on a private socket and a fake
``workbuddy`` script, so no existing session is ever touched and no model call
is ever made.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codebuddy_restore as restore  # noqa: E402
import platform_compat as compat  # noqa: E402

TMUX = shutil.which('tmux')
SCRIPT = ROOT / 'codebuddy_restore.py'


def read_journal(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


class ManifestTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.manifest = self.tmp / 'restore-manifest.json'
        self.state = self.tmp / 'state'

    def entry(self, **overrides):
        # cwd must exist or restore() skips the entry as missing_cwd; a Docker
        # runner has no /home/codex, so use the test's own temporary directory.
        entry = dict(session='work1', window_order=0, pane_order=0, cwd=str(self.tmp),
                     session_id='sid-1', session_id_source='endpoint', title='Name')
        entry.update(overrides)
        return entry

    def write(self, entries):
        self.manifest.write_text(json.dumps({'version': 1, 'captured_at': 'now',
                                             'panes': entries}), encoding='utf-8')


class TestManifestBuilding(ManifestTestCase):
    def test_projects_pane_records_into_entries(self):
        import tmux_codebuddy_pane_sync as sync
        records = [dict(session='work1', session_id='$7', window_id='@7', pane_id='%39',
                        window_index=7,
                        pane_index=39, window_order=0, pane_order=3, pane_cwd='/home/codex',
                        window_layout='1f24,200x50,0,0{100x50,0,0,0,49x50,101,0,1}',
                        codebuddy_session_id='sid-1', conversation_id_source='endpoint',
                        pid_file_session_id='stale', codebuddy_chat_name='Name',
                        socket='/tmp/x.sock')]
        self.assertEqual(sync.manifest_entries(records), [dict(
            session='work1', tmux_session_id='$7', window_order=0, pane_order=3,
            window_index=7, pane_index=39,
            cwd='/home/codex', session_id='sid-1', session_id_source='endpoint',
            pid_file_session_id='stale', title='Name',
            window_layout='1f24,200x50,0,0{100x50,0,0,0,49x50,101,0,1}')])

    def test_the_tmux_session_id_is_optional(self):
        """A record with no tmux session id still yields an entry, with a null hint."""
        import tmux_codebuddy_pane_sync as sync
        entries = sync.manifest_entries([dict(session='work1', pane_cwd='/tmp',
                                             window_order=0, pane_order=0,
                                             codebuddy_session_id='sid-1')])
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]['tmux_session_id'])

    def test_panes_without_a_session_are_skipped(self):
        import tmux_codebuddy_pane_sync as sync
        self.assertEqual(sync.manifest_entries([dict(session='work1', window_id='@0',
                                                     pane_id='%0', codebuddy_session_id=None)]), [])

    def test_a_full_sweep_replaces_the_manifest(self):
        import tmux_codebuddy_pane_sync as sync
        self.write([self.entry(session='gone', session_id='old')])
        document = sync.update_manifest(self.manifest, [
            dict(session='work1', window_order=0, pane_order=0, pane_cwd='/home/codex',
                 codebuddy_session_id='sid-1', socket='/tmp/x.sock')], scoped=False)
        self.assertEqual([e['session'] for e in document['panes']], ['work1'])

    def test_a_scoped_run_merges_and_never_truncates(self):
        """The hook writes one pane; it must not erase the other fourteen."""
        import tmux_codebuddy_pane_sync as sync
        self.write([self.entry(session='work1', pane_order=0, session_id='sid-1'),
                    self.entry(session='work2', pane_order=1, session_id='sid-2')])
        document = sync.update_manifest(self.manifest, [
            dict(session='work2', window_order=0, pane_order=1, pane_cwd='/home/codex',
                 codebuddy_session_id='sid-2-new', socket='/tmp/x.sock')], scoped=True)
        self.assertEqual({(e['session'], e['session_id']) for e in document['panes']},
                         {('work1', 'sid-1'), ('work2', 'sid-2-new')})

    def test_a_scoped_run_without_a_manifest_still_records_its_pane(self):
        import tmux_codebuddy_pane_sync as sync
        document = sync.update_manifest(self.manifest, [
            dict(session='work1', window_order=0, pane_order=0, pane_cwd='/home/codex',
                 codebuddy_session_id='sid-1', socket='/tmp/x.sock')], scoped=True)
        self.assertEqual(len(document['panes']), 1)

    def test_a_damaged_manifest_loads_as_empty(self):
        import tmux_codebuddy_pane_sync as sync
        self.manifest.write_text('{not json', encoding='utf-8')
        self.assertEqual(sync.load_manifest(self.manifest)['panes'], [])
        self.assertIsNone(restore.load_manifest(self.manifest))
        self.write([])
        self.assertIsNone(restore.load_manifest(self.tmp / 'missing.json'))


class TestLayoutPaneCount(ManifestTestCase):
    def test_counts_leaves_not_the_root(self):
        self.assertEqual(restore.layout_pane_count(
            '1f24,200x50,0,0{100x50,0,0,0,49x50,101,0,1,49x50,151,0,2}'), 3)
        self.assertEqual(restore.layout_pane_count(
            '49c2,80x24,0,0[80x12,0,0,0,80x5,0,13,13,80x5,0,19,14]'), 3)

    def test_a_single_pane_window(self):
        self.assertEqual(restore.layout_pane_count('b25e,194x59,0,0{194x59,0,0,20}'), 1)

    def test_missing_layouts_count_zero(self):
        self.assertEqual(restore.layout_pane_count(''), 0)
        self.assertEqual(restore.layout_pane_count(None), 0)


class TestLaunchCommand(ManifestTestCase):
    def test_quotes_paths_with_spaces(self):
        command = restore.launch_command('/opt/my tools/workbuddy', '/home/a b', 'sid-1')
        self.assertEqual(command, "cd '/home/a b' && '/opt/my tools/workbuddy' -r sid-1")

    def test_uses_the_resume_flag(self):
        self.assertIn(' -r ', restore.launch_command('workbuddy', '/home/codex', 'sid-1'))


class TestFindWorkbuddy(ManifestTestCase):
    def test_an_absolute_missing_path_is_not_accepted(self):
        self.assertIsNone(restore.find_workbuddy(str(self.tmp / 'nope')))

    def test_an_absolute_path_is_used_as_given(self):
        binary = self.tmp / 'workbuddy'
        binary.write_text('#!/bin/sh\n', encoding='utf-8')
        self.assertEqual(restore.find_workbuddy(str(binary)), str(binary))

    def test_a_bare_name_resolves_through_path(self):
        fake = self.tmp / 'bin' / 'workbuddy'
        fake.parent.mkdir(parents=True)
        fake.write_text('#!/bin/sh\n', encoding='utf-8')
        with mock.patch.object(restore.shutil, 'which', return_value=str(fake)):
            self.assertEqual(restore.find_workbuddy('workbuddy'), str(fake))

    def test_a_bare_name_can_fall_back_to_the_local_bin(self):
        home = self.tmp / 'home'
        (home / '.local/bin').mkdir(parents=True)
        (home / '.local/bin/workbuddy').write_text('#!/bin/sh\n', encoding='utf-8')
        with mock.patch.object(restore.shutil, 'which', return_value=None), \
                mock.patch.object(restore.Path, 'home', return_value=home):
            self.assertEqual(restore.find_workbuddy('workbuddy'), str(home / '.local/bin/workbuddy'))

    def test_the_default_launcher_is_platform_aware(self):
        with mock.patch.object(compat, 'PLATFORM', 'linux'):
            self.assertEqual(restore.default_launcher(), 'workbuddy')
        with mock.patch.object(compat, 'PLATFORM', 'darwin'):
            self.assertEqual(restore.default_launcher(), 'codebuddy')

    def test_an_unrecognised_platform_falls_back_to_codebuddy(self):
        with mock.patch.object(compat, 'PLATFORM', 'freebsd'):
            self.assertEqual(restore.default_launcher(), 'codebuddy')

    def test_the_other_cli_name_is_still_tried(self):
        """A macOS box resolves `codebuddy` even though the default is a name."""
        home = self.tmp / 'mixed-home'
        (home / '.local/bin').mkdir(parents=True)
        (home / '.local/bin/codebuddy').write_text('#!/bin/sh\n', encoding='utf-8')
        with mock.patch.object(restore.shutil, 'which', return_value=None), \
                mock.patch.object(restore.Path, 'home', return_value=home), \
                mock.patch.object(compat, 'PLATFORM', 'linux'):
            # Defaults to workbuddy on Linux, which is absent here.
            self.assertEqual(restore.find_workbuddy(None), str(home / '.local/bin/codebuddy'))

    def test_nothing_installed_resolves_to_none(self):
        home = self.tmp / 'empty-home'
        home.mkdir()
        with mock.patch.object(restore.shutil, 'which', return_value=None), \
                mock.patch.object(restore.Path, 'home', return_value=home):
            self.assertIsNone(restore.find_workbuddy(None))


class TestRestoreDryRun(ManifestTestCase):
    """--dry-run must be strictly read-only."""

    def setUp(self):
        super().setUp()
        self.launcher = self.tmp / 'bin' / 'workbuddy'
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text('#!/bin/sh\nsleep 300\n', encoding='utf-8')
        self.launcher.chmod(0o700)

    def run_restore(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--manifest', str(self.manifest),
             '--state-dir', str(self.state), '--workbuddy-command', str(self.launcher),
             '--socket', str(self.tmp / 'absent.sock'), *extra],
            text=True, capture_output=True, check=False)

    def test_missing_manifest_is_not_an_error(self):
        result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0)
        self.assertIn('no usable manifest', result.stdout)

    MUTATIONS = ('new-session', 'new-window', 'split-window', 'send-keys',
                 'select-pane', 'start-server')

    def dry_run(self, panes_listing):
        """Run restore() in dry-run mode against a stubbed tmux."""
        mutations = []

        def fake_tmux(socket, *args):
            if args[0] in self.MUTATIONS:
                mutations.append(args[0])
                return ''
            if args[0] == 'list-panes':
                return panes_listing
            if args[0] == 'display-message':
                # An impossible pid: never a CodeBuddy process. (Not 1, which is
                # an ancestor of everything and would make the scan walk /proc.)
                return '999999'
            return ''

        with mock.patch.object(restore, 'tmux_run', fake_tmux), \
                mock.patch.object(restore, 'tmux_ok', return_value=True):
            counts = restore.restore({'panes': [self.entry()]}, self.options(), self.journal())
        return counts, mutations

    def test_dry_run_reports_the_plan_without_mutating_tmux(self):
        """A dry run may read tmux, but must never create, split or send anything."""
        counts, mutations = self.dry_run('0 0 %0')
        self.assertEqual(mutations, [])
        self.assertEqual(counts['would_launch'], 1)

    def test_dry_run_reports_a_missing_pane_as_creation(self):
        counts, mutations = self.dry_run('')
        self.assertEqual(mutations, [])
        self.assertEqual(counts['would_create_and_launch'], 1)

    def options(self, **overrides):
        options = mock.Mock(workbuddy_command=str(self.launcher), only_tmux_session=set(),
                            socket=None, apply=False, stagger_seconds=0, layout='pane')
        for key, value in overrides.items():
            setattr(options, key, value)
        return options

    def journal(self):
        self.state.mkdir(parents=True, exist_ok=True)
        journal = restore.Journal(self.state / 'restore-log.jsonl', 'test-run')
        self.addCleanup(journal.close)
        return journal


class TestSessionResolution(unittest.TestCase):
    """Names are matched in Python; only ids are ever used as tmux targets.

    tmux target syntax is ``session:window.pane``, so handing it a session name
    that contains ``.`` or ``:`` silently means something else. These pin the
    invariant directly, without needing a live server.
    """

    def _listing(self, text):
        return mock.patch.object(restore, 'tmux_run', lambda socket, *args: text)

    def test_list_sessions_parses_name_and_id(self):
        with self._listing('a.b\t$1\nplain\t$2'):
            self.assertEqual(restore.list_sessions(None), {'a.b': '$1', 'plain': '$2'})

    def test_list_sessions_ignores_lines_without_both_fields(self):
        with self._listing('\nonlyname\n\t$9\nreal\t$3'):
            self.assertEqual(restore.list_sessions(None), {'real': '$3'})

    def test_a_failed_listing_is_not_an_error(self):
        def boom(socket, *args):
            raise subprocess.SubprocessError('no server')

        with mock.patch.object(restore, 'tmux_run', boom):
            self.assertEqual(restore.list_sessions(None), {})
            self.assertIsNone(restore.resolve_session(None, 'x'))

    def test_the_name_is_authoritative(self):
        with self._listing('work1\t$1\nother\t$2'):
            self.assertEqual(restore.resolve_session(None, 'work1', '$2'), '$1')

    def test_the_recorded_id_is_a_fallback(self):
        """A session renamed since the sweep can still be found by its id."""
        with self._listing('renamed\t$7'):
            self.assertEqual(restore.resolve_session(None, 'gone', '$7'), '$7')

    def test_nothing_resolving_returns_none(self):
        with self._listing('other\t$3'):
            self.assertIsNone(restore.resolve_session(None, 'gone', '$99'))

    def test_create_session_returns_the_new_id(self):
        calls = []

        def fake(socket, *args):
            calls.append(args)
            return 'a.b\t$4' if args[0] == 'list-sessions' else ''

        with mock.patch.object(restore, 'tmux_run', fake):
            self.assertEqual(restore.create_session(None, 'a.b', '/tmp'), '$4')
        # `-s` is a name argument, so a dotted name is legal here.
        self.assertEqual(calls[0], ('new-session', '-d', '-s', 'a.b', '-c', '/tmp'))

    def test_create_session_reports_failure(self):
        def boom(socket, *args):
            raise subprocess.SubprocessError('cannot create')

        with mock.patch.object(restore, 'tmux_run', boom):
            self.assertIsNone(restore.create_session(None, 'x', '/tmp'))

    def test_ensure_position_uses_the_session_id_in_targets(self):
        calls = []

        def fake(socket, *args):
            calls.append(args)
            if args[0] == 'list-panes':
                return '0 0 %0\n0 1 %1' if any(
                    c[0] == 'split-window' for c in calls) else '0 0 %0'
            return ''

        with mock.patch.object(restore, 'tmux_run', fake):
            pane, how = restore.ensure_position(None, '$5', 0, 1, '/tmp')
        self.assertEqual((pane, how), ('%1', 'created'))
        split = next(c for c in calls if c[0] == 'split-window')
        self.assertTrue(split[split.index('-t') + 1].startswith('$5:'),
                        f'the target should be id-based, got {split}')

    def test_a_dotted_name_never_reaches_a_target(self):
        calls = []

        def fake(socket, *args):
            calls.append(args)
            if args[0] == 'list-sessions':
                return 'deepseek4.1_work1\t$1'
            if args[0] == 'list-panes':
                return '0 0 %0\n0 1 %1' if any(
                    c[0] == 'split-window' for c in calls) else '0 0 %0'
            return ''

        with mock.patch.object(restore, 'tmux_run', fake):
            target = restore.resolve_session(None, 'deepseek4.1_work1')
            restore.ensure_position(None, target, 0, 1, '/tmp')
        targets = [args[args.index('-t') + 1] for args in calls if '-t' in args]
        self.assertTrue(targets, 'the split should have produced a target')
        for value in targets:
            self.assertNotIn('deepseek4.1_work1', value)

    def test_ensure_position_reports_an_unusable_session(self):
        """A failed listing must be reported, not raise."""
        with mock.patch.object(restore, 'tmux_run', lambda socket, *args: ''):
            self.assertEqual(restore.ensure_position(None, '$1', 0, 0, '/tmp'),
                             (None, 'session_unaddressable'))

    def test_hint_session_id_reads_the_recorded_id(self):
        self.assertEqual(restore.hint_session_id([{'tmux_session_id': '$3'}]), '$3')
        self.assertEqual(restore.hint_session_id(
            [{'tmux_session_id': None}, {'tmux_session_id': '$4'}]), '$4')
        self.assertIsNone(restore.hint_session_id([{}, {'tmux_session_id': ''}]))


@unittest.skipUnless(TMUX, 'tmux is required')
class TestRestoreEndToEnd(unittest.TestCase):
    """A real tmux server on a private socket, a fake workbuddy, no model calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.socket = self.tmp / 'tmux.sock'
        self.state = self.tmp / 'state'
        self.manifest = self.tmp / 'manifest.json'
        self.calls = self.tmp / 'calls.log'
        self.workdir = self.tmp / 'work dir'  # a space on purpose
        self.workdir.mkdir()
        self.binary = self.tmp / 'bin' / 'workbuddy'
        self.binary.parent.mkdir(parents=True)
        # Records its arguments, then stays alive to be detected as running.
        self.binary.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\nsleep 300\n',
                               encoding='utf-8')
        self.binary.chmod(0o700)
        self.addCleanup(self.kill_server)
        subprocess.run([TMUX, '-S', str(self.socket), 'start-server'], check=True)

    def kill_server(self):
        subprocess.run([TMUX, '-S', str(self.socket), 'kill-server'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def tmux(self, *args):
        return subprocess.check_output([TMUX, '-S', str(self.socket), *args],
                                       text=True, stderr=subprocess.STDOUT).strip()

    def write_manifest(self, entries):
        self.manifest.write_text(json.dumps({'version': 1, 'captured_at': 'now',
                                             'panes': entries}), encoding='utf-8')

    def entry(self, **overrides):
        entry = dict(session='restored', window_order=0, pane_order=0, cwd=str(self.workdir),
                     session_id='sid-1', title='恢复测试', session_id_source='endpoint')
        entry.update(overrides)
        return entry

    def run_restore(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--manifest', str(self.manifest),
             '--state-dir', str(self.state), '--workbuddy-command', str(self.binary),
             '--socket', str(self.socket), '--stagger-seconds', '0',
             '--verify-seconds', '2', *extra],
            text=True, capture_output=True, check=False)

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def wait_for_calls(self, count):
        """The pane's shell needs a moment to actually execute the command."""
        for _ in range(100):
            calls = self.calls_made()
            if len(calls) >= count:
                return calls
            time.sleep(0.05)
        self.fail(f'only {self.calls_made()} launched after waiting')

    def results(self):
        return [e for e in read_journal(self.state / 'restore-log.jsonl') if e['event'] == 'restore']

    def test_dry_run_creates_nothing(self):
        self.write_manifest([self.entry()])
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(subprocess.run([TMUX, '-S', str(self.socket), 'has-session',
                                         '-t', 'restored'],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL).returncode == 0)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.results()[0]['status'], 'would_create_and_launch')

    def test_a_session_name_containing_a_dot_is_still_restored(self):
        """`.` is tmux's window separator, so a name used as a target breaks.

        Before names were resolved to ids, ``new-session -s 'a.b'`` succeeded
        (it takes a name) but every later ``-t 'a.b'`` was parsed as session
        ``a``, window ``b``, so the restore died with ``can't find window: a``
        and the conversation was never resumed. This is the regression guard for
        that: names must never reach a ``-t`` argument.
        """
        self.write_manifest([self.entry(session='deepseek4.1_work1')])
        result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_for_calls(1), ['-r sid-1'])
        self.assertEqual(self.results()[0]['status'], 'launched')
        self.assertIn('deepseek4.1_work1',
                      self.tmux('list-sessions', '-F', '#{session_name}').splitlines())

    def test_a_colon_in_a_session_name_is_still_restored(self):
        """The other half of tmux's target syntax has the same problem."""
        self.write_manifest([self.entry(session='work:1')])
        result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_for_calls(1), ['-r sid-1'])
        self.assertEqual(self.results()[0]['status'], 'launched')

    def test_a_stale_recorded_session_id_falls_back_to_the_name(self):
        """Ids are allocated per server, so the recorded hint cannot be trusted."""
        self.write_manifest([self.entry(session='restored', tmux_session_id='$99')])
        result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_for_calls(1), ['-r sid-1'])
        self.assertEqual(self.results()[0]['status'], 'launched')

    def test_apply_creates_the_pane_and_resumes_the_conversation(self):
        self.write_manifest([self.entry()])
        result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            # Compared after resolving: macOS reports a pane's cwd under
            # /private/var/... while mkdtemp hands out /var/..., and the two are
            # the same directory.
            Path(self.tmux('list-panes', '-t', 'restored', '-F',
                           '#{pane_current_path}')).resolve(),
            Path(self.workdir).resolve())
        self.assertEqual(self.wait_for_calls(1), ['-r sid-1'])
        self.assertEqual(self.results()[0]['status'], 'launched')
        self.assertEqual(self.tmux('display-message', '-p', '-t', 'restored:0.0', '#{pane_title}'),
                         '恢复测试')

    def test_a_second_apply_is_a_no_op(self):
        self.write_manifest([self.entry()])
        self.run_restore('--apply')
        result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_for_calls(1), ['-r sid-1'])  # not launched twice
        statuses = [r['status'] for r in self.results()]
        self.assertEqual(statuses, ['launched', 'already_running'])

    def test_several_panes_are_rebuilt_in_one_session(self):
        self.write_manifest([self.entry(pane_order=0, session_id='sid-1'),
                             self.entry(pane_order=1, session_id='sid-2'),
                             self.entry(window_order=1, pane_order=0, session_id='sid-3')])
        self.run_restore('--apply')
        self.assertEqual(sorted(self.wait_for_calls(3)), ['-r sid-1', '-r sid-2', '-r sid-3'])
        self.assertEqual(len(self.tmux('list-panes', '-t', 'restored', '-F', '#{pane_id}')
                             .splitlines()), 2)
        self.assertEqual(len(self.tmux('list-windows', '-t', 'restored', '-F', '#{window_index}')
                             .splitlines()), 2)

    def test_an_existing_pane_running_a_shell_is_reused(self):
        self.tmux('new-session', '-d', '-s', 'existing', '-c', str(self.workdir))
        self.write_manifest([self.entry(session='existing')])
        self.run_restore('--apply')
        self.assertEqual(self.wait_for_calls(1), ['-r sid-1'])
        self.assertEqual(self.results()[0]['pane_how'], 'reused')

    def test_a_missing_cwd_is_skipped(self):
        self.write_manifest([self.entry(cwd=str(self.tmp / 'gone'))])
        self.run_restore('--apply')
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.results()[0]['status'], 'missing_cwd')

    def test_a_duplicate_session_id_is_launched_once(self):
        self.write_manifest([self.entry(pane_order=0, session_id='same'),
                             self.entry(pane_order=1, session_id='same')])
        self.run_restore('--apply')
        self.assertEqual(self.wait_for_calls(1), ['-r same'])
        self.assertEqual([r['status'] for r in self.results()],
                         ['launched', 'duplicate_session_id'])

    def test_a_malformed_entry_is_reported(self):
        self.write_manifest([self.entry(session_id='')])
        self.run_restore('--apply')
        self.assertEqual(self.results()[0]['status'], 'malformed_entry')

    def test_the_recorded_split_geometry_is_restored(self):
        """The whole point of the layout field: rebuild left/right, not stacked."""
        # Record a real horizontal (side by side) layout from this tmux server.
        self.tmux('new-session', '-d', '-s', 'geosrc', '-x', '200', '-y', '50',
                  '-c', str(self.workdir))
        self.tmux('split-window', '-h', '-t', 'geosrc:0')
        self.tmux('split-window', '-h', '-t', 'geosrc:0')
        layout = self.tmux('display-message', '-p', '-t', 'geosrc:0', '#{window_layout}')
        original = self.tmux('list-panes', '-t', 'geosrc:0', '-F',
                             '#{pane_width}x#{pane_height}@#{pane_left},#{pane_top}').splitlines()
        self.tmux('kill-session', '-t', 'geosrc')

        self.write_manifest([self.entry(pane_order=i, session_id=f'sid-{i}', window_layout=layout)
                             for i in range(3)])
        self.run_restore('--apply')
        # Sorted for the same reason as test_layout_window_is_idempotent.
        self.assertEqual(sorted(self.wait_for_calls(3)),
                         ['-r sid-0', '-r sid-1', '-r sid-2'])

        restored = self.tmux('list-panes', '-t', 'restored', '-F',
                             '#{pane_width}x#{pane_height}@#{pane_left},#{pane_top}').splitlines()
        self.assertEqual(sorted(restored), sorted(original))
        # left/right: same top, different left
        tops = {line.split('@')[1].split(',')[1] for line in restored}
        self.assertEqual(len(tops), 1)
        self.assertEqual([r['status'] for r in self.results()],
                         ['layout_applied', 'launched', 'launched', 'launched'])

    def test_a_layout_for_a_different_pane_count_is_not_applied(self):
        """Applying a 3-pane layout to a 1-pane window would wreck it."""
        three = '1f24,200x50,0,0{100x50,0,0,0,49x50,101,0,1,49x50,151,0,2}'
        self.write_manifest([self.entry(pane_order=0, session_id='sid-0', window_layout=three)])
        self.run_restore('--apply')
        self.assertEqual(self.results()[0]['status'], 'launched')
        self.assertNotIn('layout_applied', [r['status'] for r in self.results()])

    def test_layout_window_gives_each_conversation_its_own_window(self):
        """For clients that show one tmux window per visible tab."""
        self.write_manifest([self.entry(pane_order=0, session_id='sid-1'),
                             self.entry(pane_order=1, session_id='sid-2'),
                             self.entry(pane_order=2, session_id='sid-3')])
        self.run_restore('--apply', '--layout', 'window')
        self.assertEqual(sorted(self.wait_for_calls(3)), ['-r sid-1', '-r sid-2', '-r sid-3'])
        self.assertEqual(len(self.tmux('list-windows', '-t', 'restored',
                                       '-F', '#{window_index}').splitlines()), 3)
        self.assertEqual(len(self.tmux('list-panes', '-s', '-t', 'restored',
                                       '-F', '#{pane_id}').splitlines()), 3)
        self.assertEqual([r['status'] for r in self.results()],
                         ['launched', 'launched', 'launched'])

    def test_layout_window_is_idempotent(self):
        self.write_manifest([self.entry(pane_order=0, session_id='sid-1'),
                             self.entry(pane_order=1, session_id='sid-2')])
        self.run_restore('--apply', '--layout', 'window')
        # Sorted: each pane is launched by its own process and the log records
        # them in completion order, which is not guaranteed.
        self.assertEqual(sorted(self.wait_for_calls(2)), ['-r sid-1', '-r sid-2'])
        self.run_restore('--apply', '--layout', 'window')
        self.assertEqual(sorted(self.calls_made()), ['-r sid-1', '-r sid-2'])  # no relaunch
        self.assertEqual([r['status'] for r in self.results()].count('already_running'), 2)

    def test_three_panes_in_one_window_all_get_their_conversation(self):
        """Splitting used to renumber the window, so a later entry landed on a pane
        an earlier entry had taken and was dropped as already_running."""
        self.write_manifest([self.entry(pane_order=0, session_id='sid-1'),
                             self.entry(pane_order=1, session_id='sid-2'),
                             self.entry(pane_order=2, session_id='sid-3')])
        self.run_restore('--apply')
        self.assertEqual(sorted(self.wait_for_calls(3)), ['-r sid-1', '-r sid-2', '-r sid-3'])
        self.assertEqual(len(self.tmux('list-panes', '-s', '-t', 'restored',
                                       '-F', '#{pane_id}').splitlines()), 3)
        self.assertEqual([r['status'] for r in self.results()],
                         ['launched', 'launched', 'launched'])

    def test_apply_writes_the_restore_marker(self):
        import tmux_codebuddy_pane_sync as sync
        self.write_manifest([self.entry()])
        self.run_restore('--apply')
        marker = json.loads((self.state / 'last-restore.json').read_text())
        self.assertEqual(marker['boot_id'], sync.boot_id())

    def test_dry_run_writes_no_marker(self):
        self.write_manifest([self.entry()])
        self.run_restore()
        self.assertFalse((self.state / 'last-restore.json').exists())

    def test_a_killed_pane_does_not_relaunch_the_survivor(self):
        """Killing a pane renumbers the rest; that must not start a second writer."""
        self.write_manifest([self.entry(pane_order=0, session_id='sid-1'),
                             self.entry(pane_order=1, session_id='sid-2')])
        self.run_restore('--apply')
        self.assertEqual(sorted(self.wait_for_calls(2)), ['-r sid-1', '-r sid-2'])
        second = self.tmux('list-panes', '-t', 'restored', '-F', '#{pane_id}').splitlines()[1]
        self.tmux('kill-pane', '-t', second)
        before = len(self.calls_made())
        self.run_restore('--apply')
        self.assertEqual(sorted(self.calls_made()[before:]), ['-r sid-2'])

    def test_only_tmux_session_restricts_the_work(self):
        self.write_manifest([self.entry(session='wanted', session_id='sid-1'),
                             self.entry(session='other', session_id='sid-2')])
        self.run_restore('--apply', '--only-tmux-session', 'wanted')
        self.assertEqual(self.wait_for_calls(1), ['-r sid-1'])

    def test_an_unverified_launch_is_reported(self):
        """A launcher that never shows up as CodeBuddy must not look like success."""
        impostor = self.tmp / 'bin' / 'somethingelse'
        impostor.write_text('#!/bin/sh\nsleep 300\n', encoding='utf-8')
        impostor.chmod(0o700)
        self.write_manifest([self.entry()])
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--manifest', str(self.manifest),
             '--state-dir', str(self.state), '--workbuddy-command', str(impostor),
             '--socket', str(self.socket), '--stagger-seconds', '0',
             '--verify-seconds', '1', '--apply'],
            text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.results()[0]['status'], 'launch_unverified')

    def test_a_hash_in_the_recorded_title_is_escaped(self):
        self.write_manifest([self.entry(title='fix #{pane_id} bug')])
        self.run_restore('--apply')
        self.assertEqual(self.tmux('display-message', '-p', '-t', 'restored:0.0', '#{pane_title}'),
                         'fix #{pane_id} bug')

    def test_the_lock_prevents_a_concurrent_restore(self):
        import fcntl
        self.write_manifest([self.entry()])
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / 'restore.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0)
        self.assertIn('Another restore is running', result.stdout)
        self.assertEqual(self.calls_made(), [])


class TestBootAwareManifest(ManifestTestCase):
    """A sweep after a reboot must not erase the mapping a restore needs."""

    def record(self, session='work1', window_order=0, pane_order=0, session_id='sid-1'):
        return dict(session=session, window_order=window_order, pane_order=pane_order,
                    pane_cwd=str(self.tmp), codebuddy_session_id=session_id,
                    socket='/tmp/x.sock')

    def test_a_sweep_before_the_restore_keeps_what_it_cannot_see(self):
        import tmux_codebuddy_pane_sync as sync
        self.write([self.entry(session='work1', session_id='sid-1'),
                    self.entry(session='work2', pane_order=1, session_id='sid-2')])
        # Only work1 came back with a session; work2 is still an empty shell.
        document = sync.update_manifest(self.manifest, [self.record()],
                                        scoped=False, state_dir=self.state)
        self.assertEqual({(e['session'], e['session_id']) for e in document['panes']},
                         {('work1', 'sid-1'), ('work2', 'sid-2')})

    def test_after_the_restore_a_sweep_replaces_normally(self):
        import tmux_codebuddy_pane_sync as sync
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'last-restore.json').write_text(
            json.dumps({'boot_id': sync.boot_id()}), encoding='utf-8')
        self.write([self.entry(session='stale', session_id='old')])
        document = sync.update_manifest(self.manifest, [self.record()],
                                        scoped=False, state_dir=self.state)
        self.assertEqual([(e['session'], e['session_id']) for e in document['panes']],
                         [('work1', 'sid-1')])

    def test_a_marker_from_an_earlier_boot_still_counts_as_pending(self):
        import tmux_codebuddy_pane_sync as sync
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'last-restore.json').write_text(
            json.dumps({'boot_id': 'some-previous-boot'}), encoding='utf-8')
        self.assertTrue(sync.restore_pending(self.state))

    def test_a_corrupt_marker_counts_as_pending(self):
        import tmux_codebuddy_pane_sync as sync
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'last-restore.json').write_text('{oops', encoding='utf-8')
        self.assertTrue(sync.restore_pending(self.state))

    def test_the_manifest_records_the_boot(self):
        import tmux_codebuddy_pane_sync as sync
        document = sync.update_manifest(self.manifest, [self.record()],
                                        scoped=False, state_dir=self.state)
        self.assertEqual(document['boot_id'], sync.boot_id())


if __name__ == '__main__':
    unittest.main()
