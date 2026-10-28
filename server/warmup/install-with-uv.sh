#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv was not found. Install uv and reopen the terminal." >&2
    exit 1
fi
exec uv run --python 3.12 --with packaging "$script_dir/install_managed.py" --env-tool uv "$@"
