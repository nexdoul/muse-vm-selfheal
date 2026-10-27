#!/bin/bash
set -eu
: "${SSH_CONFIG:?}" "${ALIASES:?}" "${ALIAS_REPAIR_HELPER:?}"
check="$(dirname -- "${BASH_SOURCE[0]}")/ssh-alias-check.sh"
if bash "$check"; then exit 0; fi
# Existing local helper restores keys and aliases from its persistent template.
# Exported ALIASES/SSH_CONFIG are available to helpers that support overrides.
"$ALIAS_REPAIR_HELPER"
bash "$check"
