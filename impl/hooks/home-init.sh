#!/bin/bash
set -e
umask 077
source "${HATCH_HOOK_RUNTIME:?}"
[[ "${HATCH_HOOK_DRY_RUN:-0}" == 1 ]] && silent "dry-run"
[[ -x /home/hatch/init.sh ]] || silent "waiting for init.sh"
mkdir -p /run/hatch-home-init
exec 9>/run/hatch-home-init/lock
flock -n 9 || silent "running"
[[ ! -e /run/hatch-home-init/started ]] || silent "already run"
cd /home/hatch
{
    printf '\n[%s] init started\n' "$(date -Is)"
    # The marker records a *successful* run, not an attempt: a transient
    # failure (apt lock, network blip, interrupted restore) must be retried on
    # the next poll instead of losing the whole boot.  A genuine failure is
    # still bounded by the attempt counter below.
    attempts_file=/run/hatch-home-init/attempts
    attempts=$(cat "$attempts_file" 2>/dev/null || echo 0)
    attempts=$((attempts + 1))
    printf '%s\n' "$attempts" >"$attempts_file"
    /bin/bash ./init.sh 9>&- </dev/null && rc=0 || rc=$?
    printf '[%s] init exited: %s (attempt %s)\n' "$(date -Is)" "$rc" "$attempts"
    if ((rc == 0)); then
        touch /run/hatch-home-init/started
    elif ((attempts >= 5)); then
        # Stop retrying after five failures in one boot; the marker prevents an
        # endless loop while still leaving the log for diagnosis.
        touch /run/hatch-home-init/started
        printf '[%s] giving up after %s attempts\n' "$(date -Is)" "$attempts"
    fi
} >>/tmp/home-init.log 2>&1
silent "already run"
