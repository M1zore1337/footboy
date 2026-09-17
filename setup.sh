#!/usr/bin/env bash
set -euo pipefail

FOOTBOY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${FOOTBOY_PYTHON:-}" ]]; then
    exec "$FOOTBOY_PYTHON" "$FOOTBOY_ROOT/scripts/bootstrap.py" setup "$@"
fi
for candidate in "$FOOTBOY_ROOT/.venv/bin/python" python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
        exec "$candidate" "$FOOTBOY_ROOT/scripts/bootstrap.py" setup "$@"
    fi
done
printf '%s\n' 'Footboy requires Python 3.10+. Install it or set FOOTBOY_PYTHON to its executable.' >&2
exit 1
