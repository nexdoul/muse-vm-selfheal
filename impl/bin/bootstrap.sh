#!/bin/bash
# Install only registered units from the persistent release; never start them.
set -eu
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$base/lib/bootstrap.py" "$@"
