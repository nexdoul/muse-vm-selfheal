#!/bin/bash
# /home/hatch/init.sh — platform home-init hook: runs once after each sandbox boot.
# Runs as root with cwd /home/hatch. Must be executable (chmod 700) to take effect.
#
# Two independent responsibilities, in this order:
#   1. selfheal unit bootstrap — reinstalls this site's systemd units from the
#      persistent release and starts the controller.  This must run first: a
#      reset wipes /etc/systemd/system, so no unit exists to be repaired until
#      bootstrap recreates it.
#   2. restore-all.sh — legacy restore (cron, cf-probe, MuseAutoApprove, nginx,
#      Crypto_Auto).  It no longer owns the selfheal units.
#
# Every step is best-effort and must not block the others: a failure in one
# layer must not leave the host with no self-healing at all.  home-init only
# fires once per boot, so a hard exit here means no retry until the next reset.
set -uo pipefail

# HOME hardening: when HOME lacks the setup tree but /home/hatch has it, fix it.
if [ ! -d "${HOME:-}/workspace/setup" ] && [ -d /home/hatch/workspace/setup ]; then
  export HOME=/home/hatch
fi

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

# --- 1. selfheal unit bootstrap (must run before anything that needs a unit) ---
RELEASES=/home/hatch/selfheal-releases
if [ -x "$RELEASES/current/impl/bin/boot-ensure.sh" ]; then
  # Do not pass SITE: both Muse VMs report hostname "htch-runtime", so a
  # hostname-derived site names a template directory that does not exist.
  # boot-ensure.sh reads the authoritative site from selfheal/config/site.json.
  if out=$(RELEASES="$RELEASES" SELFHEAL=/home/hatch/selfheal \
      bash "$RELEASES/current/impl/bin/boot-ensure.sh" 2>&1); then
    log "selfheal bootstrap ok: $(tr '\n' ';' <<<"$out")"
  else
    log "WARN: selfheal bootstrap failed: $(tr '\n' ';' <<<"$out")"
  fi
else
  log "WARN: boot-ensure.sh missing under $RELEASES/current/impl/bin"
fi

# --- 2. Crypto_Auto supervisor (failure must not block the shared restore) ---
/home/hatch/code/Crypto_Auto/deploy/usmuse/boot.sh \
  >>/home/hatch/code/Crypto_Auto/data/usmuse/boot.log 2>&1 || true

# --- 3. legacy restore ---
RESTORE=/home/hatch/workspace/setup/restore-all.sh
if [ ! -x "$RESTORE" ]; then
  log "ERROR: $RESTORE missing or not executable; skipping legacy restore"
  exit 1
fi

# exec keeps restore-all.sh's own logging and exit status intact.
exec "$RESTORE"
