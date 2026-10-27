#!/usr/bin/env python3
"""Local contract tests; --mutations proves selected faults are detected in copies."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEALTH = Path(os.environ.get('M2C_HEALTH', ROOT / 'bin/health.sh'))
MANIFEST_PATH = Path(os.environ.get('M2C_MANIFEST', ROOT / 'config/services.muse02.json'))
MANIFEST = json.loads(MANIFEST_PATH.read_text())


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='m2c-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root/'state'
        self.run = self.root/'run'
        self.state.mkdir(); self.run.mkdir()
        self.config = self.root/'target.json'
        self.config.write_text(json.dumps({'desired_state': 'stopped'}))

    def service(self, **kw):
        return dict(id='ref-test', workdir=str(self.root), health_check='exit 7',
                    stopped_check='exit 0', **kw)

    def ref(self, **kw):
        return dict(file=str(self.config), pointer='/desired_state', **kw)

    def scan(self, services, rc, statuses=None, protocol=2):
        manifest = self.root/'services.json'
        manifest.write_text(json.dumps(dict(version=1, status_protocol=protocol, services=services)))
        previous = (self.state/'status.json').read_bytes() if (self.state/'status.json').exists() else None
        p = subprocess.run([str(HEALTH), '--services', str(manifest), '--state-dir', str(self.state),
                            '--run-dir', str(self.run), '--json'], capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, rc, (p.stdout, p.stderr))
        if rc == 2:
            self.assertEqual(p.stdout, '')
            self.assertIn('health:', p.stderr)
            current = (self.state/'status.json').read_bytes() if (self.state/'status.json').exists() else None
            self.assertEqual(current, previous, 'manifest errors must not publish status')
            return
        out = json.loads(p.stdout)
        self.assertEqual(out, json.loads((self.state/'status.json').read_text()))
        self.assertEqual(out['status'], {0: 'READY', 10: 'WAITING', 20: 'FAILED'}[rc])
        self.assertEqual([(c['id'], c['status']) for c in out['components']], statuses)
        return out

    def test_reference(self):
        s = self.service(desired_state_ref=self.ref())
        self.scan([s], 0, [('ref-test', 'STOPPED')])
        self.scan([s], 0, [('ref-test', 'READY')], protocol=1)
        self.config.write_text('{"desired_state":"running"}')
        self.scan([s], 20, [('ref-test', 'FAILED')])
        s['health_check'] = 'exit 0'
        s['stopped_check'] = 'exit 9'
        self.scan([s], 0, [('ref-test', 'READY')])
        # RFC 6901 escaping, empty key, root pointer and array indexing.
        for data, pointer in [({'a/b': {'~key': ['stopped']}}, '/a~1b/~0key/0'),
                              ({'': 'stopped'}, '/'), ('stopped', ''),
                              ({'~1': 'stopped'}, '/~01')]:
            self.config.write_text(json.dumps(data))
            self.scan([self.service(desired_state_ref=dict(file=str(self.config), pointer=pointer))],
                      0, [('ref-test', 'STOPPED')])

    def test_reference_errors(self):
        self.scan([self.service(desired_state='stopped')], 0, [('ref-test', 'STOPPED')])
        marker = self.root/'probe-ran'
        first = self.service(desired_state='running')
        first['health_check'] = 'touch "$MARKER"'
        first['environment'] = {'MARKER': str(marker)}
        refs = [dict(file=str(self.root/'absent'), pointer='/desired_state'),
                dict(file=str(self.root), pointer='/desired_state'), self.ref() | {'pointer': '/missing'},
                self.ref() | {'pointer': 'desired_state'}, self.ref() | {'pointer': '/bad~2'},
                None, {}, self.ref() | {'file': ''}, self.ref() | {'pointer': None},
                self.ref(when_empty='stopped'), self.ref(when_empty='bad', when_nonempty='running')]
        for ref in refs:
            with self.subTest(ref=ref):
                self.scan([first, self.service(desired_state_ref=ref)], 2)
                self.assertFalse(marker.exists(), 'all refs must be resolved before probes')
        for raw in ('{broken', '', '{} {}', '{"desired_state":"maintenance"}',
                    '{"desired_state":null}', '{"desired_state":false}', '{"desired_state":[]}',
                    '{"desired_state":7}'):
            with self.subTest(raw=raw):
                self.config.write_text(raw)
                self.scan([self.service(desired_state_ref=self.ref())], 2)
        self.config.write_text('["stopped"]')
        for pointer in ('/01', '/-1', '/-', '/1', '/0/x'):
            self.scan([self.service(desired_state_ref=self.ref() | {'pointer': pointer})], 2)

    def test_conflict(self):
        for desired in ('running', 'stopped', None):
            self.scan([self.service(desired_state=desired, desired_state_ref=self.ref())], 2)

    def mock(self, name, body):
        path = self.root/name
        path.write_text('#!/bin/bash\n'+body+'\n'); path.chmod(0o755)
        return path

    def test_keepalive(self):
        s = copy.deepcopy(next(s for s in MANIFEST['services'] if s['id'] == 'gemini-keepalive'))
        self.assertNotIn('desired_state', s)
        self.assertEqual(s['desired_state_ref']['pointer'], '/accounts')
        s['desired_state_ref']['file'] = str(self.config)
        self.mock('systemctl', '''if [[ $1 == is-active ]]; then [[ $ACTIVE_STATE == active ]]; exit; fi
case "$*" in
 *LoadState*) echo "${LOAD_STATE:-loaded}";;
 *ActiveState*) echo "$ACTIVE_STATE";;
 *Result*) echo "${RESULT:-success}";;
 *ExecMainStatus*) echo "${CODE:-0}";;
 *) exit 1;; esac''')
        log = self.root/'keepalive.log'; log.write_text('synthetic freshness evidence\n')
        s['environment'].update(PATH=str(self.root)+':/usr/bin:/bin', LOG=str(log), ACTIVE_STATE='inactive')
        self.config.write_text('{"accounts":[]}')
        self.scan([s], 0, [('gemini-keepalive', 'STOPPED')])
        for field, value in [('RESULT', 'exit-code'), ('CODE', '20'), ('LOAD_STATE', 'not-found'),
                             ('ACTIVE_STATE', 'failed'), ('ACTIVE_STATE', 'active')]:
            bad = copy.deepcopy(s); bad['environment'][field] = value
            self.scan([bad], 20, [('gemini-keepalive', 'FAILED')])
        self.config.write_text('{"accounts":[{}]}')
        self.scan([s], 20, [('gemini-keepalive', 'FAILED')])
        s['environment']['ACTIVE_STATE'] = 'active'
        self.scan([s], 0, [('gemini-keepalive', 'READY')])
        for accounts in (None, {}, '', 0):
            self.config.write_text(json.dumps({'accounts': accounts}))
            self.scan([s], 2)
        self.config.write_text('{}'); self.scan([s], 2)

    def infrastructure(self):
        self.assertEqual([s['id'] for s in MANIFEST['services'][:3]], ['volume', 'config', 'tunnels'])
        rows = copy.deepcopy(MANIFEST['services'][:3])
        site = self.root/'site.json'; site.write_text('{"version":1,"relays":[{"reverse_port":12345}]}')
        config = self.root/'infra-services.json'; config.write_text('{"version":1}')
        self.mock('findmnt', '[[ "$*" == "-n -o SOURCE /synthetic-volume" ]] || exit 8\nprintf "%s\\n" "$MOCK_SOURCE"\nexit "${MOUNT_RC:-0}"')
        for s in rows:
            self.assertEqual(s['kind'], 'infrastructure')
            self.assertEqual(s['ownership'], 'external')
            self.assertIsNone(s['repair'])
            s['environment'].update(PATH=str(self.root)+':/usr/bin:/bin', SITE_CONFIG=str(site),
                SERVICES_CONFIG=str(config), VOLUME_PATH='/synthetic-volume', VOLUME_SOURCE='/synthetic-device',
                MOCK_SOURCE='/synthetic-device/partition')
        return rows, site, config

    def test_infrastructure(self):
        rows, site, config = self.infrastructure()
        ready = [('volume', 'READY'), ('config', 'READY'), ('tunnels', 'READY')]
        self.scan(rows, 0, ready)
        for change in ({'MOCK_SOURCE': '/wrong'}, {'MOUNT_RC': '1'}):
            bad = copy.deepcopy(rows); bad[0]['environment'].update(change)
            self.scan(bad, 20, [('volume', 'FAILED')]+ready[1:])
        for path in (site, config):
            old = path.read_text()
            for raw in ('{broken', '{"version":2,"relays":[]}'):
                path.write_text(raw)
                out = self.scan(rows, 20, [('volume', 'READY'), ('config', 'FAILED'),
                                         ('tunnels', 'WAITING' if path == site and raw == '{broken' else 'READY')])
                self.assertEqual(out['consecutive_failures'] > 0, True)
            path.write_text(old)

    def test_tunnels_waiting(self):
        rows, site, _ = self.infrastructure()
        for relay in ({}, {'reverse_port': None}):
            site.write_text(json.dumps({'version': 1, 'relays': [relay, {'reverse_port': 12345}]}))
            out = self.scan(rows, 10, [('volume', 'READY'), ('config', 'READY'), ('tunnels', 'WAITING')])
            self.assertEqual(out['components'][2]['reason'], 'waiting')
            self.scan([rows[2]], 10, [('tunnels', 'WAITING')], protocol=1)
        rows[0]['environment']['MOCK_SOURCE'] = '/wrong'
        self.scan(rows, 20, [('volume', 'FAILED'), ('config', 'READY'), ('tunnels', 'WAITING')])

    def test_install_patch(self):
        doc = (ROOT/'deploy/install-keepalive.patch.md').read_text()
        snippet = doc.split('```bash\n', 1)[1].split('```', 1)[0]
        self.mock('systemctl', '''[[ ${QUERY_RC:-0} == 0 ]] || exit "$QUERY_RC"
case "$*" in *ActiveState*) echo "$STATE";; *Result*) echo "$RESULT";; *ExecMainStatus*) echo "$CODE";; *) exit 1;; esac''')
        for state, result, code, query, rc, message in [
            ('active', 'success', '0', '0', 0, 'started (running)'),
            ('inactive', 'success', '0', '0', 0, 'clean exit by design'),
            ('inactive', 'exit-code', '20', '0', 20, 'FAILED'),
            ('inactive', 'success', '1', '0', 20, 'FAILED'),
            ('failed', 'success', '0', '0', 20, 'FAILED'),
            ('activating', 'success', '0', '0', 20, 'FAILED'),
            ('active', 'success', '0', '1', 20, '')]:
            env = dict(os.environ, PATH=str(self.root)+':/usr/bin:/bin', UNIT='synthetic.service',
                       STATE=state, RESULT=result, CODE=code, QUERY_RC=query)
            p = subprocess.run(['bash', '-c', snippet], env=env, text=True, capture_output=True)
            self.assertEqual(p.returncode, rc, (state, result, code, p.stdout, p.stderr))
            self.assertIn(message, p.stdout+p.stderr)


def mutations():
    """Mutate copies only; each selected behavioral test must reject its mutant."""
    original = HEALTH.read_text()
    cases = [
        ('ignore dual-source conflict', 'test_conflict',
         original.replace('(has("desired_state") | not) and', 'true and', 1), None),
        ('invert empty accounts target', 'test_keepalive',
         original.replace('then $ref.when_empty else $ref.when_nonempty',
                          'then $ref.when_nonempty else $ref.when_empty', 1), None),
        ('volume always succeeds', 'test_infrastructure', original, 'volume'),
        ('config always succeeds', 'test_infrastructure', original, 'config'),
        ('tunnels uses ordinary health_check', 'test_tunnels_waiting', original, 'tunnels'),
    ]
    with tempfile.TemporaryDirectory(prefix='m2c-mutants-') as td:
        for label, test, source, manifest_fault in cases:
            path = Path(td)/'health.sh'; path.write_text(source); path.chmod(0o755)
            manifest = copy.deepcopy(MANIFEST)
            if manifest_fault:
                row = next(s for s in manifest['services'] if s['id'] == manifest_fault)
                if manifest_fault in ('volume', 'config'): row['health_check'] = 'exit 0'
                else: row['health_check'] = row['prerequisites'][0]; row['prerequisites'] = []
            else:
                assert source != original, 'mutation did not apply'
            mp = Path(td)/'services.json'; mp.write_text(json.dumps(manifest))
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'Tests.'+test, '-v'],
                env=dict(os.environ, M2C_HEALTH=str(path), M2C_MANIFEST=str(mp)),
                capture_output=True, text=True, timeout=60)
            print('MUTATION:', label, flush=True)
            print(p.stdout+p.stderr, end='', flush=True)
            assert p.returncode != 0 and 'FAIL:' in p.stderr, 'mutant escaped behavioral assertions'
            print('DETECTED (exit=%s)' % p.returncode, flush=True)
    print('Mutation checks: PASS; repository implementation unchanged', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--mutations']:
        mutations()
    else:
        unittest.main(verbosity=2)
