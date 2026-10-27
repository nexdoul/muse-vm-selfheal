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

# Site identity cannot come from the hostname: both Muse VMs report
# "htch-runtime", so a hostname-derived template directory never exists and the
# shared fallback would install the wrong host's units.  Read the site file,
# which deploy writes on the persistent volume.
site="${SITE:-}"
if [[ -z $site && -r $SELFHEAL/config/site.json ]]; then
    site=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("site",""))' \
        "$SELFHEAL/config/site.json" 2>/dev/null || true)
fi
if [[ -z $site ]]; then
    printf 'boot-ensure: cannot determine site (no SITE and no %s)\n' \
        "$SELFHEAL/config/site.json" >&2
    exit 2
fi
# Templates are per site: keepalive/sshd units differ between hosts, so a shared
# directory would install the wrong variant.  A site directory that is missing
# is an error, not a reason to fall back silently.
unit_dir="$B/units-$site"
if [[ ! -d $unit_dir ]]; then
    printf 'boot-ensure: no unit templates for site %s (%s)\n' "$site" "$unit_dir" >&2
    exit 2
fi
"$B/bin/bootstrap.sh" --services "${SERVICES:-$B/config/services.json}" \
    --unit-dir "$unit_dir" 9>&-
# Do not wait for controller startup: it runs bootstrap again under its own lock.
systemctl --no-block start muse-selfheal-controller.service 9>&-
