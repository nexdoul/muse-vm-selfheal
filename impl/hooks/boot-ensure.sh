#!/bin/bash
# Boot-time unit bootstrap.  Runs from the platform hook daemon, which is
# provided by the read-only image, so this survives a VM reset even when
# /etc/systemd/system was wiped and no unit exists yet.
#
# Idempotent: boot-ensure.sh installs only units whose content differs and
# only enables what is not already enabled.  Silent unless it fails, so the
# hook does not spam the agent.
set -uo pipefail
R=$(readlink -f /home/hatch/selfheal-releases/current 2>/dev/null) || {
    printf 'boot-ensure: no current release\n' >&2
    exit 1
}
[[ -x $R/impl/bin/boot-ensure.sh ]] || {
    printf 'boot-ensure: %s missing\n' "$R/impl/bin/boot-ensure.sh" >&2
    exit 1
}
out=$(RELEASES=/home/hatch/selfheal-releases SELFHEAL=/home/hatch/selfheal \
    SITE="$(hostname -s)" bash "$R/impl/bin/boot-ensure.sh" 2>&1)
rc=$?
if ((rc != 0)); then
    printf 'boot-ensure failed rc=%s\n%s\n' "$rc" "$out" >&2
    exit "$rc"
fi
# Successful installs are silent; only surface a wake when something was
# actually reinstalled, so a steady state produces no agent turns.
if grep -q 'installed' <<<"$out"; then
    printf 'boot-ensure reinstalled units:\n%s\n' "$(grep 'installed' <<<"$out")"
fi
exit 0
