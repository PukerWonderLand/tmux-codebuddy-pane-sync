"""Tests for tmux-codebuddy-pane-sync.

Integration tests create their own tmux server on a private socket, so existing
sessions are never touched.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ElementTree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import manage  # noqa: E402
import platform_compat as pc  # noqa: E402
import tmux_codebuddy_pane_sync as sync  # noqa: E402

TMUX = shutil.which('tmux')
SCRIPT = ROOT / 'tmux_codebuddy_pane_sync.py'
HOOK = ROOT / 'hooks' / manage.HOOK_BASENAME


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows),
                    encoding='utf-8')


def read_journal(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


class TestStripStatus(unittest.TestCase):
    def test_strips_the_status_glyph_codebuddy_prints(self):
        # Observed live: CodeBuddy writes "<glyph> <title>" into the pane title.
        for glyph in ('✳', '⠴', '⠙', '⠇', '⣾'):
            self.assertEqual(sync.strip_status(f'{glyph} Add pane sync'), 'Add pane sync')

    def test_leaves_plain_titles_alone(self):
        self.assertEqual(sync.strip_status('全球同步-Windterm'), '全球同步-Windterm')
        self.assertEqual(sync.strip_status(''), '')

    def test_strips_leading_whitespace(self):
        self.assertEqual(sync.strip_status('   spaced'), 'spaced')


class SessionsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / 'codebuddy'
        (self.home / 'sessions').mkdir(parents=True)
        (self.home / 'projects').mkdir()

    def transcript(self, session_id, cwd, rows):
        slug = str(cwd).strip('/').replace('/', '-')
        path = self.home / 'projects' / slug / f'{session_id}.jsonl'
        write_jsonl(path, rows)
        return path


class TestTitle(SessionsTestCase):
    def setUp(self):
        super().setUp()
        self.sessions = sync.Sessions(self.home)
        self.path = self.transcript('sid-1', '/home/codex', [
            {'type': 'message', 'role': 'user', 'content': 'not a name'},
            {'type': 'ai-title', 'aiTitle': 'Ai generated'},
            {'type': 'custom-title', 'customTitle': '手动改名'},
        ])

    def test_auto_prefers_the_custom_title(self):
        self.assertEqual(self.sessions.title(str(self.path)), ('手动改名', 'custom', None))

    def test_auto_falls_back_to_the_ai_title(self):
        path = self.transcript('sid-2', '/home/codex', [{'type': 'ai-title', 'aiTitle': 'Ai generated'}])
        self.assertEqual(sync.Sessions(self.home).title(str(path)), ('Ai generated', 'ai', None))

    def test_explicit_sources_do_not_fall_back(self):
        path = self.transcript('sid-3', '/home/codex', [{'type': 'ai-title', 'aiTitle': 'Ai generated'}])
        self.assertEqual(sync.Sessions(self.home, 'custom').title(str(path)),
                         (None, None, 'no_saved_chat_name'))
        self.assertEqual(sync.Sessions(self.home, 'ai').title(str(path)), ('Ai generated', 'ai', None))

    def test_the_last_title_wins(self):
        path = self.transcript('sid-4', '/home/codex', [
            {'type': 'custom-title', 'customTitle': 'first'},
            {'type': 'custom-title', 'customTitle': 'second'},
        ])
        self.assertEqual(sync.Sessions(self.home).title(str(path)), ('second', 'custom', None))

    def test_a_user_prompt_is_never_used_as_a_name(self):
        path = self.transcript('sid-5', '/home/codex', [{'type': 'message', 'role': 'user', 'content': 'hello'}])
        self.assertEqual(sync.Sessions(self.home).title(str(path)), (None, None, 'no_saved_chat_name'))

    def test_control_characters_are_reported_not_applied(self):
        path = self.transcript('sid-6', '/home/codex', [{'type': 'custom-title', 'customTitle': 'bad\x07name'}])
        name, source, reason = sync.Sessions(self.home).title(str(path))
        self.assertEqual((name, source, reason), ('bad\x07name', 'custom', 'unsafe_chat_name'))

    def test_tolerates_an_incomplete_trailing_line(self):
        path = self.transcript('sid-7', '/home/codex', [{'type': 'custom-title', 'customTitle': 'ok'}])
        with path.open('a', encoding='utf-8') as handle:
            handle.write('{"type": "custom-title", "customTi')
        self.assertEqual(sync.Sessions(self.home).title(str(path)), ('ok', 'custom', None))


class TestTranscriptLookup(SessionsTestCase):
    def test_uses_the_cwd_slug(self):
        path = self.transcript('sid-1', '/home/codex', [])
        self.assertEqual(sync.Sessions(self.home).transcript('sid-1', '/home/codex'), str(path))

    def test_falls_back_to_a_glob(self):
        path = self.transcript('sid-2', '/other/place', [])
        self.assertEqual(sync.Sessions(self.home).transcript('sid-2', '/not/recorded'), str(path))

    def test_missing_transcript_is_reported(self):
        self.assertIsNone(sync.Sessions(self.home).transcript('sid-3', '/home/codex'))


class TestLookup(SessionsTestCase):
    TREE = {10: (1, '1'), 11: (10, '2'), 12: (11, '3')}

    def setUp(self):
        super().setUp()
        self.sessions = sync.Sessions(self.home)
        patcher = mock.patch.object(sync.Sessions, 'is_codebuddy', staticmethod(lambda pid: pid == 11))
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, pid, session_id='sid-1', kind='interactive'):
        (self.home / 'sessions' / f'{pid}.json').write_text(json.dumps(
            {'pid': pid, 'sessionId': session_id, 'cwd': '/home/codex', 'kind': kind}),
            encoding='utf-8')

    def test_finds_a_session_through_the_process_subtree(self):
        self.record(11)
        info, reason = self.sessions.lookup(10, self.TREE)
        self.assertIsNone(reason)
        self.assertEqual(info['session_id'], 'sid-1')

    def test_no_session_is_reported(self):
        self.assertIsNone(self.sessions.lookup(10, self.TREE)[0])
        self.assertEqual(self.sessions.lookup(10, self.TREE)[1], 'no_codebuddy_session')

    def test_a_pid_outside_the_subtree_is_ignored(self):
        self.record(99)
        self.assertEqual(self.sessions.lookup(10, self.TREE)[1], 'no_codebuddy_session')

    def test_an_exited_pid_is_ignored(self):
        self.record(11)
        self.assertEqual(self.sessions.lookup(10, {10: (1, '1')})[1], 'no_codebuddy_session')

    def test_non_interactive_sessions_are_ignored(self):
        self.record(11, kind='headless')
        self.assertEqual(self.sessions.lookup(10, self.TREE)[1], 'no_codebuddy_session')


class TestSynchronize(unittest.TestCase):
    """Drive synchronize() with a stubbed tmux client and process table."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / 'codebuddy'
        (self.home / 'sessions').mkdir(parents=True)
        (self.home / 'projects' / 'home-codex').mkdir(parents=True)
        (self.home / 'projects' / 'home-codex' / 'sid-1.jsonl').write_text(
            json.dumps({'type': 'custom-title', 'customTitle': 'From transcript'}) + '\n',
            encoding='utf-8')
        (self.home / 'sessions' / '4243.json').write_text(
            json.dumps({'pid': 4243, 'sessionId': 'sid-1', 'cwd': '/home/codex',
                        'kind': 'interactive'}), encoding='utf-8')
        self.sessions = sync.Sessions(self.home)
        self.tree = {4242: (1, '100'), 4243: (4242, '101')}
        self.titles = {'%1': 'old title'}
        self.calls = []

        def fake_tmux(socket, *args):
            self.calls.append(args)
            if args[0] == 'select-pane':
                self.titles['%1'] = args[-1].replace('##', '#')
                return ''
            field = args[-1]
            if field == '#{pane_pid}':
                return '4242'
            if field == '#{pane_title}':
                return self.titles['%1']
            raise AssertionError(f'unexpected tmux call {args}')

        for name, value in (('tmux', fake_tmux), ('processes', lambda: dict(self.tree))):
            patcher = mock.patch.object(sync, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sync.Sessions, 'is_codebuddy', staticmethod(lambda pid: pid == 4243))
        patcher.start()
        self.addCleanup(patcher.stop)

    def data(self, **overrides):
        data = dict(socket='/tmp/x.sock', session_id='$1', window_id='@1', pane_id='%1',
                    pane_pid=4242, session='t', pane_title='old title', codebuddy_pid=4243,
                    codebuddy_session_id='sid-1', codebuddy_chat_name='From transcript',
                    chat_name_source='custom', transcript=str(self.home / 'projects/home-codex/sid-1.jsonl'),
                    skip_reason=None, process_identity=(1, '100'))
        data.update(overrides)
        return data

    def select_calls(self):
        return [c for c in self.calls if c[0] == 'select-pane']

    def test_skip_reason_never_touches_tmux(self):
        status, _ = sync.synchronize(self.data(skip_reason='no_codebuddy_session'), self.sessions, True)
        self.assertEqual(status, 'no_codebuddy_session')
        self.assertEqual(self.calls, [])

    def test_an_identical_title_is_left_alone(self):
        status, after = sync.synchronize(self.data(pane_title='From transcript'), self.sessions, True)
        self.assertEqual((status, after), ('unchanged', 'From transcript'))
        self.assertEqual(self.select_calls(), [])

    def test_a_status_glyph_does_not_trigger_a_rewrite(self):
        """Guards the ping-pong with CodeBuddy's own renderer."""
        data = self.data(pane_title='✳ From transcript')
        self.assertEqual(sync.synchronize(data, self.sessions, True), ('unchanged', '✳ From transcript'))
        self.assertEqual(self.select_calls(), [])

    def test_dry_run_reports_without_writing(self):
        self.assertEqual(sync.synchronize(self.data(), self.sessions, False), ('would_update', None))
        self.assertEqual(self.select_calls(), [])

    def test_apply_writes_and_verifies(self):
        status, after = sync.synchronize(self.data(), self.sessions, True)
        self.assertEqual((status, after), ('updated', 'From transcript'))
        self.assertEqual(self.titles['%1'], 'From transcript')

    def test_a_hash_in_the_name_is_escaped_for_tmux_formats(self):
        (self.home / 'projects/home-codex/sid-1.jsonl').write_text(
            json.dumps({'type': 'custom-title', 'customTitle': 'fix #{pane_id} bug'}) + '\n',
            encoding='utf-8')
        self.sessions = sync.Sessions(self.home)
        status, _ = sync.synchronize(self.data(codebuddy_chat_name='fix #{pane_id} bug'), self.sessions, True)
        self.assertEqual(status, 'updated')
        self.assertEqual(self.select_calls()[0][-1], 'fix ##{pane_id} bug')
        self.assertEqual(self.titles['%1'], 'fix #{pane_id} bug')

    def test_an_identity_change_cancels_the_update(self):
        self.tree[4242] = (1, '999')  # same pid, different start ticks
        self.assertEqual(sync.synchronize(self.data(), self.sessions, True)[0], 'identity_changed')
        self.assertEqual(self.select_calls(), [])

    def test_a_title_change_after_the_backup_cancels_the_update(self):
        self.titles['%1'] = 'something else now'
        self.assertEqual(sync.synchronize(self.data(), self.sessions, True)[0], 'title_changed_since_backup')
        self.assertEqual(self.select_calls(), [])

    def test_a_verification_mismatch_is_reported(self):
        """The app overwrites the title between our write and our read-back."""
        replies = iter(['4242', 'old title', 'old title', 'overwritten by the app'])

        def fake(socket, *args):
            return '' if args[0] == 'select-pane' else next(replies)

        with mock.patch.object(sync, 'tmux', fake):
            self.assertEqual(sync.synchronize(self.data(), self.sessions, True)[0], 'verification_mismatch')


def _plist_value(text, key):
    """The value node following ``key`` in a rendered plist."""
    root = ElementTree.fromstring(text)
    children = list(root.find('dict'))
    for index, node in enumerate(children):
        if node.tag == 'key' and node.text == key:
            return children[index + 1]
    return None


class TestLaunchdPlist(unittest.TestCase):
    """The macOS backend's renderer. Pure, so launchd is never involved."""

    def render(self, **overrides):
        options = dict(label='local.x.sync', arguments=['/usr/bin/python3', '/x/y.py'],
                       environment={'PATH': '/usr/local/bin:/usr/bin'},
                       logs_dir=Path('/tmp/logs'), interval_seconds=1800,
                       abandon_process_group=False)
        options.update(overrides)
        return manage.plist_document(**options)

    def test_the_document_is_valid_xml(self):
        self.assertEqual(ElementTree.fromstring(self.render()).tag, 'plist')

    def test_label_and_program_arguments_round_trip(self):
        text = self.render(arguments=['/usr/bin/python3', '/x/y.py', '--apply'])
        self.assertEqual(_plist_value(text, 'Label').text, 'local.x.sync')
        arguments = _plist_value(text, 'ProgramArguments')
        self.assertEqual([node.text for node in arguments], ['/usr/bin/python3', '/x/y.py', '--apply'])

    def test_run_at_load_is_set(self):
        self.assertEqual(_plist_value(self.render(), 'RunAtLoad').tag, 'true')

    def test_the_interval_is_in_seconds(self):
        self.assertEqual(_plist_value(self.render(interval_seconds=1800), 'StartInterval').text, '1800')

    def test_an_absent_interval_omits_the_key(self):
        self.assertIsNone(_plist_value(self.render(interval_seconds=None), 'StartInterval'))

    def test_abandon_process_group_is_opt_in(self):
        self.assertIsNone(_plist_value(self.render(), 'AbandonProcessGroup'))
        self.assertEqual(_plist_value(self.render(abandon_process_group=True),
                                      'AbandonProcessGroup').tag, 'true')

    def test_the_path_is_carried_explicitly(self):
        """launchd's default PATH omits /usr/local/bin, which is where tmux is."""
        node = _plist_value(self.render(), 'EnvironmentVariables')
        pairs = list(node)
        values = {pairs[i].text: pairs[i + 1].text for i in range(0, len(pairs), 2)}
        self.assertEqual(values, {'PATH': '/usr/local/bin:/usr/bin'})

    def test_log_paths_are_derived_from_the_label(self):
        text = self.render(label='local.x.restore')
        self.assertEqual(_plist_value(text, 'StandardOutPath').text, '/tmp/logs/local.x.restore.out.log')
        self.assertEqual(_plist_value(text, 'StandardErrorPath').text, '/tmp/logs/local.x.restore.err.log')

    def test_xml_special_characters_are_escaped(self):
        text = self.render(arguments=['--name', 'a&b<c>"d"'])
        self.assertEqual([node.text for node in _plist_value(text, 'ProgramArguments')],
                         ['--name', 'a&b<c>"d"'])


class TestLaunchctl(unittest.TestCase):
    def test_bootstrap_boots_out_first(self):
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            return mock.Mock(returncode=0)

        with mock.patch.object(manage, 'launchctl', fake):
            self.assertTrue(manage.bootstrap_agent(Path('/tmp/x.plist'), 'local.x'))
        domain = f'gui/{os.getuid()}'
        self.assertEqual(calls[0], ('bootout', f'{domain}/local.x'))
        self.assertEqual(calls[1], ('bootstrap', domain, '/tmp/x.plist'))

    def test_bootstrap_retries_once_after_a_transient_refusal(self):
        results = [1, 0]
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if args[0] == 'bootstrap':
                return mock.Mock(returncode=results.pop(0))
            return mock.Mock(returncode=0)

        with mock.patch.object(manage, 'launchctl', fake):
            self.assertTrue(manage.bootstrap_agent(Path('/tmp/x.plist'), 'local.x'))
        self.assertEqual([call[0] for call in calls],
                         ['bootout', 'bootstrap', 'bootout', 'bootstrap'])

    def test_bootstrap_reports_persistent_failure(self):
        with mock.patch.object(manage, 'launchctl', lambda *a, **k: mock.Mock(returncode=1)):
            self.assertFalse(manage.bootstrap_agent(Path('/tmp/x.plist'), 'local.x'))

    def test_deactivate_boots_out_both_agents(self):
        calls = []
        with mock.patch.object(manage, 'launchctl',
                               lambda *a, **k: (calls.append(a), mock.Mock(returncode=0))[1]), \
                mock.patch.object(manage.compat, 'PLATFORM', 'darwin'):
            manage.deactivate(mock.Mock())
        domain = f'gui/{os.getuid()}'
        self.assertEqual([call[1] for call in calls],
                         [f'{domain}/{manage.SYNC_LABEL}', f'{domain}/{manage.RESTORE_LABEL}'])


class TestInstallOnMacOS(unittest.TestCase):
    """install() must write the plists and never bootstrap the restore agent.

    RunAtLoad on the restore agent would fire a restore during install, which
    would start conversations the user did not ask for.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / 'home'
        (self.home / '.local/bin').mkdir(parents=True)
        tmux_dir = self.tmp / 'bin'
        tmux_dir.mkdir()
        self.tmux = tmux_dir / 'tmux'
        self.tmux.write_text('#!/bin/sh\n', encoding='utf-8')
        self.tmux.chmod(0o700)
        self.calls = []

        def fake_launchctl(*args, **kwargs):
            self.calls.append(args)
            return mock.Mock(returncode=0)

        self.patches = [
            mock.patch.object(Path, 'home', return_value=self.home),
            mock.patch.object(manage, 'launchctl', fake_launchctl),
            mock.patch.object(manage.compat, 'PLATFORM', 'darwin'),
            mock.patch.object(manage.shutil, 'which', return_value=str(self.tmux)),
            mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.tmp / 'state'),
                                         'XDG_CONFIG_HOME': str(self.tmp / 'config')}),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def args(self, **overrides):
        values = dict(interval_minutes=None, codebuddy_home=None, state_dir=None,
                      socket=None, name_source=None, launcher_command=None,
                      workbuddy_command=None, restore_layout=None, stagger_seconds=None,
                      no_hook=True, no_restore=False, no_start=False)
        values.update(overrides)
        return mock.Mock(**values)

    def install(self, **overrides):
        layout = manage.Layout({}, None)
        # install() prints a summary; keep it out of the test output.
        with contextlib.redirect_stdout(io.StringIO()):
            manage.install(self.args(**overrides), layout)
        return layout

    def test_the_plists_are_written_with_the_right_interval(self):
        layout = self.install(interval_minutes=45)
        self.assertTrue(layout.sync_agent.is_file())
        self.assertTrue(layout.restore_agent.is_file())
        text = layout.sync_agent.read_text(encoding='utf-8')
        self.assertEqual(_plist_value(text, 'StartInterval').text, str(45 * 60))

    def test_the_restore_agent_is_never_bootstrapped(self):
        layout = self.install()
        bootstrapped = [call[2] for call in self.calls if call[0] == 'bootstrap']
        self.assertEqual(bootstrapped, [str(layout.sync_agent)])

    def test_the_restore_agent_passes_a_delay(self):
        layout = self.install()
        arguments = [node.text for node in _plist_value(
            layout.restore_agent.read_text(encoding='utf-8'), 'ProgramArguments')]
        self.assertIn('--delay-seconds', arguments)
        self.assertEqual(arguments[arguments.index('--delay-seconds') + 1],
                         str(manage.RESTORE_DELAY_SECONDS))

    def test_the_shared_module_is_installed_beside_the_scripts(self):
        layout = self.install()
        self.assertTrue(layout.platform_module.is_file())
        self.assertEqual(layout.platform_module.read_bytes(),
                         (manage.ROOT / 'platform_compat.py').read_bytes())

    def test_the_installed_scripts_actually_run(self):
        """verify_installed_scripts is exercised for real, not stubbed."""
        layout = self.install()
        self.assertTrue(layout.script.is_file())
        self.assertTrue(layout.restore_script.is_file())

    def test_a_missing_shared_module_fails_the_install(self):
        """Proven in two parts, to avoid patching shutil.copyfile globally."""
        with mock.patch.object(manage, 'verify_installed_scripts') as verify:
            self.install()
        verify.assert_called_once()

        layout = self.install()
        layout.platform_module.unlink()
        with self.assertRaises(SystemExit) as caught:
            manage.verify_installed_scripts(layout, str(Path(sys.executable).resolve()))
        self.assertIn('platform_compat.py', str(caught.exception))

    def test_no_restore_omits_the_restore_agent(self):
        layout = self.install(no_restore=True)
        self.assertFalse(layout.restore_agent.exists())
        self.assertTrue(layout.sync_agent.exists())

    def test_uninstall_removes_the_plists_but_keeps_the_config(self):
        layout = self.install()
        manage.uninstall(layout, no_start=False)
        self.assertFalse(layout.sync_agent.exists())
        self.assertFalse(layout.restore_agent.exists())
        self.assertFalse(layout.script.exists())
        self.assertFalse(layout.platform_module.exists())
        self.assertTrue(layout.config_file.exists(), 'configuration is retained')


class TestHookRegistration(unittest.TestCase):
    """Mirrors the real settings.json: the turn hook lives on both events."""

    TURN = {'type': 'command', 'command': '/usr/bin/python3 /home/codex/hooks/turn_hook.py',
            'timeout': 15}

    def setUp(self):
        self.settings = {
            'model': 'deepseek-v4.1-flash',
            'hooks': {event: [{'hooks': [dict(self.TURN)]}] for event in manage.HOOK_EVENTS},
        }
        self.command = '/usr/bin/python3 /home/codex/.codebuddy/hooks/codebuddy_pane_sync_hook.py'

    def commands(self, event):
        return [entry['command'] for group in self.settings['hooks'][event]
                for entry in group['hooks']]

    def test_adds_a_group_without_disturbing_existing_entries(self):
        self.assertTrue(manage.add_hook(self.settings, 'Stop', self.command))
        self.assertEqual(len(self.settings['hooks']['Stop']), 2)
        self.assertIn(self.TURN['command'], self.commands('Stop'))
        self.assertEqual(self.settings['model'], 'deepseek-v4.1-flash')

    def test_is_idempotent(self):
        manage.add_hook(self.settings, 'UserPromptSubmit', self.command)
        before = json.dumps(self.settings, sort_keys=True)
        self.assertFalse(manage.add_hook(self.settings, 'UserPromptSubmit', self.command))
        self.assertEqual(json.dumps(self.settings, sort_keys=True), before)
        self.assertEqual(len(self.settings['hooks']['UserPromptSubmit']), 2)

    def test_refreshes_a_stale_command_in_place(self):
        manage.add_hook(self.settings, 'Stop', self.command)
        self.assertTrue(manage.add_hook(self.settings, 'Stop',
                                        self.command.replace('/usr/bin/python3',
                                                             '/usr/bin/python3.12')))
        self.assertEqual(len(self.settings['hooks']['Stop']), 2)
        self.assertIn('python3.12', self.commands('Stop')[1])

    def test_a_repeated_add_never_duplicates(self):
        for _ in range(3):
            manage.add_hook(self.settings, 'Stop', self.command)
        self.assertEqual(self.commands('Stop').count(self.command), 1)

    def test_removes_only_our_hook(self):
        manage.register_hooks(self.settings, self.command)
        self.assertTrue(manage.remove_hook(self.settings, 'UserPromptSubmit'))
        self.assertEqual(self.commands('UserPromptSubmit'), [self.TURN['command']])
        self.assertTrue(manage.remove_hook(self.settings, 'Stop'))
        self.assertEqual(self.commands('Stop'), [self.TURN['command']])

    def test_our_added_event_is_dropped_when_it_becomes_empty(self):
        manage.add_hook(self.settings, 'SessionStart', self.command)
        self.assertTrue(manage.remove_hook(self.settings, 'SessionStart'))
        self.assertNotIn('SessionStart', self.settings['hooks'])

    def test_removing_when_absent_reports_no_change(self):
        self.assertFalse(manage.remove_hook(self.settings, 'Stop'))

    def test_registering_covers_every_event(self):
        """A short-circuiting any() would silently leave Stop unregistered."""
        self.assertTrue(manage.register_hooks(self.settings, self.command))
        for event in manage.HOOK_EVENTS:
            self.assertIn(self.command, self.commands(event), f'{event} was not registered')
            self.assertIn(self.TURN['command'], self.commands(event))

    def test_registering_twice_reports_no_change(self):
        manage.register_hooks(self.settings, self.command)
        self.assertFalse(manage.register_hooks(self.settings, self.command))

    def test_unregistering_covers_every_event(self):
        manage.register_hooks(self.settings, self.command)
        self.assertTrue(manage.unregister_hooks(self.settings))
        for event in manage.HOOK_EVENTS:
            self.assertEqual(self.commands(event), [self.TURN['command']])
        self.assertFalse(manage.unregister_hooks(self.settings))


class TestHookScript(unittest.TestCase):
    """Run the hook exactly as CodeBuddy does: JSON on stdin, HOME redirected."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / 'invocations.jsonl'
        script = self.tmp / '.local/bin/tmux-codebuddy-pane-sync.py'
        script.parent.mkdir(parents=True)
        script.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'open(os.environ["HOOK_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\n',
            encoding='utf-8')
        script.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.tmp), HOOK_LOG=str(self.log))

    def run_hook(self, event):
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), text=True,
                                capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        return result

    def invocations(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_stop_synchronizes_only_that_session(self):
        self.run_hook({'hook_event_name': 'Stop', 'session_id': 'abc-123'})
        args = self.invocations()
        self.assertEqual(len(args), 1)
        self.assertEqual(args[0], ['--apply', '--quiet', '--only-session', 'abc-123'])

    def test_user_prompt_submit_is_handled(self):
        self.run_hook({'hook_event_name': 'UserPromptSubmit', 'session_id': 'abc-456'})
        self.assertEqual(len(self.invocations()), 1)

    def test_other_events_are_ignored(self):
        self.run_hook({'hook_event_name': 'SessionStart', 'session_id': 'abc-123'})
        self.assertEqual(self.invocations(), [])

    def test_missing_session_is_ignored(self):
        self.run_hook({'hook_event_name': 'Stop'})
        self.assertEqual(self.invocations(), [])

    def test_garbage_on_stdin_never_fails(self):
        result = subprocess.run([sys.executable, str(HOOK)], input='not json', text=True,
                                capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_a_missing_install_is_not_an_error(self):
        script = self.tmp / '.local/bin/tmux-codebuddy-pane-sync.py'
        script.unlink()
        self.run_hook({'hook_event_name': 'Stop', 'session_id': 'abc-123'})
        self.assertEqual(self.invocations(), [])


@unittest.skipUnless(TMUX, 'tmux is required')
class TestEndToEnd(unittest.TestCase):
    """A real tmux server, a fake CodeBuddy process, and the real script."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / 'codebuddy'
        (self.home / 'sessions').mkdir(parents=True)
        (self.home / 'projects').mkdir()
        self.state = self.tmp / 'state'
        self.socket = self.tmp / 'tmux.sock'
        self.session_id = '01a00000-0000-7000-8000-000000000001'
        self.workdir = self.tmp / 'work'
        self.workdir.mkdir()
        self.tmux('new-session', '-d', '-s', 't', '-x', '100', '-y', '30')
        self.addCleanup(self.kill_server)
        # tmux defaults a pane title to the hostname, so remember it rather than
        # assuming an empty title.
        self.initial_title = self.title()

    def tmux(self, *args):
        return subprocess.check_output([TMUX, '-S', str(self.socket), *args],
                                       text=True, stderr=subprocess.STDOUT).strip()

    def kill_server(self):
        subprocess.run([TMUX, '-S', str(self.socket), 'kill-server'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def title(self):
        return self.tmux('display-message', '-p', '-t', '%0', '#{pane_title}')

    def start_fake_codebuddy(self):
        """Run a script literally named `codebuddy` inside the pane."""
        binary = self.tmp / 'bin' / 'codebuddy'
        binary.parent.mkdir(parents=True)
        # No exec: the shell keeps argv[1] pointing at .../bin/codebuddy.
        binary.write_text('#!/bin/sh\nsleep 300\n', encoding='utf-8')
        binary.chmod(0o700)
        self.tmux('send-keys', '-t', '%0', str(binary), 'Enter')
        pane_pid = int(self.tmux('display-message', '-p', '-t', '%0', '#{pane_pid}'))
        # Found through the shared platform layer rather than /proc, so this
        # works on macOS too. The last argv token is compared after resolving,
        # because macOS reports /private/var/... where mkdtemp said /var/...
        target = binary.resolve()
        for _ in range(100):
            children = [pid for pid, command in pc.process_commands().items()
                        if command and Path(command.split()[-1]).resolve() == target
                        and len(command.split()) == 2]
            if children:
                pid = children[0]
                (self.home / 'sessions' / f'{pid}.json').write_text(json.dumps(
                    {'pid': pid, 'sessionId': self.session_id, 'cwd': str(self.workdir),
                     'kind': 'interactive'}), encoding='utf-8')
                return pid
            if not self.tmux('list-panes', '-t', '%0', '-F', '#{pane_id}'):
                self.fail('pane disappeared')
            subprocess.run(['sleep', '0.05'], check=True)
        self.fail(f'fake codebuddy never started under pane pid {pane_pid}')

    @staticmethod
    def cmdline(pid):
        """Command line of a pid, via the shared platform layer."""
        return pc.process_commands().get(int(pid), '')

    def write_transcript(self, custom=None, ai=None):
        rows = [{'type': 'message', 'role': 'user', 'content': 'hi'}]
        if ai:
            rows.append({'type': 'ai-title', 'aiTitle': ai})
        if custom:
            rows.append({'type': 'custom-title', 'customTitle': custom})
        slug = str(self.workdir).strip('/').replace('/', '-')
        path = self.home / 'projects' / slug / f'{self.session_id}.jsonl'
        write_jsonl(path, rows)
        return path

    def run_sync(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--socket', str(self.socket),
             '--codebuddy-home', str(self.home), '--state-dir', str(self.state), *extra],
            text=True, capture_output=True, check=False)

    def journal(self):
        return read_journal(self.state / 'pane-names.jsonl')

    def test_backs_up_then_updates_the_pane_title(self):
        self.start_fake_codebuddy()
        self.write_transcript(custom='全球同步-Windterm', ai='Load machine and storage info')
        result = self.run_sync('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.title(), '全球同步-Windterm')
        events = self.journal()
        backup_at = next(i for i, e in enumerate(events) if e['event'] == 'backup')
        updated_at = next(i for i, e in enumerate(events)
                          if e['event'] == 'result' and e['status'] == 'updated')
        self.assertLess(backup_at, updated_at)
        backup = events[backup_at]
        self.assertEqual(backup['codebuddy_session_id'], self.session_id)
        self.assertEqual(backup['codebuddy_chat_name'], '全球同步-Windterm')
        self.assertEqual(backup['pane_title'], self.initial_title)
        self.assertEqual(events[updated_at]['pane_title_after'], '全球同步-Windterm')

    def test_the_ai_title_is_used_when_nothing_was_renamed(self):
        self.start_fake_codebuddy()
        self.write_transcript(ai='Load machine and storage info')
        self.run_sync('--apply')
        self.assertEqual(self.title(), 'Load machine and storage info')

    def test_a_second_run_is_a_no_op(self):
        self.start_fake_codebuddy()
        self.write_transcript(custom='steady')
        self.run_sync('--apply')
        self.run_sync('--apply')
        statuses = [e['status'] for e in self.journal() if e['event'] == 'result']
        self.assertEqual(statuses, ['updated', 'unchanged'])

    def test_a_status_glyph_in_the_pane_title_is_treated_as_a_match(self):
        self.start_fake_codebuddy()
        self.write_transcript(custom='steady')
        self.tmux('select-pane', '-t', '%0', '-T', '⠙ steady')
        self.run_sync('--apply')
        self.assertEqual(self.title(), '⠙ steady')
        statuses = [e['status'] for e in self.journal() if e['event'] == 'result']
        self.assertIn('unchanged', statuses)

    def test_nothing_is_written_without_apply(self):
        self.start_fake_codebuddy()
        self.write_transcript(custom='dry only')
        self.run_sync()
        self.assertEqual(self.title(), self.initial_title)
        statuses = [e['status'] for e in self.journal() if e['event'] == 'result']
        self.assertIn('would_update', statuses)

    def test_only_session_skips_unrelated_panes(self):
        self.start_fake_codebuddy()
        self.write_transcript(custom='should not appear')
        self.run_sync('--apply', '--only-session', 'some-other-session')
        self.assertEqual(self.title(), self.initial_title)
        self.assertEqual([e for e in self.journal() if e['event'] == 'backup'], [])

    def test_only_session_writes_nothing_for_a_pane_without_codebuddy(self):
        """The hook path must not append a record per unrelated pane."""
        self.run_sync('--apply', '--only-session', 'some-other-session')
        events = self.journal()
        self.assertEqual([e for e in events if e['event'] == 'result'], [])
        self.assertEqual([e for e in events if e['event'] == 'backup'], [])

    def test_a_session_without_a_transcript_is_skipped(self):
        pid = self.start_fake_codebuddy()
        (self.home / 'projects').mkdir(exist_ok=True)
        self.run_sync('--apply')
        self.assertEqual(self.title(), self.initial_title)
        statuses = [e['status'] for e in self.journal() if e['event'] == 'result']
        self.assertIn('no_transcript', statuses)
        self.assertTrue(pid)

    def test_a_pane_without_codebuddy_is_skipped(self):
        self.run_sync('--apply')
        self.assertEqual(self.title(), self.initial_title)
        statuses = [e['status'] for e in self.journal() if e['event'] == 'result']
        self.assertEqual(statuses, ['no_codebuddy_session'])

    def test_control_characters_are_backed_up_but_not_applied(self):
        self.start_fake_codebuddy()
        self.write_transcript(custom='bad\x07name')
        self.run_sync('--apply')
        self.assertEqual(self.title(), self.initial_title)
        results = [e for e in self.journal() if e['event'] == 'result']
        self.assertEqual(results[0]['status'], 'unsafe_chat_name')
        self.assertEqual(results[0]['codebuddy_chat_name'], 'bad\x07name')

    def test_the_lock_prevents_a_concurrent_run(self):
        import fcntl
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / 'sync.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_sync('--apply')
        self.assertEqual(result.returncode, 0)
        self.assertIn('Another synchronization is running', result.stdout)
        self.assertFalse((self.state / 'pane-names.jsonl').exists())


class TestPaneOrdinals(unittest.TestCase):
    """Layout position, not tmux index, is what survives a reboot."""

    def test_orders_windows_and_panes_with_gaps(self):
        rows = [dict(session_id='$1', pane_id='%a', window_index=6, pane_index=13),
                dict(session_id='$1', pane_id='%b', window_index=6, pane_index=19),
                dict(session_id='$1', pane_id='%c', window_index=15, pane_index=37),
                dict(session_id='$2', pane_id='%d', window_index=0, pane_index=0)]
        self.assertEqual(sync.pane_ordinals(rows),
                         {'%a': (0, 0), '%b': (0, 1), '%c': (1, 0), '%d': (0, 0)})

    def test_renumbering_does_not_change_the_ordinals(self):
        def rows(offset):
            return [dict(session_id='$1', pane_id='%a', window_index=0 + offset, pane_index=0 + offset),
                    dict(session_id='$1', pane_id='%b', window_index=0 + offset, pane_index=1 + offset)]
        self.assertEqual(sync.pane_ordinals(rows(0)), sync.pane_ordinals(rows(6)))

    def test_sessions_are_counted_separately(self):
        rows = [dict(session_id='$1', pane_id='%a', window_index=0, pane_index=0),
                dict(session_id='$2', pane_id='%b', window_index=0, pane_index=0)]
        self.assertEqual(sync.pane_ordinals(rows), {'%a': (0, 0), '%b': (0, 0)})


class TestLiveEndpoint(SessionsTestCase):
    """The process's own loopback API outranks the pid file after a /resume."""

    def setUp(self):
        super().setUp()
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            payload = {'data': {'sessionId': 'live-id', 'writerOccupied': False}}
            status = 200

            def do_GET(self):
                body = json.dumps(Handler.payload).encode('utf-8')
                self.send_response(Handler.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.handler = Handler
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.sessions = sync.Sessions(self.home)

    def test_reads_the_session_id_from_the_endpoint(self):
        self.assertEqual(self.sessions.live_session(self.url), 'live-id')

    def test_a_refused_port_returns_none(self):
        self.server.shutdown()
        self.assertIsNone(self.sessions.live_session(self.url))

    def test_a_non_loopback_url_is_never_contacted(self):
        self.assertIsNone(self.sessions.live_session('http://10.11.12.13:1'))

    def test_a_malformed_reply_returns_none(self):
        self.handler.payload = {'unexpected': True}
        self.assertIsNone(self.sessions.live_session(self.url))

    def test_a_non_json_reply_returns_none(self):
        self.handler.payload = None
        self.assertIsNone(self.sessions.live_session(self.url))

    def record(self, pid, session_id, url):
        (self.home / 'sessions' / f'{pid}.json').write_text(json.dumps(
            {'pid': pid, 'sessionId': session_id, 'cwd': '/home/codex', 'kind': 'interactive',
             'url': url}), encoding='utf-8')

    def tree(self):
        return {10: (1, '1'), 11: (10, '2')}

    def test_the_endpoint_outranks_a_stale_pid_file(self):
        for session_id, name in (('live-id', 'The one on screen'), ('stale-id', 'The abandoned one')):
            write_jsonl(self.home / 'projects' / 'home-codex' / f'{session_id}.jsonl',
                        [{'type': 'custom-title', 'customTitle': name}])
        with mock.patch.object(sync.Sessions, 'is_codebuddy', staticmethod(lambda pid: pid == 11)):
            self.record(11, 'stale-id', self.url)
            info, reason = self.sessions.inspect(10, self.tree())
        self.assertIsNone(reason)
        self.assertEqual(info['session_id'], 'live-id')
        self.assertEqual(info['name'], 'The one on screen')
        self.assertEqual(info['pid_file_session_id'], 'stale-id')
        self.assertEqual(info['session_id_source'], 'endpoint')

    def test_the_pid_file_is_used_when_the_endpoint_is_silent(self):
        write_jsonl(self.home / 'projects' / 'home-codex' / 'pid-file-id.jsonl',
                    [{'type': 'custom-title', 'customTitle': 'From the pid file'}])
        with mock.patch.object(sync.Sessions, 'is_codebuddy', staticmethod(lambda pid: pid == 11)):
            self.record(11, 'pid-file-id', 'http://127.0.0.1:9')
            info, reason = self.sessions.inspect(10, self.tree())
        self.assertIsNone(reason)
        self.assertEqual(info['session_id'], 'pid-file-id')
        self.assertEqual(info['session_id_source'], 'pid_file')

    def test_a_scoped_run_still_resolves_through_the_endpoint(self):
        """--only-session filters on the resolved id, so it must be the live one."""
        with mock.patch.object(sync.Sessions, 'is_codebuddy', staticmethod(lambda pid: pid == 11)):
            self.record(11, 'stale-id', self.url)
            info, _ = self.sessions.inspect(10, self.tree())
        self.assertIn(info['session_id'], {'live-id'})


class TestTitleDisambiguation(SessionsTestCase):
    """Two CodeBuddy processes under one pane are separated by the rendered title."""

    TREE = {10: (1, '1'), 11: (10, '2'), 12: (10, '3')}

    def setUp(self):
        super().setUp()
        self.sessions = sync.Sessions(self.home)
        for pid, session_id, name in ((11, 'sid-a', 'First conversation'),
                                      (12, 'sid-b', 'Second conversation')):
            (self.home / 'sessions' / f'{pid}.json').write_text(json.dumps(
                {'pid': pid, 'sessionId': session_id, 'cwd': '/home/codex',
                 'kind': 'interactive'}), encoding='utf-8')
            write_jsonl(self.home / 'projects' / 'home-codex' / f'{session_id}.jsonl',
                        [{'type': 'custom-title', 'customTitle': name}])

    def test_the_matching_title_picks_the_process(self):
        with mock.patch.object(sync.Sessions, 'is_codebuddy',
                               staticmethod(lambda pid: pid in (11, 12))):
            info, reason = self.sessions.inspect(10, self.TREE, '✳ Second conversation')
        self.assertIsNone(reason)
        self.assertEqual(info['session_id'], 'sid-b')

    def test_an_unhelpful_title_stays_ambiguous(self):
        with mock.patch.object(sync.Sessions, 'is_codebuddy',
                               staticmethod(lambda pid: pid in (11, 12))):
            info, reason = self.sessions.inspect(10, self.TREE, 'something else')
        self.assertIsNone(info)
        self.assertEqual(reason, 'ambiguous_sessions')


if __name__ == '__main__':
    unittest.main()
