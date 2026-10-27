#!/bin/bash
# Manifest-driven, local health checks. Probe output may contain secrets: discard it.
set -uo pipefail
umask 077

fail() { printf 'health: %s\n' "$1" >&2; exit 2; }
services= state_dir= run_dir= json=false
while (($#)); do
    case "$1" in
        --services|--state-dir|--run-dir)
            (($# >= 2)) || fail 'missing option value'
            case "$1" in
                --services) services=$2 ;;
                --state-dir) state_dir=$2 ;;
                --run-dir) run_dir=$2 ;;
            esac
            shift 2 ;;
        --json) json=true; shift ;;
        *) fail 'unknown option' ;;
    esac
done
[[ -n $services && -n $state_dir && -n $run_dir ]] || fail 'services, state-dir and run-dir are required'
for tool in jq setsid env flock mktemp mkfifo id getent; do
    command -v "$tool" >/dev/null || fail 'required system tool is unavailable'
done
manifest=$(jq -c . "$services" 2>/dev/null) || fail 'cannot read services manifest'
# Resolve every external target before any probe or status publication.
jq -e '.services | type == "array" and all(.[]; type == "object")' \
    <<<"$manifest" >/dev/null 2>&1 || fail 'invalid services manifest'
resolved='[]'
while IFS= read -r service; do
    if jq -e 'has("desired_state_ref")' <<<"$service" >/dev/null; then
        # Optional marker reference; legacy JSON Pointer resolution stays below.
        if jq -e '.desired_state_ref | has("when_exists") or has("when_missing")' <<<"$service" >/dev/null 2>&1; then
            jq -e '(has("desired_state") | not) and
                (.desired_state_ref | type == "object" and
                 (keys == ["file", "when_exists", "when_missing"]) and
                 (.file | type == "string" and length > 0 and (contains("\u0000") | not)) and
                 all(.when_exists, .when_missing; . == "running" or . == "stopped"))' \
                <<<"$service" >/dev/null 2>&1 || fail 'invalid marker reference'
            service=$(python3 -c '
import json, os, sys
s = json.load(sys.stdin)
r = s.pop("desired_state_ref")
try:
    os.stat(r["file"])
    state = r["when_exists"]
except FileNotFoundError:
    state = r["when_missing"]
s["desired_state"] = state
print(json.dumps(s))
' <<<"$service" 2>/dev/null) || fail 'cannot resolve marker reference'
            resolved=$(jq -c --argjson service "$service" '. + [$service]' <<<"$resolved") || fail 'cannot resolve services'
            continue
        fi
        jq -e '
            (has("desired_state") | not) and
            (.desired_state_ref | type == "object" and
                (.file | type == "string" and length > 0 and (contains("\u0000") | not)) and
                (.pointer | type == "string" and (. == "" or startswith("/"))) and
                (if has("when_empty") or has("when_nonempty") then
                    all(.when_empty, .when_nonempty; . == "running" or . == "stopped")
                 else true end))' <<<"$service" >/dev/null 2>&1 || fail 'invalid desired_state_ref'
        ref=$(jq -c '.desired_state_ref' <<<"$service")
        ref_file=$(jq -r '.file' <<<"$ref")
        desired=$(jq -ser --argjson ref "$ref" '
            if length != 1 then error("expected one JSON document") else .[0] end |
            reduce ($ref.pointer | if . == "" then [] else split("/")[1:] end | .[]) as $part
                (.;
                 if ($part | test("~([^01]|$)")) then error("invalid escape") else . end |
                 ($part | gsub("~1"; "/") | gsub("~0"; "~")) as $key |
                 if type == "object" then
                     if has($key) then .[$key] else error("missing key") end
                 elif type == "array" and ($key | test("^(0|[1-9][0-9]*)$")) then
                     ($key | tonumber) as $index |
                     if $index < length then .[$index] else error("missing index") end
                 else error("invalid traversal") end) |
            if $ref | has("when_empty") then
                if type != "array" then error("expected array")
                elif length == 0 then $ref.when_empty else $ref.when_nonempty end
            else . end |
            if . == "running" or . == "stopped" then . else error("invalid target") end
            ' -- "$ref_file" 2>/dev/null) || fail 'cannot resolve desired_state_ref'
        service=$(jq -c --arg desired "$desired" '.desired_state = $desired | del(.desired_state_ref)' <<<"$service")
    fi
    resolved=$(jq -c --argjson service "$service" '. + [$service]' <<<"$resolved") || fail 'cannot resolve services'
done < <(jq -c '.services[]' <<<"$manifest")
manifest=$(jq -c --argjson resolved "$resolved" '.services = $resolved' <<<"$manifest") || fail 'cannot resolve services'
jq -e '
    .version == 1 and ((.status_protocol // 1) | . == 1 or . == 2) and
    (.services | type == "array") and
    all(.services[];
        (.id | type == "string" and length > 0) and
        ((.prerequisites // []) | type == "array" and all(.[]; type == "string" and length > 0)) and
        ((.desired_state // "running") | . == "running" or . == "stopped") and
        ((if .desired_state == "stopped" then .stopped_check else .health_check end) | type == "string" and length > 0) and
        ((.health_timeout_sec // 5) | type == "number" and . > 0) and
        ((.environment // {}) | type == "object" and all(to_entries[]; (.key | test("^[A-Za-z_][A-Za-z0-9_]*$")) and (.value | type == "string"))) and
        all(.home, .workdir, .user, .environment_file; . == null or type == "string"))
    ' <<<"$manifest" >/dev/null 2>&1 || fail 'invalid services manifest'
mkdir -p -- "$state_dir" "$run_dir" || fail 'cannot create output directories'
exec {lock_fd}>"$run_dir/health.lock" || fail 'cannot open lock'
# Wait for the previous run; contention must not become a health failure.
flock "$lock_fd" || fail 'cannot acquire lock'
rows=$(mktemp "$state_dir/.components.XXXXXX") || fail 'cannot create components file'
status_tmp=$(mktemp "$state_dir/.status.XXXXXX") || { rm -f -- "$rows"; fail 'cannot create status file'; }
probe_dir=$(mktemp -d "$run_dir/.probe.XXXXXX") || fail 'cannot create probe directory'
trap 'rm -f -- "$rows" "$status_tmp"; rm -rf -- "$probe_dir"' EXIT
current_user=$(id -un) || fail 'cannot resolve current user'
protocol=$(jq -r '.status_protocol // 1' <<<"$manifest")
# Both stages use the same isolated environment, user and process-group deadline.
run_probe() {
    local check=$1
    user=$(jq -r --arg default "$current_user" '.user // $default' <<<"$service")
    account=$(getent passwd "$user")
    reason=nonzero
    if [[ -n $account ]]; then
        IFS=: read -r account_name _ account_uid _ _ account_home _ <<<"$account"
        home=$(jq -r --arg default "$account_home" '.home // $default' <<<"$service")
        workdir=$(jq -r --arg default "$home" '.workdir // $default' <<<"$service")
        envfile=$(jq -r '.environment_file // ""' <<<"$service")
        seconds=$(jq -r '.health_timeout_sec // 5' <<<"$service")
        env_args=()
        while IFS= read -r -d '' entry; do env_args+=("$entry"); done < <(
            jq -j '.environment // {} | to_entries[] | .key + "=" + .value + "\u0000"' <<<"$service")
        launcher=()
        allowed=true
        if [[ $account_uid != "$(id -u)" ]]; then
            if [[ $(id -u) == 0 ]] && command -v runuser >/dev/null; then
                launcher=(runuser -u "$account_name" --)
            else
                allowed=false
            fi
        fi
        if $allowed; then
            # A FIFO lets the controller cancel the deadline without orphaning a sleep.
            rm -f -- "$probe_dir/timed-out" "$probe_dir/timer"
            mkfifo "$probe_dir/timer" || fail 'cannot create probe timer'
            exec {timer_fd}<>"$probe_dir/timer"
            result=$(
                exec {lock_fd}>&-
                setsid "${launcher[@]}" env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
                    HOME="$home" USER="$account_name" LOGNAME="$account_name" \
                    /bin/bash --noprofile --norc -c '
                        trap '\''trap "" TERM; while :; do /bin/sleep 1; done'\'' TERM
                        # Load trusted shell assignments without exposing their output.
                        (
                            cd -- "$1" || exit 125
                            if [[ -n $2 ]]; then
                                set -a
                                source "$2" || exit 125
                                set +a
                            fi
                            shift 2
                            check=$1; shift
                            for assignment do export "$assignment" || exit 125; done
                            # A separate protocol descriptor distinguishes a missing
                            # command from an explicit exit 127 without logging output.
                            command_not_found_handle() { printf "missing:" >&3; return 127; }
                            export -f command_not_found_handle
                            /bin/bash --noprofile --norc -c "$check"
                        ) 3>&1 >/dev/null 2>&1 &
                        probe_pid=$!
                        wait "$probe_pid"
                        rc=$?
                        case $rc in
                            0) printf "ready" ;;
                            2) printf "environment" ;;
                            127) printf "exit127" ;;
                            *) printf "nonzero" ;;
                        esac
                    ' health-probe "$workdir" "$envfile" "$check" "${env_args[@]}" 2>/dev/null &
                group_pid=$!
                (
                    if ! IFS= read -r -t "$seconds" -u "$timer_fd"; then
                        : >"$probe_dir/timed-out"
                        kill -TERM -- "-$group_pid" 2>/dev/null
                        sleep 1
                        kill -KILL -- "-$group_pid" 2>/dev/null
                    fi
                ) >/dev/null 2>&1 &
                timer_pid=$!
                wait "$group_pid" 2>/dev/null
                probe_rc=$?
                printf 'done\n' >&"$timer_fd"
                wait "$timer_pid"
                if [[ -f $probe_dir/timed-out ]]; then exit 124; fi
                exit "$probe_rc"
            )
            rc=$?
            exec {timer_fd}>&-
            if [[ $rc == 124 ]]; then
                reason=timeout
            elif [[ $rc == 0 && -n $result ]]; then
                case "$result" in
                    *:ready|ready) reason=ready ;;
                    *missing:exit127) reason=command_missing ;;
                    *:environment|environment) reason=environment ;;
                    *) reason=nonzero ;;
                esac
            fi
        fi
    fi
}
overall=0
while IFS= read -r service; do
    id=$(jq -r '.id' <<<"$service")
    desired=$(jq -r '.desired_state // "running"' <<<"$service")
    reason=ready
    # Stopping must remain verifiable even when running prerequisites are absent.
    if [[ $desired == running ]]; then
        while IFS= read -r prerequisite; do
            run_probe "$(jq -r . <<<"$prerequisite")"
            if [[ $reason != ready ]]; then reason=waiting; break; fi
        done < <(jq -c '.prerequisites // [] | .[]' <<<"$service")
    fi
    if [[ $reason != waiting ]]; then
        run_probe "$(jq -r 'if .desired_state == "stopped" then .stopped_check else .health_check end' <<<"$service")"
    fi
    case $reason in
        ready)
            status=READY
            if [[ $desired == stopped && $protocol == 2 ]]; then status=STOPPED; reason=stopped; fi ;;
        waiting) status=WAITING; [[ $overall == 20 ]] || overall=10 ;;
        *) status=FAILED; overall=20 ;;
    esac
    jq -cn --arg id "$id" --arg status "$status" --arg reason "$reason" \
        '{id:$id,status:$status,reason:$reason}' >>"$rows" || fail 'cannot record component'
done < <(jq -c '.services[]' <<<"$manifest")
case $overall in
    0) status=READY ;;
    10) status=WAITING ;;
    20) status=FAILED ;;
esac
generation=unknown
if [[ -f $run_dir/generation ]]; then
    generation=$(cat -- "$run_dir/generation") || fail 'cannot read generation'
fi
boot_id=$(cat /proc/sys/kernel/random/boot_id) || fail 'cannot read boot id'
checked_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) || fail 'cannot read clock'
previous='{}'
if [[ -f $state_dir/status.json ]]; then
    previous=$(jq -ce 'select(type == "object")' "$state_dir/status.json") || fail 'cannot read previous status'
fi
last_success=$(jq -r '.last_success // ""' <<<"$previous")
failures=$(jq -er '.consecutive_failures // 0 | select(type == "number" and . >= 0 and . == floor)' <<<"$previous") || fail 'invalid failure counter'
if [[ $overall == 20 ]]; then failures=$((failures + 1)); else failures=0; fi
if [[ $overall == 0 ]]; then last_success=$checked_at; fi
jq -s --arg status "$status" --arg generation "$generation" --arg boot_id "$boot_id" \
    --arg config_version "$(jq -r '.version' <<<"$manifest")" --arg checked_at "$checked_at" \
    --arg last_success "$last_success" --argjson consecutive_failures "$failures" \
    '{generation:$generation,boot_id:$boot_id,config_version:$config_version,
      checked_at:$checked_at,last_success:$last_success,consecutive_failures:$consecutive_failures,
      status:$status,components:.}' "$rows" >"$status_tmp" || fail 'cannot encode status'
mv -f -- "$status_tmp" "$state_dir/status.json" || fail 'cannot publish status'
if $json; then cat -- "$state_dir/status.json"; else printf 'status=%s\n' "$status"; fi
exit "$overall"
