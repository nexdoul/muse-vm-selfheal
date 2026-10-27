#!/usr/bin/env python3
"""Insert a selfheal bootstrap step into the muse02 tunnel-keeper cron prompt.

The platform hook daemon loads definitions only at startup, so a new hook is
never scheduled until the daemon restarts — and restarting the daemon on these
VMs risks a long connectivity outage.  The tunnel-keeper cron already runs
every 15 minutes through the agent scheduler, so it is the available entry
point that survives a reset without touching the daemon.

Idempotent: refuses to write if the bootstrap step is already present.
"""
import sys
from pathlib import Path

PATH = Path('/home/hatch/workspace/goals/muse-vm/crons/minutely/'
            'muse-selfheal-tunnel-keeper__interval@15m.md')

STEP = """- 每次回合开始先跑一次 unit 自举（幂等，无需判断）：`RELEASES=/home/hatch/selfheal-releases SELFHEAL=/home/hatch/selfheal bash /home/hatch/selfheal-releases/current/impl/bin/boot-ensure.sh`。VM 重置会清空 /etc/systemd/system，这个命令从持久卷把本机 unit 装回并启动 controller；稳态下它只打印 unchanged，不会重启任何服务。
"""


def main():
    text = PATH.read_text()
    if 'boot-ensure.sh' in text:
        print('bootstrap step already present; nothing to do')
        return 0
    marker = '- 如果两条隧道都正常且 health 是 READY'
    if marker not in text:
        print(f'anchor not found in {PATH}', file=sys.stderr)
        return 1
    PATH.write_text(text.replace(marker, STEP + marker, 1))
    print(f'inserted bootstrap step into {PATH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
