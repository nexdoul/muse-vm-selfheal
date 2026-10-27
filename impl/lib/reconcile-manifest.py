#!/usr/bin/env python3
"""Validate trusted manifests and resolve the health.sh JSON Pointer contract."""
import json
import math
import os
import re
import sys


def require(ok):
    if not ok:
        raise ValueError('invalid services manifest')


def string(value):
    return isinstance(value, str) and bool(value) and '\0' not in value


def commands(value):
    return isinstance(value, list) and all(string(x) for x in value)


def validate(doc):
    require(isinstance(doc, dict) and doc.get('version') == 1)
    require(doc.get('status_protocol', 1) in (1, 2))
    require(isinstance(doc.get('services'), list))
    ids = set()
    for s in doc['services']:
        require(isinstance(s, dict) and string(s.get('id')) and s['id'] not in ids)
        ids.add(s['id'])
        require(s.get('ownership', 'internal') in ('internal', 'managed', 'external'))
        require(commands(s.get('prerequisites', [])) and commands(s.get('prepare', [])))
        if 'repair' in s:
            require(s['repair'] is None or string(s['repair']) or
                    (commands(s['repair']) and len(s['repair']) > 0))
        elif s.get('ownership') != 'external':
            require(string(s.get('unit', s.get('environment', {}).get('UNIT'))))
        for key in ('health_timeout_sec', 'timeout_sec', 'settle_sec'):
            n = s.get(key, 3 if key == 'settle_sec' else 5)
            require(type(n) in (int, float) and math.isfinite(n) and (n >= 0 if key == 'settle_sec' else n > 0))
        env = s.get('environment', {})
        require(isinstance(env, dict) and all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
                and isinstance(v, str) and '\0' not in v for k, v in env.items()))
        for k in ('home', 'workdir', 'user', 'environment_file', 'unit', 'kind'):
            require(s.get(k) is None or string(s[k]))
        if 'desired_state_ref' in s:
            require('desired_state' not in s)
            r = s['desired_state_ref']
            require(isinstance(r, dict) and string(r.get('file')))
            if 'when_exists' in r or 'when_missing' in r:
                require(set(r) == {'file', 'when_exists', 'when_missing'})
                require(r.get('when_exists') in ('running', 'stopped') and
                        r.get('when_missing') in ('running', 'stopped'))
                require(string(s.get('health_check')) and string(s.get('stopped_check')))
                continue
            p = r.get('pointer')
            require(isinstance(p, str) and (p == '' or p.startswith('/')) and
                    not re.search(r'~([^01]|$)', p))
            if 'when_empty' in r or 'when_nonempty' in r:
                require(r.get('when_empty') in ('running', 'stopped') and
                        r.get('when_nonempty') in ('running', 'stopped'))
            require(string(s.get('health_check')) and string(s.get('stopped_check')))
        else:
            require(s.get('desired_state', 'running') in ('running', 'stopped'))
            require(string(s.get('stopped_check' if s.get('desired_state') == 'stopped' else 'health_check')))


def resolve(doc):
    for s in doc['services']:
        if 'desired_state_ref' not in s:
            continue
        r = s.pop('desired_state_ref')
        if 'when_exists' in r:
            try:
                os.stat(r['file'])
                value = r['when_exists']
            except FileNotFoundError:
                value = r['when_missing']
            s['desired_state'] = value
            continue
        with open(r['file']) as f:
            value = json.load(f)
        for part in ([] if r['pointer'] == '' else r['pointer'].split('/')[1:]):
            key = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, dict):
                value = value[key]
            elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', key):
                value = value[int(key)]
            else:
                raise ValueError('invalid reference traversal')
        if 'when_empty' in r:
            require(isinstance(value, list))
            value = r['when_nonempty'] if value else r['when_empty']
        require(value in ('running', 'stopped'))
        s['desired_state'] = value
    return doc


if __name__ == '__main__':
    try:
        with open(sys.argv[2]) as f:
            doc = json.load(f)
        validate(doc)
        if sys.argv[1] != 'validate':
            print(json.dumps(resolve(doc)))
    except (ValueError, OSError, KeyError, IndexError, TypeError, AttributeError):
        print('reconcile: invalid manifest or unresolved desired_state_ref', file=sys.stderr)
        sys.exit(2)
