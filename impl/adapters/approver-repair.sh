#!/bin/bash
set -eu
exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/approver.py" repair
