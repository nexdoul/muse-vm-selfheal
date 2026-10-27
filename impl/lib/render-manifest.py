#!/usr/bin/env python3
"""Render a manifest template into a deployable manifest.

The template keeps private host paths as <PLACEHOLDER> tokens so the repository
carries no environment-specific values.  Deployment renders them from a values
file, then bootstrap installs the units the rendered manifest registers.
"""
import argparse
import json
from pathlib import Path
import re
import sys

TOKEN = re.compile(r'<([A-Z][A-Z0-9_]*)>')


def render(value, values):
    if isinstance(value, str):
        def substitute(match):
            key = match.group(1)
            if key not in values:
                raise KeyError(key)
            return str(values[key])
        return TOKEN.sub(substitute, value)
    if isinstance(value, list):
        return [render(item, values) for item in value]
    if isinstance(value, dict):
        return {key: render(item, values) for key, item in value.items()}
    return value


def collect(value, found):
    if isinstance(value, str):
        found.update(TOKEN.findall(value))
    elif isinstance(value, list):
        for item in value:
            collect(item, found)
    elif isinstance(value, dict):
        for item in value.values():
            collect(item, found)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--values', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check', action='store_true',
                        help='only report placeholders; write nothing')
    args = parser.parse_args()

    document = json.loads(args.template.read_text())
    values = json.loads(args.values.read_text())

    if args.check:
        found = set()
        collect(document, found)
        missing = sorted(found - set(values))
        for key in missing:
            print(f'missing value: {key}')
        print(f'{len(found)} placeholder(s), {len(missing)} unresolved')
        return 1 if missing else 0

    rendered = render(document, values)
    leftover = set()
    collect(rendered, leftover)
    if leftover:
        print('unresolved placeholders: ' + ', '.join(sorted(leftover)), file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rendered, indent=2, ensure_ascii=False) + '\n')
    print(f'rendered {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
