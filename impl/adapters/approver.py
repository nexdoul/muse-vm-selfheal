#!/usr/bin/env python3
"""MuseAutoApprove identity, freshness and one-step recovery (Linux /proc)."""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def identity(pid, root, node):
    try:
        proc = Path('/proc') / str(pid)
        if proc.stat().st_uid != os.getuid():
            return None
        args = proc.joinpath('cmdline').read_bytes().split(b'\0')
        if len(args) < 3 or os.fsdecode(args[0]).split('/')[-1] != Path(node).name:
            return None
        cwd = proc.joinpath('cwd').resolve(strict=True)
        if cwd != root:
            return None
        script = Path(os.fsdecode(args[1]))
        if not script.is_absolute():
            script = cwd / script
        if script.resolve() != root / 'muse-daemon.cjs' or b'--check' in args:
            return None
        # starttime distinguishes reuse of the same numeric PID.
        fields = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def processes(root, node):
    # A stale/missing pidfile must neither authorize a signal nor create a duplicate.
    return [(int(p.name), token) for p in Path('/proc').iterdir()
            if p.name.isdecimal() and (token := identity(int(p.name), root, node)) is not None]


def fresh(log, maximum):
    try:
        # The final daemon event carries a Unix timestamp in milliseconds.
        # Copying/touching the file must not refresh stale business evidence.
        lines = log.read_text().splitlines()
        event = json.loads(next(line for line in reversed(lines) if line.strip()))
        stamp = event['ts']
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return False
        age = time.time() - stamp / 1000
        return math.isfinite(age) and 0 <= age <= maximum
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return False


def main():
    mode = sys.argv[1]
    root = Path(os.environ['APPROVER_ROOT']).resolve(strict=True)
    maximum = int(os.environ.get('MAX_STALE_SEC', '900'))
    if maximum < 0:
        return 1
    node = os.environ.get('NODE_BIN', 'node')
    stop = root / 'data/muse-daemon.stop'
    if stop.exists():
        return 0 if mode == 'repair' else 1  # stopped_check owns the STOPPED label
    live = processes(root, node)
    healthy = fresh(root / 'log/daemon-log.ndjson', maximum)
    if mode == 'check':
        return 0 if live and healthy else 1
    if mode != 'repair':
        return 1
    if live:
        if not healthy:
            for pid, token in live:
                try:
                    fd = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    if not stop.exists() and identity(pid, root, node) == token:
                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                finally:
                    os.close(fd)
        # TERM is not proof of exit. Only a later cycle may start a replacement.
        return 0
    subprocess.run([node, 'muse-daemon.cjs', '--check'], cwd=root, check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if stop.exists() or processes(root, node):
        return 0
    (root / 'log').mkdir(exist_ok=True)
    with (root / 'log/muse-console.log').open('ab') as console:
        subprocess.Popen(['nohup', node, 'muse-daemon.cjs', '--loop', '10000', '--always', '--fallback-once'],
                         cwd=root, stdin=subprocess.DEVNULL, stdout=console, stderr=console,
                         close_fds=True)
    return 0  # reconcile must recheck process identity AND the daemon's own log.


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit(1)
