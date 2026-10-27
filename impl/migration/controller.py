#!/usr/bin/env python3
"""Local deployment template: one release pointer owns code/config/adapters/units."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def call(*args, capture=False):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None)


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2)+'\n')
    tmp.chmod(0o600)
    tmp.replace(path)


def pointer(path, target):
    tmp = path.with_name(path.name+'.switch')
    if os.path.lexists(tmp):
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(path)


def snapshot(path, backup):
    if path.is_symlink():
        return {'path': str(path), 'link': os.readlink(path)}
    if path.exists():
        if not path.is_file():
            raise ValueError('managed reference must be a file or symlink')
        shutil.copy2(path, backup)
        return {'path': str(path), 'backup': str(backup)}
    return {'path': str(path)}


def restore(item):
    path = Path(item['path'])
    if 'link' in item:
        pointer(path, item['link'])
    elif 'backup' in item:
        tmp = path.with_name(path.name+'.restore')
        shutil.copy2(item['backup'], tmp)
        tmp.replace(path)
    elif os.path.lexists(path):
        path.unlink()


def unit_state(unit):
    out = {}
    for key, verb in [('active', 'is-active'), ('enabled', 'is-enabled')]:
        p = subprocess.run(['systemctl', verb, unit], text=True, capture_output=True)
        out[key] = p.stdout.strip()
    if out['active'] not in ('active', 'inactive', 'failed') or out['enabled'] not in (
            'enabled', 'disabled', 'static', 'masked', 'indirect'):
        raise ValueError('unsupported unit state; no changes made')
    return out


def rollback(root):
    old_file, new_file = root/'old-state.json', root/'new-state.json'
    old = json.loads(old_file.read_text())
    new = json.loads(new_file.read_text()) if new_file.exists() else None
    if new:
        # Remove the new restart source before stopping its controller.
        hook_path = Path(new['hook']['path'])
        if os.path.lexists(hook_path):
            hook_path.unlink()
        call('systemctl', 'stop', new['unit'])
        call('systemctl', 'disable', new['unit'])
        restore(new['current'])
        restore(new['unit_link'])
        call('systemctl', 'daemon-reload')
        restore(new['hook'])
    for hook in old['hooks']:
        source, disabled = Path(hook['path']), Path(hook['disabled'])
        if disabled.exists() or disabled.is_symlink():
            shutil.move(str(disabled), str(source))
    if old['enabled'] == 'enabled':
        call('systemctl', 'enable', old['unit'])
    elif old['enabled'] == 'masked':
        call('systemctl', 'mask', old['unit'])
    else:
        call('systemctl', 'disable', old['unit'])
    call('systemctl', 'start' if old['active'] == 'active' else 'stop', old['unit'])
    if new_file.exists():
        new_file.unlink()
    # Retain a rollback record; a later quiesce takes a fresh snapshot.
    old['restored'] = True
    save(old_file, old)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['quiesce-old', 'enable-new', 'rollback'])
    p.add_argument('--root', required=True, type=Path)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--unit')
    p.add_argument('--hook', action='append', default=[], type=Path,
                   help='old restart entry file; repeat for every hook/bootstrap entry')
    p.add_argument('--release', type=Path)
    p.add_argument('--services', type=Path)
    p.add_argument('--unit-link', type=Path)
    p.add_argument('--hook-link', type=Path)
    a = p.parse_args()
    root = a.root.absolute()
    if a.dry_run:
        print(f'DRY-RUN {a.operation}: root={root}; no writes or commands executed')
        if a.operation == 'quiesce-old':
            print(f'record active/enabled state: {a.unit or "<OLD_UNIT>"} -> {root}/old-state.json')
            print('disable all hook/bootstrap entry files: '+(', '.join(map(str, a.hook)) or '<repeat --hook PATH>'))
            print(f'stop and disable: {a.unit or "<OLD_UNIT>"}')
        elif a.operation == 'enable-new':
            print(f'validate complete release and bundled manifest: {a.services or "<SERVICES>"}')
            print(f'atomically switch {root}/current -> {a.release or "<RELEASE>"} (bin/lib/config/adapters/units/hooks)')
            print('link --unit-link and --hook-link through current; daemon-reload; enable --now <NEW_UNIT>')
            print('health.sh --json: require exit 0 and exact manifest ID multiset; on failure rollback all references and old controller')
        else:
            print('disable new restart entry; stop/disable new unit; restore release/unit/hook references')
            print('restore old hook/bootstrap entries and recorded enabled/active state')
        return
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(root/'migration.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old_file = root/'old-state.json'
        if a.operation == 'rollback':
            rollback(root)
        elif a.operation == 'quiesce-old':
            if not a.unit or not a.hook:
                raise ValueError('--unit and all --hook entry paths are required')
            if old_file.exists() and not json.loads(old_file.read_text()).get('restored'):
                old = json.loads(old_file.read_text())
                if old['unit'] != a.unit or [h['path'] for h in old['hooks']] != [str(x.absolute()) for x in a.hook]:
                    raise ValueError('quiesce arguments differ from saved transaction')
            else:
                hooks = []
                for index, h in enumerate(a.hook):
                    h = h.absolute()
                    disabled = root/f'old-hook-{index}.disabled'
                    if not os.path.lexists(h) or h.is_dir() or os.path.lexists(disabled):
                        raise ValueError('missing hook or occupied disabled path')
                    hooks.append({'path': str(h), 'disabled': str(disabled)})
                old = dict(unit=a.unit, hooks=hooks, **unit_state(a.unit))
                save(old_file, old)
            # Renaming dedicated entry files removes them from their dispatcher.
            for h in old['hooks']:
                source, disabled = Path(h['path']), Path(h['disabled'])
                if os.path.lexists(source):
                    shutil.move(str(source), str(disabled))
            call('systemctl', 'stop', old['unit'])
            call('systemctl', 'disable', old['unit'])
            old['quiesced'] = True
            save(old_file, old)
        else:
            if (root/'new-state.json').exists():
                raise ValueError('deployment transaction exists; rollback before another enable')
            try:
                if not all((a.release, a.services, a.unit, a.unit_link, a.hook_link)):
                    raise ValueError('--release, --services, --unit, --unit-link, --hook-link are required')
                if not old_file.exists() or json.loads(old_file.read_text()).get('restored'):
                    raise ValueError('quiesce-old must complete before enable-new')
                old = json.loads(old_file.read_text())
                if not old.get('quiesced'):
                    raise ValueError('quiesce-old did not finish')
                if a.unit == old['unit']:
                    raise ValueError('new unit must have a distinct name')
                if any(os.path.lexists(h['path']) or not os.path.lexists(h['disabled']) for h in old['hooks']):
                    raise ValueError('old restart sources are not quiesced')
                if Path(a.unit).name != a.unit or not a.unit.endswith('.service'):
                    raise ValueError('new unit must be a service basename')
                managed = [root/'current', a.unit_link.absolute(), a.hook_link.absolute()]
                if len(set(managed)) != 3:
                    raise ValueError('managed reference paths must be distinct')
                if os.path.lexists(a.unit_link) or os.path.lexists(a.hook_link):
                    raise ValueError('new unit and hook reference paths must be unused')
                active = subprocess.run(['systemctl', 'is-active', '--quiet', a.unit])
                if active.returncode == 0:
                    raise ValueError('new controller is already active')
                release = a.release.resolve(strict=True)
                for path in ('bin/health.sh', 'bin/reconcile.sh', 'lib/reconcile-manifest.py',
                             'config/services.json', 'adapters', 'units/'+a.unit, 'hooks/bootstrap'):
                    candidate = (release/path).resolve(strict=True)
                    if not candidate.is_relative_to(release):
                        raise ValueError('release references must remain within release')
                if a.services.read_bytes() != (release/'config/services.json').read_bytes():
                    raise ValueError('--services must equal bundled config/services.json')
                if not os.access(release/'hooks/bootstrap', os.X_OK):
                    raise ValueError('release hook must be executable')
                call(str(release/'bin/reconcile.sh'), '--services', str(a.services),
                     '--state-dir', str(root/'state'), '--run-dir', str(root/'run'), '--validate')
                current = root/'current'
                if current.exists() and not current.is_symlink():
                    raise ValueError('current must be a release symlink')
                if a.hook_link.absolute() in [Path(h['path']) for h in old['hooks']]:
                    raise ValueError('new hook entry must have a distinct path')
                new = {'unit': a.unit, 'current': snapshot(current, root/'current.backup'),
                       'unit_link': snapshot(a.unit_link.absolute(), root/'unit.backup'),
                       'hook': snapshot(a.hook_link.absolute(), root/'hook.backup')}
                save(root/'new-state.json', new)
                pointer(current, release)
                pointer(a.unit_link.absolute(), current/'units'/a.unit)
                call('systemctl', 'daemon-reload')
                call('systemctl', 'enable', '--now', a.unit)
                result = call(str(current/'bin/health.sh'), '--services', str(current/'config/services.json'),
                              '--state-dir', str(root/'state'), '--run-dir', str(root/'run'), '--json', capture=True)
                status = json.loads(result.stdout)
                manifest = json.loads((current/'config/services.json').read_text())
                if sorted(c['id'] for c in status['components']) != sorted(s['id'] for s in manifest['services']):
                    raise ValueError('health component set differs from manifest')
                if status['status'] != 'READY':
                    raise ValueError('health is not READY')
                call('systemctl', 'is-active', '--quiet', a.unit)
                pointer(a.hook_link.absolute(), current/'hooks/bootstrap')
            except Exception:
                if old_file.exists() and not json.loads(old_file.read_text()).get('restored'):
                    rollback(root)
                raise


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'migration: {exc}', file=sys.stderr)
        sys.exit(2)
