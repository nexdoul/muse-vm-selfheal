#!/bin/bash
set -eu
: "${UNIT:?}" "${PROBE_BIN:?}" "${SECRET_FILE:?}" "${INSTALL_SCRIPT:?}"
load=$(systemctl show -p LoadState --value "$UNIT") || exit 1
case "$load" in
    loaded)
        if ! systemctl is-active --quiet "$UNIT"; then
            result=$(systemctl show -p Result --value "$UNIT") || exit 1
            if [ "$result" = start-limit-hit ]; then systemctl reset-failed -- "$UNIT"; fi
            systemctl start -- "$UNIT"
        fi ;;
    not-found)
        # The sanitized installer consumes an existing protected config, never a CLI secret.
        bash "$INSTALL_SCRIPT" ;;
    *) exit 1 ;;
esac
