#!/bin/bash
# The installer restores units; account configuration determines start vs stop.
set -euo pipefail
: "${KEEPALIVE_ROOT:?}" "${KEEPALIVE_CONFIG:?}" "${UNIT:?}"
action=$(jq -er '.accounts | if type != "array" then error("accounts must be an array")
    elif length == 0 then "stop" else "start" end' "$KEEPALIVE_CONFIG")
bash "$KEEPALIVE_ROOT/install-keepalive.sh"
result=$(systemctl show -p Result --value "$UNIT")
if [[ $result == start-limit-hit ]]; then systemctl reset-failed -- "$UNIT"; fi
systemctl "$action" -- "$UNIT"
