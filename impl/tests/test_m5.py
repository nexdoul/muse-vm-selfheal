#!/usr/bin/env python3
"""M5 sandbox identity contracts; only local fixtures and copied mutations."""
import contextlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_m4 import ROOT, Tests as M4Tests


class Tests(M4Tests):
    def relative_daemon(self):
        p = subprocess.Popen([self.node.name, 'muse-daemon.cjs', '--loop', '10000'],
                             executable='/usr/bin/python3', cwd=self.app)
        self.children.append(p)
        deadline = time.monotonic() + 3
        while not self.pidfile.exists() or not self.log.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        return p

    def test_relative_identity_without_cwd(self):
        p = self.relative_daemon()
        resolve, readlink = Path.resolve, Path.readlink
        denied = []
        def deny_cwd(path, method, *args, **kwargs):
            if str(path).startswith('/proc/') and path.name in ('cwd', 'exe'):
                denied.append(str(path))
                raise PermissionError(13, 'sandbox denial', str(path))
            return method(path, *args, **kwargs)
        with patch.object(Path, 'resolve', lambda p, *a, **kw: deny_cwd(p, resolve, *a, **kw)), \
             patch.object(Path, 'readlink', lambda p, *a, **kw: deny_cwd(p, readlink, *a, **kw)), \
             contextlib.redirect_stderr(io.StringIO()) as log:
            token = self.approver.identity(p.pid, self.app, self.node)
            self.assertEqual(token, Path(f'/proc/{p.pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
            self.assertEqual(self.main(), 0)
            self.assertEqual(denied, [f'/proc/{os.getpid()}/cwd'])
            self.assertEqual(log.getvalue().count('cwd unavailable'), 1)
            self.write_log(age=1000)
            with patch.object(self.approver.signal, 'pidfd_send_signal') as send:
                self.assertEqual(self.main('repair'), 0)
                send.assert_called_once()
        self.adapter('approver-check', rc=1)  # stale business evidence still fails

    def test_relative_pidfile_binding(self):
        p = self.relative_daemon()
        self.assertIsNotNone(self.approver.identity(p.pid, self.app, self.node))
        self.pidfile.write_text(str(p.pid + 1))
        with self.assertRaises(self.approver.IdentityUnavailable):
            self.approver.identity(p.pid, self.app, self.node)
        self.assertEqual(self.main(), 2)
        self.adapter('approver-check', rc=2)
        self.pidfile.write_text(str(p.pid))
        other = self.root / 'other'; other.mkdir(); (other / 'data').mkdir()
        with self.assertRaises(self.approver.IdentityUnavailable):
            self.approver.identity(p.pid, other, self.node)
        for contents in ('', 'not-a-pid'):
            self.pidfile.write_text(contents)
            self.assertEqual(self.main(), 2)
        self.pidfile.unlink()
        with patch.object(self.approver.subprocess, 'run') as run, \
             patch.object(self.approver.subprocess, 'Popen') as launch, \
             patch.object(self.approver.signal, 'pidfd_send_signal') as send:
            self.assertEqual(self.main('repair'), 2)
            run.assert_not_called(); launch.assert_not_called(); send.assert_not_called()
        self.assertIsNone(p.poll())

    def test_relative_starttime_evidence(self):
        p = self.relative_daemon()
        token = self.approver.identity(p.pid, self.app, self.node)
        boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                    if line.startswith('btime '))
        started = boot + int(token) / os.sysconf('SC_CLK_TCK')
        for modified in (started - 10, time.time() + 3600):
            os.utime(self.pidfile, (modified, modified))
            with self.assertRaises(self.approver.IdentityUnavailable):
                self.approver.identity(p.pid, self.app, self.node)
            self.assertEqual(self.main(), 2)
        os.utime(self.pidfile, None)
        self.assertEqual(self.main(), 0)
        self.write_log(age=1000)
        with patch.object(self.approver, 'processes', return_value=[(p.pid, str(int(token)-1))]), \
             patch.object(self.approver.signal, 'pidfd_send_signal') as send:
            self.assertEqual(self.main('repair'), 0)
            send.assert_not_called()

    def test_relative_script_exact(self):
        self.assert_rejected(self.identity_process(script=self.app/'other.cjs'))
        # A relative path containing another directory is not the root's daemon.
        p = self.relative_daemon()
        read_bytes = Path.read_bytes
        for script in (b'other/muse-daemon.cjs', b'../muse-daemon.cjs', b'muse-daemon.cjs.bak'):
            def args(path):
                return (os.fsencode(self.node.name) + b'\0' + script + b'\0--loop\0') if path == Path(f'/proc/{p.pid}/cmdline') else read_bytes(path)
            with patch.object(Path, 'read_bytes', args):
                self.assertIsNone(self.approver.identity(p.pid, self.app, self.node))

    def test_permission_errors_are_environment(self):
        p = self.identity_process()
        for target, method in ((f'/proc/{p.pid}', 'stat'),
                               (f'/proc/{p.pid}/cmdline', 'read_bytes'),
                               (f'/proc/{p.pid}/stat', 'read_text')):
            original = getattr(Path, method)
            def deny(path, *a, **kw):
                if str(path) == target:
                    raise PermissionError(13, 'sandbox denial', target)
                return original(path, *a, **kw)
            with self.subTest(target=target), patch.object(Path, method, deny):
                with self.assertRaises(PermissionError):
                    self.approver.identity(p.pid, self.app, self.node)
                with self.assertRaises(PermissionError):
                    self.approver.processes(self.app, self.node)
                self.assertEqual(self.main(), 2)
                with patch.object(self.approver.subprocess, 'Popen') as launch, \
                     patch.object(self.approver.signal, 'pidfd_send_signal') as send:
                    self.assertEqual(self.main('repair'), 2)
                    launch.assert_not_called(); send.assert_not_called()
        p.terminate(); p.wait()
        self.assertIsNone(self.approver.identity(p.pid, self.app, self.node))
        self.assertEqual(self.main(), 1)

    def test_preflight_required_fields(self):
        for name, method in (('cmdline', 'read_bytes'), ('stat', 'read_text')):
            original = getattr(Path, method)
            def deny(path, *a, **kw):
                if path == Path(f'/proc/{os.getpid()}/{name}'):
                    raise PermissionError(13, 'preflight denial', str(path))
                return original(path, *a, **kw)
            with self.subTest(name=name), patch.object(Path, method, deny), \
                 patch.object(self.approver, 'processes') as scan:
                self.assertEqual(self.main(), 2)
                scan.assert_not_called()

    def test_pidfile_log_and_signal_permissions(self):
        p = self.relative_daemon()
        original = Path.open
        for target in (self.pidfile, self.log, Path('/proc/stat')):
            def deny(path, *a, **kw):
                if path == target:
                    raise PermissionError(13, 'sandbox denial', str(path))
                return original(path, *a, **kw)
            with self.subTest(target=target), patch.object(Path, 'open', deny):
                self.assertEqual(self.main(), 2)
        self.write_log(age=1000)
        for obj, name in ((self.approver.os, 'pidfd_open'), (self.approver.signal, 'pidfd_send_signal')):
            with patch.object(obj, name, side_effect=PermissionError(1, 'signal denied')):
                self.assertEqual(self.main('repair'), 2)
        self.assertIsNone(p.poll())

    def test_entrypoint_environment_mapping(self):
        p = self.identity_process()
        injector = self.root / 'deny.py'
        injector.write_text('''import runpy, sys
from pathlib import Path
read = Path.read_bytes
def denied(path):
    if str(path) == %r:
        raise PermissionError(13, 'sandbox denial', str(path))
    return read(path)
Path.read_bytes = denied
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
''' % f'/proc/{p.pid}/cmdline')
        command = [sys.executable, str(injector), str(ROOT/'adapters/approver.py'), 'check']
        result = subprocess.run(command, env=dict(os.environ, **self.rows['approver']['environment']),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('environment:', result.stderr)
        row = self.rows['approver']
        row['health_check'] = shlex.join(command)
        out = self.invoke([row], rc=20)
        self.assertEqual(out['components'], [dict(id='approver', status='FAILED', reason='environment')])
        row['health_check'] = 'exit 1'
        self.scan('approver', 'FAILED')


NAMES = [name for name in Tests.__dict__ if name.startswith('test_')]


def load_tests(loader, tests, pattern):
    # Reuse the fixture, without silently counting the inherited M4 tests twice.
    return unittest.TestSuite(Tests(name) for name in NAMES)


def mutations():
    cases = [
        ('relative daemon rejected', 'test_relative_identity_without_cwd', 'adapters/approver.py',
         'relative_identity(pid, root, fields[19])', 'return None'),
        ('relative PID binding bypassed', 'test_relative_pidfile_binding', 'adapters/approver.py',
         'recorded != pid', 'False'),
        ('relative starttime evidence bypassed', 'test_relative_starttime_evidence', 'adapters/approver.py',
         'not started <= modified <= time.time()', 'False'),
        ('relative script constraint bypassed', 'test_relative_script_exact', 'adapters/approver.py',
         "elif script != Path('muse-daemon.cjs'):", 'elif False:'),
        ('identity permission swallowed', 'test_permission_errors_are_environment', 'adapters/approver.py',
         '    except PermissionError:\n        raise\n    except (FileNotFoundError, ProcessLookupError):',
         '    except PermissionError:\n        return None\n    except (FileNotFoundError, ProcessLookupError):'),
        ('environment exit downgraded', 'test_entrypoint_environment_mapping', 'adapters/approver.py',
         '        return 2', '        return 1'),
        ('preflight bypassed', 'test_preflight_required_fields', 'adapters/approver.py',
         '    proc_preflight()', '    pass'),
        ('environment reason downgraded', 'test_entrypoint_environment_mapping', 'bin/health.sh',
         '*:environment|environment) reason=environment ;;', '*:environment|environment) reason=nonzero ;;'),
    ]
    with tempfile.TemporaryDirectory(prefix='m5-mutants-') as td:
        for index, (label, test, rel, old, new) in enumerate(cases):
            root = Path(td)/str(index); shutil.copytree(ROOT, root)
            path = root/rel; source = path.read_text()
            assert source.count(old) == 1, (label, 'mutation must match exactly once')
            path.write_text(source.replace(old, new, 1))
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'Tests.'+test],
                               env=dict(os.environ, M4_IMPL=str(root)), capture_output=True, text=True, timeout=90)
            output = p.stdout + p.stderr
            print('MUTATION:', label, flush=True)
            print(output, end='', flush=True)
            if p.returncode == 0 or 'FAIL:' not in output or 'ERROR:' in output:
                raise AssertionError('mutant escaped: '+label)
            print('DETECTED', flush=True)
    print('M5 mutation checks: PASS (copies only)', flush=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--mutations']:
        mutations()
    else:
        unittest.main(verbosity=2)
