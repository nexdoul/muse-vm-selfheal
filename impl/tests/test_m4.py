#!/usr/bin/env python3
"""M4 local contracts. Mutations copy the release; no remote commands/services."""
import copy
import json
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('M4_IMPL', Path(__file__).resolve().parents[1]))
TEMPLATE = ROOT / 'config/services.muse01.json'


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='m4-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.app = self.root / 'app with spaces'; self.app.mkdir()
        for sub in ('data', 'log'): (self.app / sub).mkdir()
        self.calls = self.root / 'calls'
        self.db = self.root / 'unit.json'
        self.db.write_text(json.dumps(dict(load='loaded', active=True, result='success')))
        self.secret = self.root / 'secret'; self.secret.write_text('synthetic'); self.secret.chmod(0o600)
        self.probe_bin = self.root / 'probe'; self.probe_bin.write_text('fake'); self.probe_bin.chmod(0o700)
        self.ssh = self.root / 'ssh_config'
        # Synthetic aliases prove the adapter consumes the supplied list.
        self.aliases = 'relay_a relay_b relay_c relay_d relay_e'
        self.ssh.write_text(''.join(f'Host {a}\n' for a in self.aliases.split()))
        self.accounts = self.root / 'accounts.json'; self.accounts.write_text('{"accounts":[{}]}')
        self.site = self.root / 'site.json'; self.site.write_text('{"version":1}')
        self.keep_log = self.root / 'keep.log'; self.keep_log.write_text('ok')
        self.stop = self.app / 'data/muse-daemon.stop'
        self.log = self.app / 'log/daemon-log.ndjson'
        self.pidfile = self.app / 'data/muse-daemon.pid'
        self.script = self.app / 'muse-daemon.cjs'
        self.script.write_text('''import os, time
from pathlib import Path
Path('data/muse-daemon.pid').write_text(str(os.getpid()))
Path('log/daemon-log.ndjson').write_text('{"ts":%d}' % (time.time()*1000))
time.sleep(120)
''')
        self.mock('systemctl', '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['DB']); s=json.loads(p.read_text()); a=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f: f.write('systemctl '+ ' '.join(a)+'\\n')
if s.get('query_error'): sys.exit(1)
if a[0]=='is-active': sys.exit(0 if s['active'] else 3)
if a[0]=='show':
    prop=a[a.index('-p')+1] if '-p' in a else next(x.split('=',1)[1] for x in a if x.startswith('--property='))
    print({'LoadState':s['load'],'Result':s['result'],'ActiveState':'active' if s['active'] else 'inactive','ExecMainStatus':str(s.get('code',0))}[prop])
elif a[0]=='reset-failed': s['result']='success'; p.write_text(json.dumps(s))
elif a[0]=='start':
    if s['result']=='start-limit-hit': sys.exit(1)
    s['active']=not s.get('stay_failed',False); p.write_text(json.dumps(s))
else: sys.exit(8)
''')
        self.mock('pgrep', '#!/bin/bash\nexit "${EXTERNAL_RC:-0}"\n')
        self.mock('ss', '#!/bin/bash\n[ "${EXTERNAL_RC:-0}" = 0 ] || exit 1\necho "LISTEN 0 128 127.0.0.1:22 0.0.0.0:*"\n')
        self.mock('findmnt', '#!/bin/bash\necho synthetic-device\n')
        self.installer = self.mock('install-probe', '''#!/bin/bash
[ "$#" = 0 ] || exit 9
printf 'install\\n' >> "$CALLS"
[ "${INSTALL_FAIL:-0}" = 0 ] || exit 1
python3 -c 'import json,os; p=os.environ["DB"]; s=json.load(open(p)); s.update(load="loaded",active=True); open(p,"w").write(json.dumps(s))'
''')
        self.alias_helper = self.mock('restore-aliases', '''#!/bin/bash
printf 'aliases:%s\\n' "$ALIASES" >> "$CALLS"
[ "${HELPER_FAIL:-0}" = 0 ] || exit 1
[ "${HELPER_NOOP:-0}" = 0 ] || exit 0
: > "$SSH_CONFIG"
for alias in $ALIASES; do printf 'Host %s\\n' "$alias" >> "$SSH_CONFIG"; done
''')
        self.node = self.mock('node', '''#!/usr/bin/python3
import os, sys
with open(os.environ['CALLS'],'a') as f: f.write('node '+ ' '.join(sys.argv[1:])+'\\n')
if '--check' in sys.argv: sys.exit(int(os.environ.get('PRECHECK_RC','0')))
os.execv('/usr/bin/python3', ['node', *sys.argv[1:]])
''')
        self.mock('ssh', '#!/bin/bash\necho forbidden-ssh >> "$CALLS"\nexit 99\n')
        self.env = dict(PATH=str(self.bin)+':/usr/bin:/bin', DB=str(self.db), CALLS=str(self.calls))
        self.manifest = json.loads(TEMPLATE.read_text())
        values = {
            'SERVICE_HOME': str(self.root), 'VOLUME_PATH': str(self.root), 'VOLUME_SOURCE_PATTERN':'synthetic-device',
            'SITE_CONFIG':str(self.site), 'SERVICES_CONFIG':str(self.site), 'CF_PROBE_UNIT':'synthetic.service',
            'CF_PROBE_BIN':str(self.probe_bin), 'CF_PROBE_SECRET_FILE':str(self.secret),
            'CF_PROBE_INSTALL_SCRIPT':str(self.installer), 'APPROVER_ROOT':str(self.app),
            'ADAPTER_DIR':str(ROOT/'adapters'), 'NODE_BIN':str(self.node), 'SSH_CONFIG':str(self.ssh),
            'SSH_ALIASES':self.aliases, 'ALIAS_REPAIR_HELPER':str(self.alias_helper),
            'KEEPALIVE_CONFIG':str(self.accounts), 'KEEPALIVE_UNIT':'synthetic-keep.service',
            'KEEPALIVE_LOG':str(self.keep_log), 'TUNNEL_KRVPS_HOST':'synthetic-a.invalid',
            'TUNNEL_USVPS_HOST':'synthetic-b.invalid', 'TUNNEL_KRVPS_PORT':'12345', 'TUNNEL_USVPS_PORT':'23456',
            'SSHD_LISTEN_ADDRESS':'127.0.0.1:22', 'HERMES_PROCESS_PATTERN':'synthetic-worker'
        }
        def render(x):
            if isinstance(x,str): return re.sub(r'<([A-Z_]+)>', lambda m:values[m[1]], x)
            if isinstance(x,list): return [render(v) for v in x]
            if isinstance(x,dict): return {k:render(v) for k,v in x.items()}
            return x
        self.manifest = render(self.manifest)
        self.rows = {s['id']:s for s in self.manifest['services']}
        for s in self.rows.values():
            s.setdefault('environment',{}).update(self.env)
            s['settle_sec']=0.1
        spec = importlib.util.spec_from_file_location('approver_under_test', ROOT/'adapters/approver.py')
        self.approver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.approver)
        self.children=[]
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for p in self.children:
            if p.poll() is None: p.terminate()
            p.wait(timeout=5)
        # Repairs start only our synthetic daemon; the file is inside this fixture.
        if self.pidfile.exists():
            try:
                pid=int(self.pidfile.read_text())
                args=Path(f'/proc/{pid}/cmdline').read_bytes()
                cwd=Path(f'/proc/{pid}/cwd').resolve()
                if cwd==self.app and b'muse-daemon.cjs' in args: os.kill(pid,15)
            except (ValueError,ProcessLookupError,FileNotFoundError): pass

    def mock(self,name,body):
        p=self.bin/name; p.write_text(body); p.chmod(0o755); return p

    def unit(self,**change):
        s=json.loads(self.db.read_text()); s.update(change); self.db.write_text(json.dumps(s))

    def invoke(self,rows=None,mode='health',options=(),rc=0,path=None):
        config=self.root/'services.json'
        if path is None:
            config.write_text(json.dumps(dict(version=1,status_protocol=2,services=rows or list(self.rows.values()))))
        else: config=path
        p=subprocess.run([str(ROOT/'bin'/f'{mode}.sh'),'--services',str(config),
                          '--state-dir',str(self.root/'state'),'--run-dir',str(self.root/'run'),
                          '--json',*options],capture_output=True,text=True,timeout=45,
                         env=dict(os.environ,**self.env))
        self.assertEqual(p.returncode,rc,(p.stdout,p.stderr))
        return json.loads(p.stdout) if p.stdout.strip() else None

    def scan(self,id,status):
        out=self.invoke([self.rows[id]],rc=20 if status=='FAILED' else 0)
        self.assertEqual(out['components'],[dict(id=id,status=status,reason='nonzero' if status=='FAILED' else status.lower())])

    def write_log(self, age=0):
        self.log.write_text(json.dumps({'ts': int((time.time()-age)*1000)}) + '\n')

    def main(self, mode='check'):
        with patch.dict(os.environ, self.rows['approver']['environment']), \
             patch.object(sys, 'argv', ['approver.py', mode]):
            return self.approver.main()

    def identity_process(self, argv0='node', cwd=None, script=None, args=()):
        # A real /proc entry; argv[0] deliberately matches the configured node
        # basename. No dependency on a system Node installation is needed.
        script = script or self.script
        script.write_text('import time; time.sleep(120)')
        p = subprocess.Popen([argv0, str(script), *args], executable='/usr/bin/python3',
                             cwd=cwd or self.app)
        self.children.append(p)
        self.write_log()
        self.assertIsNone(p.poll())
        return p

    def assert_rejected(self, p):
        self.assertIsNone(self.approver.identity(p.pid, self.app, self.node))
        self.assertEqual(self.main(), 1)
        self.adapter('approver-check', rc=1)
        self.scan('approver', 'FAILED')

    def daemon(self,other=False):
        script=self.script
        if other:
            alt=self.root/'different-app'; alt.mkdir(); script=alt/'muse-daemon.cjs'
            script.write_text('import time; time.sleep(120)')
        p=subprocess.Popen(['node',str(script)],executable='/usr/bin/python3',cwd=self.app)
        self.children.append(p)
        if not other:
            deadline=time.monotonic()+3
            while (not self.pidfile.exists() or not self.log.exists()) and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue(self.pidfile.exists())
            self.assertTrue(self.log.exists())
        return p

    def adapter(self,name,rc=0):
        row=self.rows['approver' if name.startswith('approver') else 'ssh-alias' if name.startswith('ssh') else 'cf-probe']
        p=subprocess.run(['bash',str(ROOT/'adapters'/f'{name}.sh')],env=dict(os.environ,**row['environment']),
                         capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,rc,(p.stdout,p.stderr))

    def test_validate_plan(self):
        self.invoke(mode='reconcile',options=('--validate',),path=TEMPLATE)
        plan=self.invoke(mode='reconcile',options=('--plan',))
        self.assertEqual([s['id'] for s in plan],['cf-probe','approver','ssh-alias'])
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root/'state').exists())
        self.assertFalse((self.root/'run').exists())
        self.assertEqual(len(self.rows),10)
        self.assertEqual({k for k,s in self.rows.items() if s['ownership']=='external'},
                         {'tunnel-krvps','tunnel-usvps','sshd','hermes'})

    def test_complete_manifest(self):
        self.stop.touch()
        out=self.invoke()
        self.assertEqual([c['id'] for c in out['components']],list(self.rows))
        self.assertEqual([c['status'] for c in out['components']],
                         ['STOPPED' if k=='approver' else 'READY' for k in self.rows])
        self.assertEqual(self.manifest['status_protocol'],2)
        for k in ('volume','config'):
            self.assertEqual(self.rows[k]['ownership'],'internal')
            self.assertIsNone(self.rows[k]['repair'])
        for k in ('cf-probe','approver','ssh-alias','gemini-keepalive'):
            self.assertEqual(self.rows[k]['ownership'],'managed')
        self.assertIsNone(self.rows['gemini-keepalive']['repair'])

    def test_cf_check(self):
        self.scan('cf-probe','READY')
        self.unit(active=False); self.scan('cf-probe','FAILED'); self.unit(active=True)
        self.probe_bin.chmod(0o600); self.scan('cf-probe','FAILED'); self.probe_bin.chmod(0o700)
        self.probe_bin.unlink(); self.scan('cf-probe','FAILED')
        self.probe_bin.write_text('fake'); self.probe_bin.chmod(0o700)
        self.secret.chmod(0o644); self.scan('cf-probe','FAILED')
        self.secret.unlink(); self.scan('cf-probe','FAILED')

    def test_cf_repair_environment_and_recheck(self):
        self.unit(active=False,result='start-limit-hit')
        self.invoke([self.rows['cf-probe']],mode='reconcile')
        calls=self.calls.read_text()
        self.assertIn('start -- synthetic.service',calls)
        self.assertLess(calls.index('reset-failed'),calls.index('start --'))
        self.assertNotIn('install\n',calls)
        self.unit(active=False,stay_failed=True)
        self.invoke([self.rows['cf-probe']],mode='reconcile',rc=20)
        self.unit(active=True); self.secret.chmod(0o644); self.calls.unlink()
        self.invoke([self.rows['cf-probe']],mode='reconcile',rc=20)
        self.assertNotIn('install\n',self.calls.read_text())
        self.assertNotIn('start --',self.calls.read_text())

    def test_cf_missing_unit(self):
        self.unit(load='not-found',active=False)
        self.invoke([self.rows['cf-probe']],mode='reconcile')
        self.assertIn('install\n',self.calls.read_text())
        self.unit(load='not-found',active=False)
        self.rows['cf-probe']['environment']['INSTALL_FAIL']='1'
        self.invoke([self.rows['cf-probe']],mode='reconcile',rc=20)
        self.unit(load='error',active=False); self.calls.unlink()
        self.invoke([self.rows['cf-probe']],mode='reconcile',rc=20)
        self.assertNotIn('install\n',self.calls.read_text())

    def test_alias_check(self):
        self.scan('ssh-alias','READY')
        for missing in self.aliases.split():
            self.ssh.write_text(''.join(f'Host {a}\n' for a in self.aliases.split() if a!=missing))
            self.scan('ssh-alias','FAILED')
        self.ssh.write_text(''.join(f'Host {a}suffix\n' for a in self.aliases.split()))
        self.scan('ssh-alias','FAILED')
        self.rows['ssh-alias']['environment']['ALIASES']='   '
        self.scan('ssh-alias','FAILED')

    def test_alias_repair(self):
        self.ssh.unlink()
        self.invoke([self.rows['ssh-alias']],mode='reconcile')
        self.assertIn('aliases:'+self.aliases,self.calls.read_text())
        self.scan('ssh-alias','READY')
        self.ssh.write_text(''); self.rows['ssh-alias']['environment']['HELPER_NOOP']='1'
        self.invoke([self.rows['ssh-alias']],mode='reconcile',rc=20)
        self.rows['ssh-alias']['environment']['HELPER_FAIL']='1'
        self.invoke([self.rows['ssh-alias']],mode='reconcile',rc=20)

    def test_approver_stop(self):
        self.stop.touch()
        self.adapter('approver-check', rc=1)
        self.scan('approver','STOPPED')
        row = self.rows['approver']
        original = row['stopped_check']
        health = row['health_check']
        row['stopped_check'] = 'exit 1'
        row['health_check'] = 'exit 0'
        self.scan('approver', 'FAILED')
        row['stopped_check'] = original
        row['health_check'] = health
        self.invoke([self.rows['approver']],mode='reconcile')
        self.adapter('approver-repair')
        self.assertFalse(self.calls.exists())
        self.stop.unlink(); self.scan('approver','FAILED')
        self.daemon(); self.stop.touch(); self.scan('approver','STOPPED')
        self.adapter('approver-check', rc=1)

    def test_approver_check(self):
        self.scan('approver','FAILED')
        p=self.daemon(); self.scan('approver','READY')
        self.write_log(age=1000); self.scan('approver','FAILED')
        self.write_log(); self.scan('approver','READY')
        self.write_log(age=-60); self.scan('approver','FAILED')
        self.log.unlink(); self.scan('approver','FAILED')
        self.write_log(); p.terminate(); p.wait(); self.scan('approver','FAILED')

    def test_approver_identity(self):
        foreign=self.daemon(other=True)
        self.pidfile.write_text(str(foreign.pid)); self.write_log()
        self.scan('approver','FAILED')
        self.rows['approver']['environment']['PRECHECK_RC']='2'
        self.adapter('approver-repair',rc=1)
        self.assertIsNone(foreign.poll())
        before=self.calls.read_text()
        self.pidfile.unlink()
        p=self.daemon(); self.pidfile.unlink()
        self.scan('approver','READY')
        self.adapter('approver-repair')
        self.assertEqual(self.calls.read_text(),before)
        self.assertIsNone(p.poll())

    def test_approver_stale_repair(self):
        p=self.daemon(); self.write_log(age=1000)
        self.adapter('approver-check', rc=1)
        self.scan('approver','FAILED')
        self.adapter('approver-repair')
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail('stale verified daemon did not receive TERM')
        self.assertEqual(p.returncode,-15)
        self.assertFalse(self.calls.exists(),'TERM cycle must not launch concurrently')
        self.scan('approver','FAILED')
        self.invoke([self.rows['approver']],mode='reconcile')
        calls=self.calls.read_text()
        self.assertIn('--check',calls)
        self.assertIn('--loop 10000 --always --fallback-once',calls)
        self.scan('approver','READY')
        before=calls; self.invoke([self.rows['approver']],mode='reconcile')
        self.assertEqual(self.calls.read_text(),before)

    def test_identity_uid(self):
        p = self.identity_process()
        self.assertIsNotNone(self.approver.identity(p.pid, self.app, self.node))
        # UID is the only simulated property; /proc and the process are real.
        with patch.object(self.approver.os, 'getuid', return_value=os.getuid()+1):
            self.assertIsNone(self.approver.identity(p.pid, self.app, self.node))
            self.assertEqual(self.main(), 1)
        # The shell entry point inherits the real UID; use the same isolated
        # getuid override in a subprocess to verify its check exit status.
        code = ("import runpy,os,sys; os.getuid=lambda: %d; "
                "sys.argv=['approver.py','check']; runpy.run_path(%r,run_name='__main__')"
                % (os.getuid()+1, str(ROOT/'adapters/approver.py')))
        result = subprocess.run([sys.executable, '-c', code],
                                env=dict(os.environ, **self.rows['approver']['environment']))
        self.assertEqual(result.returncode, 1)

    def test_identity_node_basename(self):
        self.assert_rejected(self.identity_process(argv0='/usr/bin/python3'))

    def test_identity_cwd(self):
        other = self.root/'other'; other.mkdir()
        self.assert_rejected(self.identity_process(cwd=other))

    def test_identity_script_path(self):
        self.assert_rejected(self.identity_process(script=self.app/'other.cjs'))

    def test_identity_check_argument(self):
        self.assert_rejected(self.identity_process(args=('--check',)))

    def test_identity_zombie(self):
        p = self.identity_process()
        cmdline = Path(f'/proc/{p.pid}/cmdline').read_bytes()
        p.terminate()
        # Do not poll()/wait(): those reap the zombie under test.
        stat = Path(f'/proc/{p.pid}/stat')
        deadline = time.monotonic()+3
        while stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
            self.assertLess(time.monotonic(), deadline, 'child did not become zombie')
            time.sleep(.01)
        self.assert_rejected(p)
        # Linux zombies have empty cmdline and no cwd. Supply only those two
        # earlier fields to reach the Z guard; state/UID/starttime remain real.
        read_bytes, resolve = Path.read_bytes, Path.resolve
        def zombie_args(path):
            return cmdline if path == Path(f'/proc/{p.pid}/cmdline') else read_bytes(path)
        def zombie_cwd(path, *a, **kw):
            return self.app if path == Path(f'/proc/{p.pid}/cwd') else resolve(path, *a, **kw)
        with patch.object(Path, 'read_bytes', zombie_args), patch.object(Path, 'resolve', zombie_cwd):
            self.assertIsNone(self.approver.identity(p.pid, self.app, self.node))
            self.assertEqual(self.main(), 1)

    def test_identity_starttime_reuse(self):
        p = self.identity_process()
        token = self.approver.identity(p.pid, self.app, self.node)
        self.assertEqual(token, Path(f'/proc/{p.pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
        self.write_log(age=1000)
        # Deterministic reuse race: discovery observed a previous generation,
        # but identity at signal time reads this real process's starttime.
        with patch.object(self.approver, 'processes', return_value=[(p.pid, str(int(token)-1))]), \
             patch.object(self.approver.signal, 'pidfd_send_signal') as send:
            self.assertEqual(self.main('repair'), 0)
            send.assert_not_called()
        self.assertIsNone(p.poll())
        # Matching generation must still receive TERM (intercept only signal).
        with patch.object(self.approver.signal, 'pidfd_send_signal') as send:
            self.assertEqual(self.main('repair'), 0)
            send.assert_called_once()
        # An exited PID in a pidfile is not identity evidence for check.
        p.terminate(); p.wait()
        self.pidfile.write_text(str(p.pid)); self.write_log()
        self.assert_rejected(p)

    def test_approver_log_timestamp(self):
        self.daemon()
        self.write_log(age=1000)
        os.utime(self.log, None)
        self.adapter('approver-check', rc=1)
        self.write_log()
        old = time.time()-1000
        os.utime(self.log, (old, old))
        self.adapter('approver-check')
        # The final event, not an earlier fresh event, determines freshness.
        self.log.write_text(json.dumps({'ts': int(time.time()*1000)})+'\n'+
                            json.dumps({'ts': int((time.time()-1000)*1000)})+'\n')
        self.adapter('approver-check', rc=1)
        for body in ('', 'not-json', '{}', '{"ts":"fresh"}', '{"ts":null}', '{"ts":true}'):
            with self.subTest(body=body):
                self.log.write_text(body)
                self.adapter('approver-check', rc=1)

    def test_approver_precheck(self):
        self.rows['approver']['environment']['PRECHECK_RC']='2'
        self.invoke([self.rows['approver']],mode='reconcile',rc=20)
        self.assertFalse(self.pidfile.exists())
        self.assertIn('--check',self.calls.read_text())
        self.assertNotIn('--loop',self.calls.read_text())

    def test_external(self):
        external=[copy.deepcopy(s) for s in self.rows.values() if s['ownership']=='external']
        for s in external: s['environment']['EXTERNAL_RC']='1'
        out=self.invoke(external,mode='reconcile',rc=20)
        self.assertTrue(all(c['status']=='FAILED' for c in out['components']))
        self.assertFalse(self.calls.exists())
        # Poison repair/prepare: external itself must guard mutations, independently of repair:null.
        marker=self.root/'forbidden'
        for s in external:
            s['repair']=[f'touch {marker}']; s['prepare']=[f'touch {marker}']
        self.assertEqual(self.invoke(external,mode='reconcile',options=('--plan',)),[])
        self.invoke(external,mode='reconcile',rc=20)
        self.assertFalse(marker.exists())

    def test_keepalive(self):
        self.scan('gemini-keepalive','READY')
        self.accounts.write_text('{"accounts":[]}'); self.unit(active=False)
        self.scan('gemini-keepalive','STOPPED')
        self.unit(active=True); self.scan('gemini-keepalive','FAILED')
        self.accounts.write_text('{}')
        self.invoke([self.rows['gemini-keepalive']],rc=2)

    def test_marker_validation(self):
        s=copy.deepcopy(self.rows['approver'])
        for ref in ({'file':str(self.stop),'when_exists':'stopped'},
                    {'file':str(self.stop),'when_exists':'bad','when_missing':'running'},
                    {'file':str(self.stop),'when_exists':'stopped','when_missing':'running','pointer':''}):
            s['desired_state_ref']=ref
            self.invoke([s],mode='reconcile',options=('--validate',),rc=2)
            self.invoke([s],rc=2)
        s=copy.deepcopy(self.rows['approver']); s['desired_state']='running'
        self.invoke([s],mode='reconcile',options=('--validate',),rc=2)
        self.invoke([s],rc=2)


def mutations(test_names=()):
    cases=[
        ('identity UID bypassed','test_identity_uid','adapters/approver.py',
         'if proc.stat().st_uid != os.getuid():','if False:'),
        ('identity node basename bypassed','test_identity_node_basename','adapters/approver.py',
         "os.fsdecode(args[0]).split('/')[-1] != Path(node).name",'False'),
        ('identity cwd bypassed','test_identity_cwd','adapters/approver.py',
         'if cwd != root:','if False:'),
        ('identity script path bypassed','test_identity_script_path','adapters/approver.py',
         "script.resolve() != root / 'muse-daemon.cjs'",'False'),
        ('identity --check accepted','test_identity_check_argument','adapters/approver.py',
         "b'--check' in args",'False'),
        ('identity zombie accepted','test_identity_zombie','adapters/approver.py',
         "if fields[0] == 'Z':",'if False:'),
        ('identity starttime field wrong','test_identity_starttime_reuse','adapters/approver.py',
         'return fields[19]','return fields[18]'),
        ('identity starttime recheck bypassed','test_identity_starttime_reuse','adapters/approver.py',
         'identity(pid, root, node) == token','True'),
        ('stopped state uses health_check','test_approver_stop','bin/health.sh',
         "run_probe \"$(jq -r 'if .desired_state == \"stopped\" then .stopped_check else .health_check end'",
         "run_probe \"$(jq -r '.health_check'"),
        ('stop check returns success','test_approver_stop','adapters/approver.py',
         "return 0 if mode == 'repair' else 1",'return 0'),
        ('freshness threshold enlarged','test_approver_log_timestamp','adapters/approver.py',
         '0 <= age <= maximum','0 <= age <= 999999.999'),
        ('freshness uses mtime','test_approver_log_timestamp','adapters/approver.py',
         'age = time.time() - stamp / 1000','age = time.time() - log.stat().st_mtime'),
        ('freshness reads first event','test_approver_log_timestamp','adapters/approver.py',
         'for line in reversed(lines)','for line in lines'),
        ('probe ignores permissions','test_cf_check','config/services.muse01.json',
         '[ \\"$(stat -c %a \\"$SECRET_FILE\\")\\" = 600 ] || exit 1','exit 0'),
        ('alias check always succeeds','test_alias_check','adapters/ssh-alias-check.sh',
         'set -eu','exit 0\nset -eu'),
        ('approver ignores log freshness','test_approver_check','adapters/approver.py',
         'return 0 if live and healthy else 1','return 0 if live else 1'),
        ('approver ignores process presence','test_approver_check','adapters/approver.py',
         'return 0 if live and healthy else 1','return 0 if healthy else 1'),
        ('stop marker ignored','test_approver_stop','bin/health.sh',
         'state = r["when_exists"]','state = r["when_missing"]'),
        ('wrong process identity accepted','test_approver_identity','adapters/approver.py',
         "script.resolve() != root / 'muse-daemon.cjs'",'False'),
        ('stale process not terminated','test_approver_stale_repair','adapters/approver.py',
         'signal.pidfd_send_signal(fd, signal.SIGTERM)','pass'),
        ('precheck bypassed','test_approver_precheck','adapters/approver.py',
         'cwd=root, check=True','cwd=root, check=False'),
        ('external repair guard removed','test_external','bin/reconcile.sh',
         '.ownership == "external" or (has("repair") and .repair == null)',
         '(has("repair") and .repair == null)'),
        ('loaded probe unnecessarily reinstalled','test_cf_repair_environment_and_recheck','adapters/cf-probe-repair.sh',
         'systemctl start -- "$UNIT"','bash "$INSTALL_SCRIPT"'),
    ]
    if test_names:
        unknown = set(test_names) - {case[1] for case in cases}
        assert not unknown, ('unknown mutation tests', unknown)
        cases = [case for case in cases if case[1] in test_names]
    with tempfile.TemporaryDirectory(prefix='m4-mutants-') as td:
        for i,(label,test,rel,old,new) in enumerate(cases):
            root=Path(td)/str(i); shutil.copytree(ROOT,root)
            path=root/rel; source=path.read_text()
            assert source.count(old) == 1, (label,'mutation must match exactly once')
            path.write_text(source.replace(old,new,1))
            p=subprocess.run([sys.executable,str(Path(__file__).resolve()),'Tests.'+test],
                             env=dict(os.environ,M4_IMPL=str(root)),text=True,capture_output=True,timeout=90)
            output=p.stdout+p.stderr
            print('MUTATION:',label,flush=True)
            print(output,end='',flush=True)
            if p.returncode==0 or 'FAIL:' not in output or 'ERROR:' in output:
                raise AssertionError('mutant escaped: '+label)
            print('DETECTED',flush=True)
    print('M4 mutation checks: PASS (copies only)',flush=True)


if __name__=='__main__':
    if sys.argv[1:2]==['--mutations']: mutations(sys.argv[2:])
    else: unittest.main(verbosity=2)
