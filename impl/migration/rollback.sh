#!/bin/bash
set -euo pipefail
base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$base/controller.py" rollback "$@"
