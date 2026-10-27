#!/usr/bin/env python3
"""Isolated protocol and probe regression checks; never operates real services."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HEALTH = ROOT / 'bin/health.sh'
MANIFEST = json.loads((ROOT / 'config/services.muse02.json').read_text())
with tempfile.TemporaryDirectory(prefix='m2-contract-') as td:
    root = Path(td)
    state, run = root / 'state', root / 'run'
    state.mkdir(); run.mkdir()
    config = root / 'services.json'
    def scan(services, expected, **extra):
        config.write_text(json.dumps(dict(version=1, status_protocol=2, services=services, **extra)))
        p = subprocess.run([str(HEALTH), '--services', str(config), '--state-dir', str(state),
                            '--run-dir', str(run), '--json'], capture_output=True, text=True, timeout=12)
        assert p.returncode == expected, (p.returncode, p.stdout, p.stderr)
        out = json.loads(p.stdout)
        assert out == json.loads((state / 'status.json').read_text())
        assert not list(state.glob('.*'))
        return out
    def svc(name, **kw):
        return dict(id=name, workdir=td, health_check='exit 0', **kw)
    waiting = svc('waiting', prerequisites=['test "$TOKEN" = configured'], environment={'TOKEN':'missing'})
    failed = dict(svc('failed'), health_check='exit 7')
    for rows in ([failed, waiting], [waiting, failed]):
        out = scan(rows, 20)
        assert out['status'] == 'FAILED'
        assert {c['status'] for c in out['components']} == {'FAILED','WAITING'}
    assert out['consecutive_failures'] == 2
    out = scan([waiting], 10)
    assert out['status'] == 'WAITING' and out['consecutive_failures'] == 0
    out = scan([svc('stopped', desired_state='stopped', stopped_check='exit 0', prerequisites=['exit 1'])], 0)
    assert out['components'][0] == dict(id='stopped', status='STOPPED', reason='stopped')
    assert out['generation'] == 'unknown' and out['last_success'] == out['checked_at']
    last_success = out['last_success']
    (run/'generation').write_text('fixture-generation\n')
    out = scan([failed], 20)
    assert out['generation'] == 'fixture-generation' and out['last_success'] == last_success
    assert out['boot_id'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert out['config_version'] == '1' and out['consecutive_failures'] == 1
    print('protocol: WAITING, failure precedence (both orders), STOPPED, metadata, atomic output PASS')

    # Execute real manifest expressions against deterministic command adapters.
    bindir = root/'bin'; bindir.mkdir()
    def mock(name, code):
        path = bindir/name; path.write_text('#!/bin/bash\n'+code+'\n'); path.chmod(0o755)
    mock('systemctl', '''if [[ $1 == is-active ]]; then exit "${ACTIVE_RC:-0}"; fi
case "$*" in *LoadState*) echo "${LOAD_STATE:-loaded}";; *) echo "${ACTIVE_STATE:-inactive}";; esac''')
    mock('curl', 'exit "${CURL_RC:-0}"')
    mock('ss', 'printf "%s\\n" "$LISTENERS"; exit "${SS_RC:-0}"')
    def probe(name, expected, **env):
        s=copy.deepcopy(next(s for s in MANIFEST['services'] if s['id']==name))
        s.pop('user', None)
        s['environment'].update(PATH=str(bindir)+':/usr/bin:/bin', **env)
        return scan([s], expected)
    probe('local-sshd', 0, LISTENERS='LISTEN 0 128 127.0.0.1:22 0.0.0.0:*')
    probe('local-sshd', 20, LISTENERS='LISTEN 0 128 127.0.0.1:2222 0.0.0.0:*')
    probe('local-sshd', 20, LISTENERS='LISTEN 0 128 127.0.0.1:22 0.0.0.0:*', SS_RC='1')
    for name in ('douyin-parse','douyin-clip-mcp'):
        probe(name, 0)
        probe(name, 20, CURL_RC='22')
        probe(name, 20, ACTIVE_RC='3')
    cfg=root/'approver.json'; data=root/'status-data.json'; calls=root/'calls'
    mock('approverctl', 'echo called >> "$CALLS"; cat "$STATUS_DATA"')
    cfg.write_text(json.dumps(dict(version=1, desired_state='running', enabled=True)))
    def approver(pid, seconds):
        data.write_text(json.dumps(dict(worker_pid=pid, evidence=dict(last_list_success_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()-seconds))))))
    kwargs=dict(APPROVER_CONFIG=str(cfg), APPROVERCTL=str(bindir/'approverctl'), STATUS_DATA=str(data), CALLS=str(calls))
    approver(os.getpid(), 0); probe('approver', 0, **kwargs)
    assert calls.read_text().splitlines() == ['called']
    approver(2147483647, 0); probe('approver', 20, **kwargs)
    approver(os.getpid(), 121); probe('approver', 20, **kwargs)
    approver(os.getpid(), -60); probe('approver', 20, **kwargs)
    data.write_text('{broken'); probe('approver', 20, **kwargs)
    cfg.write_text(json.dumps(dict(version=1, desired_state='stopped', enabled=True)))
    before=calls.read_text(); probe('approver', 0, **kwargs); assert calls.read_text()==before
    log=root/'keepalive.log'; log.write_text('refresh ok\n')
    probe('gemini-keepalive', 0, LOG=str(log))
    os.utime(log,(time.time()-600,time.time()-600)); probe('gemini-keepalive', 20, LOG=str(log))
    probe('gemini-keepalive', 20, LOG=str(log), ACTIVE_RC='3')
    stop=copy.deepcopy(next(s for s in MANIFEST['services'] if s['id']=='gemini-keepalive'))
    stop['desired_state']='stopped'; stop['environment']['PATH']=str(bindir)+':/usr/bin:/bin'
    assert scan([stop],0)['components'][0]['status']=='STOPPED'
    stop['environment']['LOAD_STATE']='not-found'; scan([stop],20)
    print('manifest probes: exact listener, approver single call/live PID/freshness/stopped, keepalive freshness/design-stopped, HTTP and unit conjunction PASS')

    # A process with synthetic SSH argv, without opening any connection.
    sleeper=root/'sleeper.py'; sleeper.write_text('import time; time.sleep(30)\n')
    child=subprocess.Popen(['ssh',str(sleeper),'-N','-R','127.0.0.1:23456:127.0.0.1:22','-p','22','root@192.0.2.40'],executable='/usr/bin/python3')
    try:
        for name in ('tunnel-krvps','tunnel-usvps'):
            probe(name, 0, REVERSE_PORT='23456', RELAY_HOST='192.0.2.40')
            probe(name, 20, REVERSE_PORT='2345', RELAY_HOST='192.0.2.40')
            probe(name, 20, REVERSE_PORT='23456', RELAY_HOST='192x0x2x40')
        child.terminate(); child.wait()
        probe('tunnel-krvps',20, REVERSE_PORT='23456', RELAY_HOST='192.0.2.40')
    finally:
        if child.poll() is None: child.terminate(); child.wait()
    print('tunnels: real pgrep matches SSH-shaped argv, rejects wrong port/host and absent process PASS')
print('M2 regression: PASS')
