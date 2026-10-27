#!/usr/bin/env python3
"""Local reconciliation and migration contracts; mutation checks use copies only."""
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECONCILE = Path(os.environ.get('M3_RECONCILE', ROOT/'bin/reconcile.sh'))


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='m3-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root/'bin'; self.bin.mkdir()
        self.calls = self.root/'calls'
        self.unitstate = self.root/'unit.json'
        self.set_unit('failed', 'start-limit-hit', '1')
        self.mock('systemctl', '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['UNIT_STATE'])
s=json.loads(p.read_text()); args=sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f: f.write(' '.join(args)+'\\n')
if args[0]=='show':
    prop=args[args.index('-p')+1]
    print(s.get(prop, 'loaded'))
elif args[0]=='reset-failed':
    s['Result']='success'; p.write_text(json.dumps(s))
elif args[0] in ('start', 'stop'):
    if s['Result']=='start-limit-hit': sys.exit(1)
    s.update(ActiveState=os.environ.get('AFTER_STATE', 'inactive'),
             Result=os.environ.get('AFTER_RESULT', 'success'),
             ExecMainStatus=os.environ.get('AFTER_CODE', '0'))
    p.write_text(json.dumps(s))
elif args[0]=='is-active': sys.exit(0 if s['ActiveState']=='active' else 3)
else: sys.exit(8)
''')
        self.env = dict(PATH=str(self.bin)+':/usr/bin:/bin', UNIT_STATE=str(self.unitstate), CALLS=str(self.calls))
        self.mock('pgrep', '#!/bin/bash\nexit 1\n')

    def mock(self, name, body):
        p = self.bin/name; p.write_text(body); p.chmod(0o755)
        return p

    def set_unit(self, state, result='success', code='0'):
        self.unitstate.write_text(json.dumps(dict(ActiveState=state, Result=result, ExecMainStatus=code)))

    def service(self, **kw):
        return dict(id='synthetic', workdir=str(self.root), environment=self.env.copy(),
                    unit='synthetic.service', health_check='exit 1', stopped_check='exit 0',
                    settle_sec=0, **kw)

    def invoke(self, rows, *options, rc=0):
        manifest = self.root/'services.json'
        manifest.write_text(json.dumps(dict(version=1, status_protocol=2, services=rows)))
        p = subprocess.run([str(RECONCILE), '--services', str(manifest), '--state-dir', str(self.root/'state'),
                            '--run-dir', str(self.root/'run'), *options], text=True, capture_output=True,
                           env=dict(os.environ, **self.env), timeout=30)
        self.assertEqual(p.returncode, rc, (p.stdout, p.stderr))
        return p

    def test_validate(self):
        self.invoke([self.service()], '--validate')
        self.invoke([self.service(desired_state_ref=dict(file='/not-read-by-validate', pointer='/state'))], '--validate')
        for change in ({'repair': {}}, {'prepare': 'bad'}, {'desired_state': 'bad'}, {'health_check': ''},
                       {'prerequisites': [3]}, {'ownership': 'unknown'}, {'settle_sec': -1},
                       {'desired_state_ref': {'file': 'x', 'pointer': '/bad~2'}},
                       {'desired_state': 'running', 'desired_state_ref': {'file': 'x', 'pointer': ''}}):
            s = self.service(); s.update(change)
            with self.subTest(change=change): self.invoke([s], '--validate', rc=2)
        self.invoke([self.service(), self.service()], '--validate', rc=2)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root/'state').exists())

    def test_plan(self):
        marker = self.root/'executed'
        s = self.service(prepare=[f'touch {marker}'], repair=[f'touch {marker}'])
        s['health_check'] = f'touch {marker}; systemctl start synthetic.service'
        stop = self.service(desired_state='stopped'); stop['id']='stop'
        out = json.loads(self.invoke([s, stop], '--plan', '--json').stdout)
        self.assertEqual([x['id'] for x in out], ['synthetic', 'stop'])
        self.assertEqual(out[0]['prepare'], s['prepare'])
        self.assertEqual(out[0]['repair'], {'commands': s['repair']})
        self.assertEqual(out[1]['repair']['action'], 'stop')
        self.assertEqual(out[1]['repair']['unit'], 'synthetic.service')
        self.assertFalse(self.calls.exists())
        self.assertFalse(marker.exists())
        self.assertFalse((self.root/'run').exists())

    def test_repair_null(self):
        marker = self.root/'prepared'
        s = self.service(repair=None, prepare=[f'touch {marker}'])
        self.assertEqual(json.loads(self.invoke([s], '--plan').stdout), [])
        self.invoke([s], rc=20)
        self.assertFalse(self.calls.exists(), 'repair:null must never call systemctl')
        self.assertFalse(marker.exists())

    def test_external(self):
        marker = self.root/'repaired'
        for repair in (None, f'touch {marker}', [f'touch {marker}']):
            s = self.service(ownership='external', repair=repair, prepare=[f'touch {marker}'])
            self.assertEqual(json.loads(self.invoke([s], '--plan').stdout), [])
            self.invoke([s], rc=20)
            self.assertFalse(marker.exists())
        s = self.service(ownership='external')
        self.invoke([s], rc=20)
        self.assertFalse(self.calls.exists())

    def test_precedence(self):
        # Repair has a side effect but returns failure. Final health is only WAITING;
        # therefore this tests reconcile's own severity fold, not health's fold.
        marker = self.root/'ready'
        failed = self.service(repair=f'touch {marker}; exit 1')
        failed['health_check'] = f'test -f {marker}'
        waiting = self.service(prerequisites=['exit 1']); waiting['id']='waiting'
        for rows in ([failed, waiting], [waiting, failed]):
            marker.unlink(missing_ok=True)
            self.invoke(rows, rc=20)
            self.assertTrue(marker.exists())
            final = json.loads((self.root/'state/status.json').read_text())
            self.assertEqual(final['status'], 'WAITING')
        # Also exercise persistent FAILED + WAITING in both orders.
        failed['repair']=None; failed['health_check']='exit 1'
        for rows in ([failed, waiting], [waiting, failed]): self.invoke(rows, rc=20)

    def unit_service(self):
        s = self.service()
        s['health_check'] = '''state=$(systemctl show -p ActiveState --value synthetic.service) || exit 1
result=$(systemctl show -p Result --value synthetic.service) || exit 1
code=$(systemctl show -p ExecMainStatus --value synthetic.service) || exit 1
[[ $state == active || ( $state == inactive && $result == success && $code == 0 ) ]]'''
        return s

    def test_reset_and_terminal(self):
        for state, result, code, rc in [('inactive','success','0',0), ('active','success','0',0),
                                      ('failed','success','0',20), ('inactive','exit-code','1',20),
                                      ('inactive','success','1',20)]:
            with self.subTest(state=state, result=result, code=code):
                self.set_unit('failed','start-limit-hit','1')
                self.calls.unlink(missing_ok=True)
                s=self.unit_service(); s['environment'].update(AFTER_STATE=state, AFTER_RESULT=result, AFTER_CODE=code)
                self.invoke([s], '--json', rc=rc)
                calls=self.calls.read_text().splitlines()
                reset=calls.index('reset-failed -- synthetic.service')
                start=calls.index('start -- synthetic.service')
                self.assertLess(reset, start)
                self.assertTrue(any('ActiveState' in c for c in calls[start+1:]), 'must recheck after start')
                self.assertEqual(calls.count('start -- synthetic.service'), 1)

    def test_terminal_guard_independent_of_health(self):
        marker=self.root/'started'
        # Health goes green after any start, so only the unit terminal guard can
        # reject failed/dirty exits. This avoids a false-positive terminal test.
        for state, result, code, rc in [('inactive','success','0',0), ('active','success','0',0),
                                      ('failed','success','0',20), ('inactive','success','9',20)]:
            self.set_unit('failed','start-limit-hit','1')
            s=self.service(); s['environment'].update(AFTER_STATE=state, AFTER_RESULT=result, AFTER_CODE=code)
            s['health_check']='! grep -q start-limit-hit "$UNIT_STATE"'
            self.invoke([s], rc=rc)

    def test_prepare_custom_repair_and_recheck(self):
        marker=self.root/'order'
        for repair in (f'echo repair >> {marker}', [f'echo repair >> {marker}']):
            marker.unlink(missing_ok=True)
            s=self.service(prepare=[f'echo prepare >> {marker}'], repair=repair)
            s['health_check']=f'test -f {marker} && grep -q repair {marker}'
            self.invoke([s])
            self.assertEqual(marker.read_text().splitlines(), ['prepare','repair'])
            self.assertFalse(self.calls.exists())
        # A command returning zero is not sufficient evidence of repair success.
        self.invoke([self.service(repair='exit 0')], rc=20)
        marker.unlink()
        self.invoke([self.service(prepare=['exit 1'], repair=f'touch {marker}')], rc=20)
        self.assertFalse(marker.exists())

    def test_reference_stop_and_lock(self):
        config=self.root/'target.json'; config.write_text('{"accounts":[]}')
        s=self.service(desired_state_ref=dict(file=str(config),pointer='/accounts',when_empty='stopped',when_nonempty='running'),
                       prepare=['exit 1'], prerequisites=['exit 1'])
        s['stopped_check']='[[ $(systemctl show -p ActiveState --value synthetic.service) == inactive ]]'
        self.set_unit('active')
        self.invoke([s])
        calls=self.calls.read_text(); self.assertIn('stop -- synthetic.service', calls); self.assertNotIn('start --', calls)
        config.write_text('{}'); self.calls.unlink()
        self.invoke([s], rc=2); self.assertFalse(self.calls.exists())
        run=self.root/'run'; run.mkdir(exist_ok=True)
        with open(run/'reconcile.lock','w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.invoke([self.service()], rc=10)
        self.assertFalse(self.calls.exists())

    def test_static_and_template(self):
        code='\n'.join(line for line in RECONCILE.read_text().splitlines() if not line.lstrip().startswith('#'))
        self.assertIsNone(re.search(r'approver|local-sshd|douyin|tunnel-',code))
        d=json.loads((ROOT/'config/services.muse02.reconcile.json').read_text())
        self.assertEqual(len(d['services']),10)
        self.assertTrue(all(s.get('repair','missing') is None for s in d['services'] if s['kind']=='ssh-reverse-tunnel'))
        approval=next(s for s in d['services'] if s['id']=='approver')
        self.assertEqual(approval['desired_state_ref']['pointer'],'/desired_state')
        self.assertNotIn('desired_state',approval)
        self.assertTrue(next(s for s in d['services'] if s['id']=='local-sshd')['prepare'])
        self.invoke(d['services'],'--validate')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='m3-migration-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.control=self.root/'control'
        self.bin=self.root/'bin'; self.bin.mkdir()
        self.db=self.root/'units.json'
        self.db.write_text(json.dumps({'fake-old.service': {'active': True, 'enabled': True}}))
        self.calls=self.root/'calls'
        systemctl=self.bin/'systemctl'
        systemctl.write_text('''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['MOCK_DB']); d=json.loads(p.read_text()); args=sys.argv[1:]
with open(os.environ['MOCK_CALLS'],'a') as f: f.write(' '.join(args)+'\\n')
verb=args[0]
if verb=='daemon-reload': sys.exit(0)
unit=args[-1]; s=d.setdefault(unit,dict(active=False, enabled=False))
if verb=='is-active':
    if '--quiet' not in args: print('active' if s['active'] else 'inactive')
    sys.exit(0 if s['active'] else 3)
if verb=='is-enabled':
    print('enabled' if s['enabled'] else 'disabled'); sys.exit(0 if s['enabled'] else 1)
if verb in ('start','stop'): s['active']=verb=='start'
if verb in ('enable','disable'):
    s['enabled']=verb=='enable'
    if '--now' in args: s['active']=True
p.write_text(json.dumps(d))
''')
        systemctl.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.bin)+':/usr/bin:/bin',MOCK_DB=str(self.db),MOCK_CALLS=str(self.calls))
        self.hooks=[self.root/'old-hook',self.root/'old-bootstrap']
        for h in self.hooks: h.write_text('old entry\n'); h.chmod(0o700)
        self.release=self.root/'release'
        for d in ('bin','lib','config','adapters','units','hooks'):
            (self.release/d).mkdir(parents=True)
        self.manifest=self.release/'config/services.json'
        self.manifest.write_text('{"version":1,"services":[{"id":"synthetic"}]}')
        (self.release/'lib/reconcile-manifest.py').write_text('# fake validator\n')
        (self.release/'units/fake-new.service').write_text('[Service]\nExecStart=/fake\n')
        self.executable('bin/reconcile.sh','#!/bin/bash\nexit 0\n')
        self.executable('hooks/bootstrap','#!/bin/bash\nexit 0\n')
        self.health('{"status":"READY","components":[{"id":"synthetic"}]}')
        self.unit_link=self.root/'fake-new.service'
        self.hook_link=self.root/'new-hook'
        self.control.mkdir()
        (self.control/'current').symlink_to(self.root/'previous-release')

    def executable(self,name,body):
        p=self.release/name; p.write_text(body); p.chmod(0o755)

    def health(self, data, rc=0):
        self.executable('bin/health.sh', "#!/bin/bash\nprintf '%s\\n' '"+data+"'\nexit "+str(rc)+'\n')

    def run_script(self, operation, *extra, rc=0):
        p=subprocess.run([str(ROOT/'migration'/f'{operation}.sh'),'--root',str(self.control),*map(str,extra)],
                         text=True,capture_output=True,env=self.env,timeout=10)
        self.assertEqual(p.returncode,rc,(p.stdout,p.stderr))
        return p

    def quiesce(self):
        self.run_script('quiesce-old','--unit','fake-old.service','--hook',self.hooks[0],'--hook',self.hooks[1])

    def enable(self, rc=0):
        self.run_script('enable-new','--unit','fake-new.service','--release',self.release,
                        '--services',self.manifest,'--unit-link',self.unit_link,'--hook-link',self.hook_link,rc=rc)

    def assert_restored(self):
        self.assertEqual(os.readlink(self.control/'current'),str(self.root/'previous-release'))
        self.assertFalse(os.path.lexists(self.unit_link)); self.assertFalse(os.path.lexists(self.hook_link))
        for h in self.hooks:
            self.assertEqual(h.read_text(),'old entry\n'); self.assertEqual(h.stat().st_mode & 0o777,0o700)
        states=json.loads(self.db.read_text())
        self.assertEqual(states['fake-old.service'],dict(active=True,enabled=True))
        self.assertEqual(states['fake-new.service'],dict(active=False,enabled=False))

    def test_switch_and_rollback(self):
        self.quiesce(); self.quiesce()
        self.assertTrue(all(not h.exists() for h in self.hooks))
        self.assertEqual(json.loads(self.db.read_text())['fake-old.service'],dict(active=False,enabled=False))
        self.enable()
        self.assertEqual((self.control/'current').resolve(),self.release)
        self.assertEqual(self.unit_link.resolve(),self.release/'units/fake-new.service')
        self.assertEqual(self.hook_link.resolve(),self.release/'hooks/bootstrap')
        self.run_script('rollback'); self.assert_restored()
        self.run_script('rollback'); self.assert_restored()

    def test_automatic_rollback(self):
        for data,rc in [('{"status":"READY","components":[]}',0),
                        ('{"status":"READY","components":[{"id":"synthetic"},{"id":"synthetic"}]}',0),
                        ('{"status":"WAITING","components":[{"id":"synthetic"}]}',10),
                        ('invalid json',0)]:
            with self.subTest(data=data):
                self.quiesce(); self.health(data,rc); self.enable(rc=2); self.assert_restored()

    def test_preflight_failure_restores_old(self):
        self.quiesce()
        (self.release/'adapters').rmdir()
        self.enable(rc=2)
        self.assertTrue(all(h.exists() for h in self.hooks))
        self.assertEqual(json.loads(self.db.read_text())['fake-old.service'],dict(active=True,enabled=True))
        self.assertEqual(os.readlink(self.control/'current'),str(self.root/'previous-release'))

    def test_dry_run(self):
        for op in ('quiesce-old','enable-new','rollback'):
            p=self.run_script(op,'--dry-run')
            self.assertIn('no writes or commands executed',p.stdout)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.control/'old-state.json').exists())


def mutations():
    source=RECONCILE.read_text()
    cases=[('remove repair:null execution guard','test_repair_null',
            source.replace('.ownership == "external" or (has("repair") and .repair == null)', '.ownership == "external"')),
           ('WAITING overwrites FAILED','test_precedence',
            source.replace('[[ $overall == 20 ]] || overall=10', 'overall=10')),
           ('remove terminal state guard','test_terminal_guard_independent_of_health',
            source.replace('command_probe "$terminal" || ok=false', ': # faulty terminal success'))]
    with tempfile.TemporaryDirectory(prefix='m3-mutants-') as td:
        release=Path(td)/'impl'; shutil.copytree(ROOT/'bin',release/'bin'); shutil.copytree(ROOT/'lib',release/'lib')
        path=release/'bin/reconcile.sh'
        for label,test,mutant in cases:
            assert mutant!=source, 'mutation did not apply'
            path.write_text(mutant)
            p=subprocess.run([sys.executable,str(Path(__file__).resolve()),'Tests.'+test],
                             text=True,capture_output=True,env=dict(os.environ,M3_RECONCILE=str(path)),timeout=60)
            print('MUTATION:',label,flush=True)
            print(p.stdout+p.stderr,end='',flush=True)
            assert p.returncode!=0 and 'FAIL:' in p.stderr, 'mutation escaped assertions'
            print(f'DETECTED (exit={p.returncode})',flush=True)
    print('Mutation checks: PASS; repository sources unchanged',flush=True)


if __name__=='__main__':
    if sys.argv[1:]==['--mutations']: mutations()
    else: unittest.main(verbosity=2)
