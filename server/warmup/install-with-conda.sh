#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v conda >/dev/null 2>&1; then
    echo "Conda was not found. Open an Anaconda/Miniconda terminal and retry." >&2
    exit 1
fi
exec conda run --no-capture-output -n base python "$script_dir/install_managed.py" --env-tool conda "$@"
