#!/bin/bash
# Persistent platform entry: independent of /etc units and home-init's started flag.
# Installs the units this site's manifest registers, then starts the controller.
set -euo pipefail
RELEASES="${RELEASES:-/home/hatch/selfheal-releases}"
SELFHEAL="${SELFHEAL:-/home/hatch/selfheal}"
mkdir -p "$SELFHEAL/run"
exec 9>"$SELFHEAL/run/boot-ensure.lock"
flock -n 9 || exit 10
B=$(readlink -f "$RELEASES/current")/impl
# Templates are per site: keepalive/sshd units differ between hosts, so a
# shared directory would install the wrong variant.  Fall back to the shared
# directory when the site has no dedicated one.
site="${SITE:-$(hostname -s)}"
unit_dir="$B/units-$site"
[[ -d $unit_dir ]] || unit_dir="$B/units"
"$B/bin/bootstrap.sh" --services "${SERVICES:-$B/config/services.json}" --unit-dir "$unit_dir" 9>&-
# Do not wait for controller startup: it runs bootstrap again under its own lock.
systemctl --no-block start muse-selfheal-controller.service 9>&-
