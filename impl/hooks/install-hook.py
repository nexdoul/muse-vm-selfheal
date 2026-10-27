#!/usr/bin/env python3
"""Install the boot-ensure hook definition on a Muse VM.

The platform hook daemon reads definitions from the persistent volume and is
itself supplied by the read-only image, so a hook survives a reset that wipes
/etc/systemd/system.  This is the only entry point that can reinstall the
controller unit after such a wipe.

Idempotent: rewrites the definition only when its content differs.
"""
import argparse
import json
from pathlib import Path

DEFAULT_PATH = Path('/home/hatch/hooks/definitions/boot-ensure.json')
SCRIPT = '/home/hatch/hooks/scripts/boot-ensure.sh'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definitions', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--script', default=SCRIPT)
    parser.add_argument('--interval', type=int, default=120)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    definition = {
        'version': 1,
        'id': 'boot-ensure',
        'enabled': True,
        'script_path': args.script,
        'prompt': ('Boot-time unit bootstrap. Its script reinstalls this site\'s '
                   'systemd units from the persistent release when /etc was wiped, '
                   'then starts the controller. Silent when nothing changed.'),
        'poll_interval_secs': args.interval,
        'script_timeout_secs': 300,
        'delivery': {'surface': 'main'},
    }
    text = json.dumps(definition, indent=2, ensure_ascii=False) + '\n'
    path = args.definitions
    if path.is_file() and path.read_text() == text:
        print(f'{path}: unchanged')
        return 0
    if args.dry_run:
        print(f'{path}: would install (interval {args.interval}s)')
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f'{path}: installed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
