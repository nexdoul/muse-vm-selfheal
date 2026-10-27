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
while true; do
    "$B/bin/reconcile.sh" --services "$B/config/services.${SITE:-$(hostname -s)}.json" \
        --state-dir "$SELFHEAL/state" --run-dir "$SELFHEAL/run" \
        >> "$SELFHEAL/logs/controller.log" 2>&1
    rc=$?
    echo "[$(date -u +%FT%TZ)] reconcile rc=$rc" >> "$SELFHEAL/logs/controller.log"
    sleep "$INTERVAL"
done
