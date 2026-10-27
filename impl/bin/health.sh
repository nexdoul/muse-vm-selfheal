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
jq -e '
    .version == 1 and (.services | type == "array") and
    all(.services[];
        (.id | type == "string" and length > 0) and
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
overall=0
while IFS= read -r service; do
    id=$(jq -r '.id' <<<"$service")
    user=$(jq -r --arg default "$current_user" '.user // $default' <<<"$service")
    account=$(getent passwd "$user")
    reason=nonzero
    if [[ -n $account ]]; then
        IFS=: read -r account_name _ account_uid _ _ account_home _ <<<"$account"
        home=$(jq -r --arg default "$account_home" '.home // $default' <<<"$service")
        workdir=$(jq -r --arg default "$home" '.workdir // $default' <<<"$service")
        envfile=$(jq -r '.environment_file // ""' <<<"$service")
        check=$(jq -r 'if .desired_state == "stopped" then .stopped_check else .health_check end' <<<"$service")
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
                    *) reason=nonzero ;;
                esac
            fi
        fi
    fi
    status=FAILED
    if [[ $reason == ready ]]; then status=READY; else overall=20; fi
    jq -cn --arg id "$id" --arg status "$status" --arg reason "$reason" \
        '{id:$id,status:$status,reason:$reason}' >>"$rows" || fail 'cannot record component'
done < <(jq -c '.services[]' <<<"$manifest")
status=READY
[[ $overall == 0 ]] || status=FAILED
jq -s --arg status "$status" '{status:$status,components:.}' "$rows" >"$status_tmp" || fail 'cannot encode status'
mv -f -- "$status_tmp" "$state_dir/status.json" || fail 'cannot publish status'
if $json; then cat -- "$state_dir/status.json"; else printf 'status=%s\n' "$status"; fi
exit "$overall"
