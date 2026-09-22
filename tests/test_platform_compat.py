"""Tests for platform_compat, the module both programs share.

These are deliberately host-independent: the parsers are fed recorded
`ps`/`sysctl` output rather than live commands, and the platform switch is
driven by patching ``platform_compat.PLATFORM`` (which every dispatch reads at
call time). Only two tests touch the real system, and both are guarded.
"""
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import platform_compat as pc  # noqa: E402

# Recorded on macOS 26.6.2 (arm64), so the parser is tested against the real
# shapes: a double-padded day field and trailing padding.
PS_TREE_OUTPUT = """\
    1     0 Mon Sep  7 17:23:23 2026    
  521     1 Mon Sep  7 17:24:44 2026    
  522     1 Mon Sep  7 17:24:44 2026    
 79552     1 Tue Sep 22 16:22:14 2026    
"""

PS_COMMAND_OUTPUT = """\
    1 /sbin/launchd
79552 node /Users/mac/.nvm/versions/node/v22.23.2/bin/codebuddy
 2834 /Applications/ChatGPT.app/Contents/Resources/codex -c features.code_mode_host=true
 9001 /bin/sh /tmp/x/bin/codebuddy
 9002 vim codebuddy
 9003 grep -r codebuddy /tmp
 9004 bash -c 'FOO=codebuddy echo hi'
"""

BOOTTIME = '{ sec = 1788773004, usec = 167243 } Mon Sep  7 17:23:24 2026'


class BootTimeTests(unittest.TestCase):
    def test_parses_the_sysctl_shape(self):
        self.assertEqual(pc.normalize_boottime(BOOTTIME), 'macos:1788773004.167243')

    def test_tolerates_irregular_spacing(self):
        self.assertEqual(pc.normalize_boottime('{sec=5,usec=6} x'), 'macos:5.6')

    def test_rejects_unparseable_input(self):
        for value in ('', None, 'no numbers here', '{ sec = abc }'):
            self.assertIsNone(pc.normalize_boottime(value))

    def test_the_two_calls_agree(self):
        """The whole point of the module: one implementation, so one string."""
        first, second = pc.boot_id(), pc.boot_id()
        self.assertIsNotNone(first, 'boot_id() found no identity on this host')
        self.assertEqual(first, second)

    def test_linux_uses_proc(self):
        with mock.patch.object(pc, 'PLATFORM', 'linux'), \
                mock.patch.object(pc.Path, 'read_text',
                                  return_value='  deadbeef\n'):
            self.assertEqual(pc.boot_id(), 'deadbeef')

    def test_darwin_prefers_the_session_uuid(self):
        seen = []

        def fake_run(argv):
            seen.append(argv)
            if argv[-1] == 'kern.bootsessionuuid':
                return 'C1325407-6E06-4C57-8DB5-F4075188595C\n'
            return None

        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', fake_run):
            self.assertEqual(pc.boot_id(), 'macos:C1325407-6E06-4C57-8DB5-F4075188595C')
        self.assertEqual(len(seen), 1, 'the fallbacks should not have been tried')

    def test_darwin_falls_back_to_boottime(self):
        def fake_run(argv):
            return BOOTTIME if argv[-1] == 'kern.boottime' else None

        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', fake_run):
            self.assertEqual(pc.boot_id(), 'macos:1788773004.167243')

    def test_darwin_falls_back_to_pid_one(self):
        def fake_run(argv):
            if argv[:2] == ['ps', '-o']:
                return 'Mon Sep  7 17:23:23 2026\n'
            return None

        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', fake_run):
            self.assertEqual(pc.boot_id(), 'launchd:Mon Sep  7 17:23:23 2026')

    def test_darwin_gives_up_cleanly(self):
        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', lambda argv: None):
            self.assertIsNone(pc.boot_id())


class ProcessTableTests(unittest.TestCase):
    def test_parses_ps_tree_output(self):
        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', lambda argv: PS_TREE_OUTPUT):
            tree = pc.processes()
        self.assertEqual(tree[79552][0], 1)
        self.assertEqual(tree[521], (1, 'Mon Sep  7 17:24:44 2026'))
        self.assertEqual(sorted(tree), [1, 521, 522, 79552])

    def test_tree_ignores_junk_lines(self):
        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', lambda argv: 'garbage\n\n  7  8 x\n'):
            tree = pc.processes()
        self.assertEqual(tree, {7: (8, 'x')})

    def test_a_failed_command_yields_an_empty_table(self):
        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', lambda argv: None):
            self.assertEqual(pc.processes(), {})

    def test_descendants_walks_the_whole_subtree(self):
        tree = {1: (0, 'a'), 2: (1, 'b'), 3: (2, 'c'), 4: (1, 'd'), 9: (8, 'e')}
        self.assertEqual(pc.descendants(1, tree), {1, 2, 3, 4})
        self.assertEqual(pc.descendants(3, tree), {3})
        self.assertEqual(pc.descendants(99, tree), {99})

    def test_the_cache_can_be_reused_and_refreshed(self):
        calls = []

        def fake(argv):
            calls.append(argv)
            return PS_TREE_OUTPUT

        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', fake):
            pc.processes(0.0)
            pc.processes(30.0)
            self.assertEqual(len(calls), 1, 'the second call should hit the cache')
            pc.processes(0.0)
            self.assertEqual(len(calls), 2, 'max_age=0 must re-read')


class CommandMatchingTests(unittest.TestCase):
    def test_matches_the_real_node_wrapper(self):
        """The CLI on this machine runs as `node …/bin/codebuddy`."""
        self.assertTrue(pc.command_matches(
            'node /Users/mac/.nvm/versions/node/v22.23.2/bin/codebuddy'))

    def test_matches_a_path_at_argv0(self):
        self.assertTrue(pc.command_matches('/usr/local/bin/codebuddy'))
        self.assertTrue(pc.command_matches('/opt/wb/bin/workbuddy -r sid'))

    def test_matches_a_bare_name_at_argv0(self):
        self.assertTrue(pc.command_matches('codebuddy'))
        self.assertTrue(pc.command_matches('codebuddy -r 01a0c818'))

    def test_matches_a_shell_wrapper(self):
        self.assertTrue(pc.command_matches('/bin/sh /tmp/x/bin/codebuddy'))

    def test_does_not_match_the_name_as_a_mere_argument(self):
        self.assertFalse(pc.command_matches('vim codebuddy'))
        self.assertFalse(pc.command_matches('grep -r codebuddy /tmp'))
        self.assertFalse(pc.command_matches("bash -c 'FOO=codebuddy echo hi'"))

    def test_does_not_match_a_longer_name(self):
        self.assertFalse(pc.command_matches('codebuddy-helper'))
        self.assertFalse(pc.command_matches('/tmp/codebuddy_backup'))

    def test_handles_empty_input(self):
        for value in ('', None, '   '):
            self.assertFalse(pc.command_matches(value))

    def test_codebuddy_pids_filters_the_table(self):
        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', lambda argv: PS_COMMAND_OUTPUT):
            self.assertEqual(pc.codebuddy_pids(), {79552, 9001})

    def test_is_codebuddy_uses_the_batch_on_darwin(self):
        with mock.patch.object(pc, 'PLATFORM', 'darwin'), \
                mock.patch.object(pc, '_run', lambda argv: PS_COMMAND_OUTPUT):
            self.assertTrue(pc.is_codebuddy(79552))
            self.assertTrue(pc.is_codebuddy(9001))
            self.assertFalse(pc.is_codebuddy(9002))
            self.assertFalse(pc.is_codebuddy(1))


class SocketCandidateTests(unittest.TestCase):
    """Discovery must consult every place tmux might have put its socket.

    These assert containment and ordering rather than exact lists, because the
    host running the tests may have a real socket in /tmp — this Mac does.
    """

    def test_roots_follow_tmux_priority_order(self):
        env = {'TMUX_TMPDIR': '/run', 'TMPDIR': '/var/tmp'}
        self.assertEqual(pc.socket_roots(env=env),
                         [Path('/run'), Path('/var/tmp'), Path('/tmp')])

    def test_roots_skip_unset_variables(self):
        self.assertEqual(pc.socket_roots(env={'TMPDIR': '/var/tmp'}),
                         [Path('/var/tmp'), Path('/tmp')])
        self.assertEqual(pc.socket_roots(env={}), [Path('/tmp')])

    def test_roots_ignore_empty_values(self):
        self.assertEqual(pc.socket_roots(env={'TMPDIR': '', 'TMUX_TMPDIR': ''}),
                         [Path('/tmp')])

    def test_tmux_env_socket_is_extracted(self):
        self.assertEqual(pc.tmux_env_socket(env={'TMUX': '/run/s,79552,0'}),
                         Path('/run/s'))

    def test_tmux_env_socket_survives_a_comma_in_the_path(self):
        self.assertEqual(pc.tmux_env_socket(env={'TMUX': '/tmp/a,b/s,79552,0'}),
                         Path('/tmp/a,b/s'))

    def test_tmux_env_socket_is_absent_when_unset(self):
        for env in ({}, {'TMUX': ''}):
            self.assertIsNone(pc.tmux_env_socket(env=env))

    def test_the_named_socket_is_tried_first(self):
        env = {'TMUX': '/run/tmux-501/default,1,0'}
        names = [str(p) for p in pc.socket_candidates(env=env)]
        self.assertEqual(names[0], '/run/tmux-501/default')

    def test_every_root_contributes_its_socket_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / f'tmux-{os.getuid()}'
            directory.mkdir()
            socket_path = directory / 'default'
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(socket_path))
                names = [str(p) for p in pc.socket_candidates(env={'TMPDIR': tmp})]
            finally:
                server.close()
        self.assertIn(str(socket_path), names)

    def test_explicit_paths_replace_env_discovery(self):
        env = {'TMUX': '/ignored/x,1,0', 'TMPDIR': '/var/tmp'}
        names = [str(p) for p in pc.socket_candidates(explicit=['/s1'], env=env)]
        self.assertEqual(names, ['/s1'])

    def test_explicit_paths_also_ignore_extra(self):
        names = [str(p) for p in pc.socket_candidates(
            explicit=['/s1'], extra=['/from/manifest'], env={})]
        self.assertEqual(names, ['/s1'])

    def test_extra_paths_are_unioned_when_discovering(self):
        names = [str(p) for p in pc.socket_candidates(
            extra=['/from/manifest'], env={})]
        self.assertIn('/from/manifest', names)

    def test_duplicates_are_collapsed_by_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / 'real'
            real.mkdir()
            link = Path(tmp) / 'link'
            link.symlink_to(real)
            candidates = pc.socket_candidates(extra=[str(link)], env={'TMUX_TMPDIR': str(real)})
            resolved = [str(p.resolve()) for p in candidates]
        self.assertEqual(len(resolved), len(set(resolved)), f'not deduplicated: {resolved}')

    def test_a_missing_temporary_directory_contributes_nothing(self):
        names = [str(p) for p in pc.socket_candidates(
            env={'TMPDIR': '/nonexistent-tmpdir'})]
        invented = [n for n in names if n.startswith('/nonexistent-tmpdir')]
        self.assertEqual(invented, [], 'nothing should be invented')


class DiscoverSocketTests(unittest.TestCase):
    """Roots are pinned to a temp directory so the host's real socket is invisible.

    This machine has a live server at /tmp/tmux-501/default, and /tmp is always
    a root, so an unisolated test would see it and fail for the wrong reason.
    """

    def _discover(self, tmp, env=None, extra=()):
        with mock.patch.object(pc, 'socket_roots', lambda environment: [Path(tmp)]):
            return pc.discover_sockets(extra=extra, env=(env or {}))

    def _socket_dir(self, tmp):
        directory = Path(tmp) / f'tmux-{os.getuid()}'
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _bind(self, path):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        return server

    def test_finds_a_real_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self._socket_dir(tmp)
            server = self._bind(directory / 'default')
            try:
                found = self._discover(tmp)
            finally:
                server.close()
        self.assertEqual(found, [str(directory / 'default')])

    def test_ignores_a_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self._socket_dir(tmp)
            (directory / 'not-a-socket').write_text('hi', encoding='utf-8')
            self.assertEqual(self._discover(tmp), [])

    def test_ignores_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self._socket_dir(tmp)
            (directory / 'subdir').mkdir()
            self.assertEqual(self._discover(tmp), [])

    def test_an_empty_directory_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._socket_dir(tmp)
            self.assertEqual(self._discover(tmp), [])

    def test_a_socket_owned_by_another_user_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self._socket_dir(tmp)
            server = self._bind(directory / 'default')
            try:
                info = mock.Mock(st_mode=stat.S_IFSOCK, st_uid=os.getuid() + 1)
                with mock.patch.object(pc.Path, 'stat', lambda self: info):
                    found = self._discover(tmp)
            finally:
                server.close()
        self.assertEqual(found, [], "another user's socket must never be used")

    def test_the_named_socket_from_tmux_env_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            server_path = Path(tmp) / 'custom-sock'
            server = self._bind(server_path)
            try:
                found = self._discover(tmp, env={'TMUX': f'{server_path},1234,0'})
            finally:
                server.close()
        self.assertEqual(found, [str(server_path)])


class SharedIdentityTests(unittest.TestCase):
    """The two programs must not carry their own copies of these."""

    def test_both_programs_use_the_same_boot_id(self):
        import codebuddy_restore as restore
        import tmux_codebuddy_pane_sync as sync

        self.assertIs(sync.boot_id, pc.boot_id)
        self.assertIs(restore.boot_id, pc.boot_id)

    def test_both_programs_use_the_same_process_helpers(self):
        import codebuddy_restore as restore
        import tmux_codebuddy_pane_sync as sync

        self.assertIs(sync.processes, pc.processes)
        self.assertIs(sync.descendants, pc.descendants)
        self.assertIs(restore.processes, pc.processes)
        self.assertIs(restore.descendants, pc.descendants)

    def test_the_pane_lookup_is_shared(self):
        import codebuddy_restore as restore
        import tmux_codebuddy_pane_sync as sync

        self.assertIs(restore.is_codebuddy, pc.is_codebuddy)
        # sync keeps it on Sessions because callers patch it there; accessing the
        # attribute through the class unwraps the staticmethod.
        self.assertIs(sync.Sessions.is_codebuddy, pc.is_codebuddy)
        self.assertIs(sync.Sessions.is_codebuddy, restore.is_codebuddy)


if __name__ == '__main__':
    unittest.main()
