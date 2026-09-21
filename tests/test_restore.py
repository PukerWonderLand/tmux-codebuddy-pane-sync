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
        records = [dict(session='work1', window_id='@7', pane_id='%39', window_index=7,
                        pane_index=39, window_order=0, pane_order=3, pane_cwd='/home/codex',
                        codebuddy_session_id='sid-1', conversation_id_source='endpoint',
                        pid_file_session_id='stale', codebuddy_chat_name='Name',
                        socket='/tmp/x.sock')]
        self.assertEqual(sync.manifest_entries(records), [dict(
            session='work1', window_order=0, pane_order=3, window_index=7, pane_index=39,
            cwd='/home/codex', session_id='sid-1', session_id_source='endpoint',
            pid_file_session_id='stale', title='Name')])

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
                            socket=None, apply=False, stagger_seconds=0)
        for key, value in overrides.items():
            setattr(options, key, value)
        return options

    def journal(self):
        self.state.mkdir(parents=True, exist_ok=True)
        journal = restore.Journal(self.state / 'restore-log.jsonl', 'test-run')
        self.addCleanup(journal.close)
        return journal


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

    def test_apply_creates_the_pane_and_resumes_the_conversation(self):
        self.write_manifest([self.entry()])
        result = self.run_restore('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tmux('list-panes', '-t', 'restored', '-F', '#{pane_current_path}'),
                         str(self.workdir))
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


if __name__ == '__main__':
    unittest.main()
