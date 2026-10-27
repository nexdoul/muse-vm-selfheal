#!/usr/bin/env python3
"""M6 local reset/recovery contracts; no real systemctl or remote operations."""
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import test_m4

ROOT = test_m4.ROOT
CONTROLLER = 'muse-selfheal-controller.service'


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='m6-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.units = self.root/'units'; self.units.mkdir()
        self.target = self.root/'etc-systemd'
        self.bin = self.root/'bin'; self.bin.mkdir()
        self.calls = self.root/'calls'
        self.config = self.root/'services.json'
        self.rows = [dict(id='managed', unit='managed.service', repair=None, health_check='true'),
                     dict(id='external', unit='external.service', ownership='external', health_check='true')]
        self.save()
        for name in (CONTROLLER, 'managed.service', 'unregistered.service'):
            (self.units/name).write_text('[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n')
        self.mock('systemctl', '''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
case "$*" in
  daemon-reload) exit "${RELOAD_FAIL:-0}";;
  is-enabled*) if [ -f "$ENABLED/$2" ]; then echo enabled; else echo disabled; exit 1; fi;;
  '--no-reload enable '*) [ "${ENABLE_FAIL:-0}" = 0 ] || exit 1; touch "$ENABLED/$3";;
  *) exit 99;;
esac
''')
        enabled = self.root/'enabled'; enabled.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin)+':/usr/bin:/bin', CALLS=str(self.calls), ENABLED=str(enabled))

    def mock(self, name, text):
        path = self.bin/name; path.write_text(text); path.chmod(0o755)

    def save(self):
        self.config.write_text(json.dumps(dict(version=1, services=self.rows)))

    def invoke(self, *args, rc=0):
        p = subprocess.run([str(ROOT/'bin/bootstrap.sh'), '--services', str(self.config),
                            '--unit-dir', str(self.units), '--system-unit-dir', str(self.target), *args],
                           env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, rc, (p.stdout,p.stderr))
        return p.stdout

    def test_idempotence(self):
        self.invoke()
        paths = [self.target/name for name in (CONTROLLER, 'managed.service')]
        # Force an old mtime: even an identical rewrite must fail this assertion.
        for p in paths: os.utime(p, ns=(1_000_000_000, 1_000_000_000))
        before = [(p.stat().st_ino,p.stat().st_mtime_ns) for p in paths]
        calls = self.calls.read_text()
        out = self.invoke()
        self.assertEqual(before, [(p.stat().st_ino,p.stat().st_mtime_ns) for p in paths])
        self.assertEqual(out.count(': unchanged'), 2)
        after = self.calls.read_text()[len(calls):]
        self.assertNotIn('daemon-reload', after)
        self.assertNotIn(' enable ', after)
        self.assertEqual(self.calls.read_text().count('daemon-reload'),1)
        self.assertEqual([p.stat().st_mode & 0o777 for p in paths], [0o644,0o644])

    def test_dry_run(self):
        self.invoke('--dry-run')
        self.assertFalse(self.target.exists())
        self.assertFalse(self.calls.exists())

    def test_allowlist_external_and_legacy(self):
        self.rows.append(dict(id='legacy', repair=None, environment={'UNIT':'legacy.service'}, health_check='true'))
        (self.units/'legacy.service').write_text('[Service]\nExecStart=/bin/true\n')
        self.save()
        out = self.invoke()
        self.assertIn('external.service: skipped(external)',out)
        self.assertFalse((self.target/'external.service').exists())
        self.assertFalse((self.target/'unregistered.service').exists())
        self.assertTrue((self.target/'legacy.service').exists())
        self.assertNotIn('external.service',self.calls.read_text())

    def test_missing_template(self):
        (self.units/'managed.service').unlink()
        self.assertIn('managed.service: error:',self.invoke(rc=1))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.calls.exists())

    def test_reload_retry_and_enable_failure(self):
        self.env['RELOAD_FAIL']='1'
        self.assertIn('error:',self.invoke(rc=1))
        self.assertTrue((self.target/'.selfheal-bootstrap-reload-needed').exists())
        self.env['RELOAD_FAIL']='0'; self.env['ENABLE_FAIL']='1'
        self.assertIn('error:',self.invoke(rc=1))
        self.assertFalse((self.target/'.selfheal-bootstrap-reload-needed').exists())
        self.env['ENABLE_FAIL']='0'; self.invoke()
        self.assertEqual(self.calls.read_text().count('daemon-reload'),2)

    def test_changed_template_and_reset(self):
        self.invoke()
        (self.units/'managed.service').write_text('[Service]\nExecStart=/bin/false\n')
        out=self.invoke()
        self.assertIn('managed.service: installed',out)
        self.assertEqual((self.target/'managed.service').read_bytes(),(self.units/'managed.service').read_bytes())
        shutil.rmtree(self.target)
        self.invoke()
        self.assertTrue((self.target/CONTROLLER).exists())

    def test_invalid_registration(self):
        for unit in ('../escape.service','<UNIT>','x.service\nother.service'):
            self.rows[0]['unit']=unit; self.save(); self.invoke(rc=2)
        self.assertFalse(self.target.exists())
        self.rows[0]['unit']=CONTROLLER; self.rows[0]['ownership']='external'
        self.save(); self.invoke(rc=2)

    def test_controller_startup_order(self):
        releases=self.root/'releases'; releases.mkdir()
        release=releases/'r1'; bindir=release/'impl/bin'; bindir.mkdir(parents=True)
        (releases/'current').symlink_to(release)
        for name,body in (
            ('bootstrap.sh','echo bootstrap >> "$CALLS"; exit "${BOOT_RC:-0}"'),
            ('reconcile.sh','echo reconcile >> "$CALLS"')):
            p=bindir/name; p.write_text('#!/bin/bash\n'+body+'\n'); p.chmod(0o755)
        env=dict(self.env,RELEASES=str(releases),SELFHEAL=str(self.root/'selfheal'),INTERVAL='60',SITE='synthetic')
        p=subprocess.run(['bash',str(ROOT/'bin/controller-loop.sh')],env=dict(env,BOOT_RC='1'),timeout=5)
        self.assertEqual(p.returncode,2)
        self.assertEqual(self.calls.read_text().splitlines(),['bootstrap'])
        self.calls.unlink()
        p=subprocess.Popen(['bash',str(ROOT/'bin/controller-loop.sh')],env=env,start_new_session=True)
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if self.calls.exists() and 'reconcile' in self.calls.read_text(): break
                time.sleep(.02)
            self.assertEqual(self.calls.read_text().splitlines(),['bootstrap','reconcile'])
        finally:
            os.killpg(p.pid,signal.SIGTERM); p.wait(timeout=5)

    def test_boot_entry_needs_no_controller_unit(self):
        releases=self.root/'releases'; releases.mkdir()
        release=releases/'r1'; bindir=release/'impl/bin'; bindir.mkdir(parents=True)
        (releases/'current').symlink_to(release)
        bootstrap=bindir/'bootstrap.sh'
        bootstrap.write_text('#!/bin/bash\necho bootstrap >> "$CALLS"\nexit "${BOOT_RC:-0}"\n')
        bootstrap.chmod(0o755)
        self.mock('systemctl', '#!/bin/bash\necho "$*" >> "$CALLS"\n')
        env=dict(self.env,RELEASES=str(releases),SELFHEAL=str(self.root/'selfheal'))
        for fail in ('1','0'):
            self.calls.unlink(missing_ok=True)
            p=subprocess.run(['bash',str(ROOT/'bin/boot-ensure.sh')],env=dict(env,BOOT_RC=fail),timeout=5)
            self.assertEqual(p.returncode,int(fail))
            self.assertEqual(self.calls.read_text().splitlines(),['bootstrap'] if fail=='1' else
                             ['bootstrap','--no-block start '+CONTROLLER])


class RecoveryTests(test_m4.Tests):
    def test_missing_unit_reason(self):
        row=self.rows['gemini-keepalive']
        self.unit(load='not-found',active=False)
        for accounts in ([],[{}]):
            self.accounts.write_text(json.dumps({'accounts':accounts}))
            out=self.invoke([row],rc=20)
            self.assertEqual(out['components'][0]['reason'],'missing_unit')
        self.unit(load='loaded',active=False)
        self.assertEqual(self.invoke([row],rc=20)['components'][0]['reason'],'nonzero')
        self.unit(load='not-found',active=False,query_error=True)
        self.assertEqual(self.invoke([row],rc=20)['components'][0]['reason'],'nonzero')

    def test_keepalive_installs_and_converges(self):
        row=self.rows['gemini-keepalive']
        root=Path(row['environment']['KEEPALIVE_ROOT']); root.mkdir()
        installer=root/'install-keepalive.sh'
        installer.write_text('''#!/bin/bash
[ "$#" = 0 ] || exit 9
echo install >> "$CALLS"
[ "${INSTALL_FAIL:-0}" = 0 ] || exit 1
python3 -c 'import json,os; p=os.environ["DB"]; s=json.load(open(p)); s["load"]="loaded"; open(p,"w").write(json.dumps(s))'
''')
        # M4 fake supports start; add stop for the new empty-account repair path.
        ctl=self.bin/'systemctl'
        ctl.write_text(ctl.read_text().replace("elif a[0]=='start':", "elif a[0]=='stop': s.update(active=False,result='success',code=0); p.write_text(json.dumps(s))\nelif a[0]=='start':"))
        for accounts,status,action in (([], 'STOPPED','stop'),([{}],'READY','start')):
            self.accounts.write_text(json.dumps({'accounts':accounts}))
            self.unit(load='not-found',active=False,result='start-limit-hit')
            out=self.invoke([row],mode='reconcile')
            self.assertEqual(out['components'][0]['status'],status)
            calls=self.calls.read_text()
            self.assertIn('install\n',calls)
            self.assertIn('systemctl '+action+' -- synthetic-keep.service',calls)
            before=calls
            self.invoke([row],mode='reconcile')
            self.assertNotIn('install\n',self.calls.read_text()[len(before):])
        self.unit(load='not-found',active=False)
        row['environment']['INSTALL_FAIL']='1'
        out=self.invoke([row],mode='reconcile',rc=20)
        self.assertEqual(out['components'][0]['reason'],'missing_unit')

    def test_external_and_null_still_observe_only(self):
        # Observe-only rows must never probe systemd to classify a failure:
        # they cannot repair anything, so LoadState is irrelevant to them.
        # The reason therefore stays the ordinary nonzero, and no start is run.
        row=self.rows['gemini-keepalive']
        self.unit(load='not-found',active=False)
        for patch in ({'ownership':'external'},{'repair':None}):
            s=copy.deepcopy(row); s.update(patch)
            out=self.invoke([s],mode='reconcile',rc=20)
            self.assertEqual(out['components'][0]['reason'],'nonzero')
        self.assertNotIn('systemctl start',self.calls.read_text())

    def test_missing_unit_protocol_preserves_other_failures(self):
        row=self.rows['gemini-keepalive']
        row.pop('desired_state_ref'); row['desired_state']='running'
        for command,reason in (('exit 1','nonzero'),('exit 2','environment'),
                               ('exit 127','nonzero'),
                               ('printf "missing_unit:" >&3; exit 1','missing_unit'),
                               ('printf "missing_unit:" >&3; exit 2','environment')):
            row['health_check']=command
            self.assertEqual(self.invoke([row],rc=20)['components'][0]['reason'],reason)


RECOVERY_NAMES=[n for n in RecoveryTests.__dict__ if n.startswith('test_')]


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([loader.loadTestsFromTestCase(BootstrapTests),
                              unittest.TestSuite(RecoveryTests(n) for n in RECOVERY_NAMES)])


def mutations():
    with tempfile.TemporaryDirectory(prefix='m6-mutant-') as td:
        root=Path(td)/'impl'; shutil.copytree(ROOT,root)
        path=root/'lib/bootstrap.py'; source=path.read_text()
        old='same = path.is_file() and path.read_bytes() == data'
        assert source.count(old)==2
        path.write_text(source.replace(old,'same = False'))
        p=subprocess.run([sys.executable,str(Path(__file__).resolve()),'BootstrapTests.test_idempotence'],
                         env=dict(os.environ,M4_IMPL=str(root)),capture_output=True,text=True,timeout=30)
        output=p.stdout+p.stderr
        print('MUTATION: always rewrite identical unit\n'+output)
        if p.returncode==0 or 'FAIL:' not in output or 'ERROR:' in output:
            raise AssertionError('idempotence mutant escaped')
        print('DETECTED; M6 mutation checks: PASS (temporary copy only)')


if __name__=='__main__':
    if sys.argv[1:2]==['--mutations']: mutations()
    else: unittest.main(verbosity=2)
