#!/usr/bin/env python3
"""Keep historical installer commands on the maintained dependency lifecycle."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from utils.environment_lifecycle import environment_path_entries
from utils.environment_lifecycle import managed_python_env
from utils.environment_lifecycle import resolve_environment_python
from utils.environment_lifecycle import transactional_install_or_update


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="安装或更新已验证的 PDF2zh 兼容依赖组合。")
    parser.add_argument("--env-tool", choices=("uv", "conda"), required=True)
    parser.add_argument("--no-mirror", action="store_true", help="优先使用官方 PyPI。")
    parser.add_argument("--upgrade", action="store_true", help="兼容旧参数；始终更新到本维护版验证的组合。")
    parser.add_argument("--warmup", action="store_true", help="安装成功后预下载引擎资产。")
    args = parser.parse_args(argv)
    next_environment = None
    # The same entry point is used by Server and update_packages.py. There is
    # deliberately no second `pip --upgrade pdf2zh_next` path after this step.
    for engine in ("pdf2zh", "pdf2zh_next"):
        success, _, environment = transactional_install_or_update(
            engine,
            env_tool=args.env_tool,
            preferred_index="https://pypi.org/simple" if args.no_mirror else None,
            require_deepseek_thinking=(engine == "pdf2zh_next"),
        )
        if not success:
            return 1
        if engine == "pdf2zh_next":
            next_environment = environment
    if args.warmup:
        if next_environment is None:
            print("无法找到已安装的 pdf2zh_next 环境。", file=sys.stderr)
            return 1
        python = resolve_environment_python(next_environment)
        env = managed_python_env()
        env["PATH"] = os.pathsep.join(
            [str(path) for path in environment_path_entries(next_environment, args.env_tool)]
            + ([env["PATH"]] if env.get("PATH") else [])
        )
        return subprocess.run(
            [str(python), "-u", str(SERVER_ROOT / "engine_runner.py"), "pdf2zh_next", "--warmup"],
            cwd=SERVER_ROOT,
            env=env,
            check=False,
        ).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
