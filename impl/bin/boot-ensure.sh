#!/bin/bash
# Persistent platform entry: independent of /etc units and home-init's started flag.
set -euo pipefail
RELEASES="${RELEASES:-/home/hatch/selfheal-releases}"
SELFHEAL="${SELFHEAL:-/home/hatch/selfheal}"
mkdir -p "$SELFHEAL/run"
exec 9>"$SELFHEAL/run/boot-ensure.lock"
flock -n 9 || exit 10
B=$(readlink -f "$RELEASES/current")/impl
"$B/bin/bootstrap.sh" --services "${SERVICES:-$B/config/services.json}" --unit-dir "$B/units" 9>&-
# Do not wait for controller startup: it runs bootstrap again under its own lock.
systemctl --no-block start muse-selfheal-controller.service 9>&-
