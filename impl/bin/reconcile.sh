#!/bin/bash
# Manifest-driven reconciliation; commands in manifests are trusted local code.
set -uo pipefail
umask 077
fail() { printf 'reconcile: %s\n' "$1" >&2; exit 2; }
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd) || exit 2
services= state_dir= run_dir= mode=execute json=false
while (($#)); do
    case $1 in
        --services|--state-dir|--run-dir)
            (($# >= 2)) || fail 'missing option value'
            case $1 in --services) services=$2;; --state-dir) state_dir=$2;; --run-dir) run_dir=$2;; esac
            shift 2;;
        --plan|--validate)
            [[ $mode == execute ]] || fail 'choose one mode'
            mode=${1#--}; shift;;
        --json) json=true; shift;;
        *) fail 'unknown option';;
    esac
done
[[ -n $services && -n $state_dir && -n $run_dir ]] || fail 'services, state-dir and run-dir are required'
if [[ $mode == validate ]]; then
    python3 "$base/lib/reconcile-manifest.py" validate "$services"
    exit $?
fi
manifest=$(python3 "$base/lib/reconcile-manifest.py" resolve "$services") || exit 2
# Plans are conditional: no probes, prepare commands or systemctl calls are run.
if [[ $mode == plan ]]; then
    jq '[.services[] | select(.ownership != "external") |
        select((has("repair") and .repair == null) | not) |
        {id, desired_state:(.desired_state // "running"),
         when:"target check fails and running prerequisites pass",
         prepare:(if .desired_state == "stopped" then [] else (.prepare // []) end),
         repair:(if has("repair") then {commands:(.repair | if type == "array" then . else [.] end)}
                 else {unit:(.unit // .environment.UNIT),
                       action:(if .desired_state == "stopped" then "stop" else "start" end),
                       reset_failed_if:"start-limit-hit"} end)}]' <<<"$manifest"
    exit $?
fi
mkdir -p -- "$run_dir" "$state_dir" || fail 'cannot create directories'
exec {lock_fd}>"$run_dir/reconcile.lock" || fail 'cannot open lock'
flock -n "$lock_fd" || exit 10
scratch=$(mktemp -d "$run_dir/.reconcile.XXXXXX") || fail 'cannot create scratch directory'
trap 'rm -rf -- "$scratch"' EXIT
# Reuse health.sh for identity, isolated environment, deadlines and prerequisites.
probe() {
    local row=$1 rc
    jq -cn --argjson row "$row" '{version:1,status_protocol:2,services:[$row]}' >"$scratch/probe.json" || return 2
    "$base/bin/health.sh" --services "$scratch/probe.json" --state-dir "$scratch/state" \
        --run-dir "$scratch/run" --json >"$scratch/result.json"
    rc=$?
    return "$rc"
}
command_probe() {
    local row
    row=$(jq -c --arg command "$1" '.desired_state="running" | .prerequisites=[] |
        .health_check=$command | .health_timeout_sec=(.timeout_sec // 30)' <<<"$service") || return 2
    probe "$row"
}
overall=0
while IFS= read -r service; do
    probe "$service"; rc=$?
    case $rc in
        0) continue;;
        10) [[ $overall == 20 ]] || overall=10; continue;;
        20) ;;
        *) fail 'health probe failed';;
    esac
    if jq -e '.ownership == "external" or (has("repair") and .repair == null)' <<<"$service" >/dev/null; then
        overall=20; continue
    fi
    desired=$(jq -r '.desired_state // "running"' <<<"$service")
    ok=true
    if [[ $desired == running ]]; then
        while IFS= read -r command; do
            command_probe "$(jq -r . <<<"$command")" || { ok=false; break; }
        done < <(jq -c '.prepare // [] | .[]' <<<"$service")
    fi
    if $ok; then
        if jq -e 'has("repair")' <<<"$service" >/dev/null; then
            while IFS= read -r command; do
                command_probe "$(jq -r . <<<"$command")" || { ok=false; break; }
            done < <(jq -c '.repair | if type == "array" then .[] else . end' <<<"$service")
        else
            unit=$(jq -r '.unit // .environment.UNIT' <<<"$service")
            # Shell-quote a manifest value; never interpolate it as shell code.
            printf -v quoted_unit '%q' "$unit"
            action=start; [[ $desired == stopped ]] && action=stop
            command_probe "result=\$(systemctl show -p Result --value $quoted_unit) || exit 1
if [[ \$result == start-limit-hit ]]; then systemctl reset-failed -- $quoted_unit || exit 1; fi
systemctl $action -- $quoted_unit" || ok=false
        fi
    fi
    if $ok; then
        sleep "$(jq -r '.settle_sec // 3' <<<"$service")" || fail 'cannot wait for repair'
        if ! jq -e 'has("repair")' <<<"$service" >/dev/null; then
            # Accept a clean designed exit, but not failed/activating/query errors.
            terminal="state=\$(systemctl show -p ActiveState --value $quoted_unit) || exit 1
result=\$(systemctl show -p Result --value $quoted_unit) || exit 1
code=\$(systemctl show -p ExecMainStatus --value $quoted_unit) || exit 1
[[ \$state == inactive && \$result == success && \$code == 0 ]]"
            if [[ $desired == running ]]; then
                terminal="state=\$(systemctl show -p ActiveState --value $quoted_unit) || exit 1
result=\$(systemctl show -p Result --value $quoted_unit) || exit 1
code=\$(systemctl show -p ExecMainStatus --value $quoted_unit) || exit 1
[[ \$state == active || ( \$state == inactive && \$result == success && \$code == 0 ) ]]"
            fi
            command_probe "$terminal" || ok=false
        fi
        if $ok; then
            probe "$service"; rc=$?
            case $rc in
                0) ;;
                10) [[ $overall == 20 ]] || overall=10;;
                20) ok=false;;
                *) fail 'health recheck failed';;
            esac
        fi
    fi
    $ok || overall=20
done < <(jq -c '.services[]' <<<"$manifest")
# Publish the final scan using exactly the caller's paths and current references.
args=(--services "$services" --state-dir "$state_dir" --run-dir "$run_dir")
$json && args+=(--json)
"$base/bin/health.sh" "${args[@]}"; health_rc=$?
case $health_rc in
    0) ;;
    10) [[ $overall == 20 ]] || overall=10;;
    20) overall=20;;
    *) exit "$health_rc";;
esac
exit "$overall"
