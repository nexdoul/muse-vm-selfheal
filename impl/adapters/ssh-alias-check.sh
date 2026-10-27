#!/bin/bash
set -eu
: "${SSH_CONFIG:?}" "${ALIASES:?}"
read -r -a names <<<"$ALIASES"
((${#names[@]})) || exit 1
for alias in "${names[@]}"; do
    [[ $alias =~ ^[a-zA-Z0-9_-]+$ ]] || exit 1
    grep -qE "^Host[[:space:]]+$alias([[:space:]]|$)" "$SSH_CONFIG" || exit 1
done
