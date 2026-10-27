#!/bin/bash
# Install only registered units from the persistent release; never start them.
# Defaults resolve from the site file so callers need not repeat the layout.
set -eu
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
site="${SITE:-}"
if [[ -z $site ]]; then
    for f in "${SELFHEAL:-/home/hatch/selfheal}/config/site.json" "$base/config/site.json"; do
        if [[ -r $f ]]; then site=$(jq -r '.site // ""' "$f" 2>/dev/null); break; fi
    done
fi
args=()
[[ -d $base/units-$site && -n $site ]] && args+=(--unit-dir "$base/units-$site")
if [[ ! -r $base/config/services.json && -n $site && -r $base/config/services.$site.json ]]; then
    args+=(--services "$base/config/services.$site.json")
fi
exec python3 "$base/lib/bootstrap.py" "${args[@]}" "$@"
