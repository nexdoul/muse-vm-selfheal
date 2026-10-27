#!/bin/bash
# Manifest-driven controller loop: one reconcile per interval.
# Owned by the controller unit; reconcile itself is single-shot.
set -u
RELEASES="${RELEASES:-/home/hatch/selfheal-releases}"
SELFHEAL="${SELFHEAL:-/home/hatch/selfheal}"
INTERVAL="${INTERVAL:-30}"
R=$(readlink -f "$RELEASES/current") || exit 2
B="$R/impl"
mkdir -p "$SELFHEAL/state" "$SELFHEAL/run" "$SELFHEAL/logs"

# Site identity cannot come from the hostname: both Muse VMs report
# "htch-runtime", so a hostname-derived manifest name never exists.  Resolve in
# priority order and fail loudly instead of looping on a missing file.
site="${SITE:-}"
if [[ -z $site && -r $SELFHEAL/config/site.json ]]; then
    site=$(jq -r '.site // ""' "$SELFHEAL/config/site.json" 2>/dev/null)
fi
services="${SERVICES:-}"
if [[ -z $services ]]; then
    # A rendered manifest is authoritative: deploy writes it for this host and
    # it is already placeholder-free.  Site-named templates are render inputs,
    # not runnable manifests.
    if [[ -r $B/config/services.json ]]; then
        services="$B/config/services.json"
    elif [[ -n $site && -r $B/config/services.$site.json ]]; then
        services="$B/config/services.$site.json"
    else
        # No manifest is resolvable from names, but bootstrap owns the decision
        # and reports a precise error; failing here would hide which path was
        # tried.  Use the conventional name and let it fail loudly.
        services="$B/config/services.json"
    fi
fi

# Templates are per site (keepalive/sshd units differ between hosts).  A shared
# fallback would install the wrong host's units, so require the site directory.
unit_dir="$B/units-$site"
if [[ -z $site || ! -d $unit_dir ]]; then
    echo "controller: no unit templates for site '$site' ($unit_dir)" >>"$SELFHEAL/logs/controller.log"
    exit 2
fi

# A failed bootstrap must not enter reconciliation with partially restored units.
# The controller unit restarts us; direct callers receive the failure explicitly.
"$B/bin/bootstrap.sh" --services "$services" --unit-dir "$unit_dir" \
    >> "$SELFHEAL/logs/controller.log" 2>&1 || exit 2
while true; do
    "$B/bin/reconcile.sh" --services "$services" \
        --state-dir "$SELFHEAL/state" --run-dir "$SELFHEAL/run" \
        >> "$SELFHEAL/logs/controller.log" 2>&1
    rc=$?
    echo "[$(date -u +%FT%TZ)] reconcile rc=$rc" >> "$SELFHEAL/logs/controller.log"
    sleep "$INTERVAL"
done
