#!/usr/bin/env python3
"""Install persistent templates without resolving runtime desired-state references."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

BASE = Path(__file__).resolve().parents[1]
CONTROLLER = 'muse-selfheal-controller.service'


def systemctl(*args):
    return subprocess.run(['systemctl', *args], text=True, capture_output=True, timeout=30)


def checked(*args):
    result = systemctl(*args)
    if result.returncode:
        raise ValueError('systemctl ' + ' '.join(args) + ': ' + result.stderr.strip())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--services', type=Path, default=BASE/'config/services.json')
    parser.add_argument('--unit-dir', type=Path, default=BASE/'units')
    parser.add_argument('--system-unit-dir', type=Path, default=Path('/etc/systemd/system'),
                        help='alternate destination for isolated tests with a fake systemctl')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('manifest', BASE/'lib/reconcile-manifest.py')
    manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest)
    doc = json.loads(args.services.read_text())
    manifest.validate(doc)
    units = {CONTROLLER: False}
    for service in doc['services']:
        # environment.UNIT is the existing legacy registration form.
        unit = service.get('unit') or service.get('environment', {}).get('UNIT')
        if unit is None:
            continue
        if not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_@.:-]+\.service', unit):
            raise ValueError(f'invalid unit name: {unit!r}')
        external = service.get('ownership') == 'external'
        if unit in units and units[unit] != external:
            raise ValueError(f'conflicting ownership: {unit}')
        units[unit] = external
    # Preflight every template before changing any unit.
    templates = {}
    errors = False
    for unit, external in units.items():
        if external:
            print(f'{unit}: skipped(external)')
            continue
        try:
            data = (args.unit_dir/unit).read_bytes()
            if not data or re.search(rb'<[A-Z][A-Z0-9_]*>', data):
                raise ValueError('empty or unrendered template')
            templates[unit] = data
        except (OSError, ValueError) as exc:
            print(f'{unit}: error: {exc}', flush=True)
            errors = True
    if errors:
        return 1
    target = args.system_unit_dir
    if args.dry_run:
        for unit, data in templates.items():
            path = target/unit
            same = path.is_file() and path.read_bytes() == data
            print(f'{unit}: {"unchanged" if same else "installed"} (dry-run; would ensure enabled)')
        return 0
    target.mkdir(parents=True, exist_ok=True)
    with (target/'.selfheal-bootstrap.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = target/'.selfheal-bootstrap-reload-needed'
        results = {}
        for unit, data in templates.items():
            try:
                path = target/unit
                same = path.is_file() and path.read_bytes() == data
                if not same:
                    # Retain reload intent across a failed reload or interrupted install.
                    pending.touch()
                    fd, name = tempfile.mkstemp(prefix='.unit-', dir=target)
                    try:
                        with os.fdopen(fd, 'wb') as out:
                            out.write(data)
                            os.fchmod(out.fileno(), 0o644)
                        os.replace(name, path)
                    finally:
                        if os.path.exists(name):
                            os.unlink(name)
                results[unit] = 'unchanged' if same else 'installed'
            except OSError as exc:
                print(f'{unit}: error: {exc}', flush=True)
                errors = True
        if pending.exists():
            try:
                checked('daemon-reload')
                pending.unlink()
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                for unit in results:
                    print(f'{unit}: error: {exc}', flush=True)
                return 1
        for unit, state in results.items():
            try:
                enabled = systemctl('is-enabled', unit)
                if enabled.returncode != 0 or enabled.stdout.strip() != 'enabled':
                    # Avoid systemctl enable's implicit daemon reload.
                    checked('--no-reload', 'enable', unit)
                    enabled = systemctl('is-enabled', unit)
                    if enabled.returncode != 0 or enabled.stdout.strip() != 'enabled':
                        raise ValueError('unit did not become persistently enabled')
                print(f'{unit}: {state}', flush=True)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                print(f'{unit}: error: {exc}', flush=True)
                errors = True
    return int(errors)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f'bootstrap: error: {exc}', flush=True)
        raise SystemExit(2)
