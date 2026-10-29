"""Run a managed translation engine with explicit, process-local adapters.

Usage: <managed-python> engine_runner.py pdf2zh_next <engine CLI arguments>
The installed console entry point remains the engine's source of CLI behavior.
Only pdf2zh-next receives the response shim; legacy pdf2zh runs unchanged.
The 2.x CLI can log a whole-file failure and nevertheless exit zero, so a small
logging observer preserves that failure as a nonzero exit without hiding logs.
Remove this observer once supported upstream CLIs propagate all file failures.
"""

import importlib.metadata
import logging
import os
import sys

from utils.engine_compat import install_openai_response_compat
from utils.engine_logging import install_engine_diagnostics, remove_engine_diagnostics
from utils.rich_text_compat import install_rich_text_compat

_COMPAT_ENV = "ZOTERO_PDF2ZH_ENGINE_COMPAT"


class _EngineFailureObserver(logging.Handler):
    """Observe known whole-file failure messages, not recoverable warnings."""

    _FAILURE_PREFIXES = (
        "Error translating file ",
        "Translation error:",
        "Subprocess initialization error:",
        "Error in translate_wrapper_async:",
        "Error running async translation:",
    )

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.failed = False

    def emit(self, record):
        if record.name == "pdf2zh_next" or record.name.startswith("pdf2zh_next."):
            if record.getMessage().startswith(self._FAILURE_PREFIXES):
                self.failed = True


def _entry_point(engine):
    matches = list(importlib.metadata.entry_points(group="console_scripts", name=engine))
    if len(matches) != 1:
        raise RuntimeError(
            f"无法唯一定位 {engine} 的命令入口；请检查当前托管 Python 环境的安装。"
        )
    return matches[0].load()


def _exit_code(result):
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    print(result, file=sys.stderr)
    return 1


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"pdf2zh", "pdf2zh_next"}:
        print("用法：engine_runner.py {pdf2zh|pdf2zh_next} [引擎参数...]", file=sys.stderr)
        return 2

    engine, *engine_args = args
    informational = any(flag in engine_args for flag in ("--help", "-h", "--version"))
    previous_argv = sys.argv
    previous_compat = os.environ.get(_COMPAT_ENV)
    observer = _EngineFailureObserver()
    diagnostic_handlers = []
    # Attach to this namespace rather than root: upstream basicConfig remains
    # free to install its normal RichHandler, and messages still propagate.
    engine_logger = logging.getLogger("pdf2zh_next")
    if engine == "pdf2zh_next":
        engine_logger.addHandler(observer)

    try:
        if engine == "pdf2zh_next" and not informational:
            from utils.asset_downloads import prepare_babeldoc_assets
            from utils.asset_repair import ensure_legacy_rapidocr_asset

            diagnostic_handlers = install_engine_diagnostics()
            asset = ensure_legacy_rapidocr_asset()
            if asset["status"] == "failed":
                print(f"[engine_runner] {asset['message']}", file=sys.stderr)
                if asset.get("required"):
                    return 1
            elif asset["status"] == "repaired":
                print(f"[engine_runner] {asset['message']}", file=sys.stderr)
            prepare_babeldoc_assets()
            os.environ[_COMPAT_ENV] = "1"
            install_openai_response_compat()
            install_rich_text_compat()
        else:
            os.environ.pop(_COMPAT_ENV, None)

        command = _entry_point(engine)
        sys.argv = [engine, *engine_args]
        try:
            result = command()
        except SystemExit as exc:
            result = exc.code
        code = _exit_code(result)
        if code == 0 and observer.failed:
            print(
                "[engine_runner] 引擎记录了翻译失败；已将退出状态设为失败，请查看前面的具体错误。",
                file=sys.stderr,
            )
            return 1
        return code
    except Exception as exc:
        # Compatible-response exceptions contain deliberately safe diagnostics;
        # other engine failures retain their exception type for troubleshooting.
        print(f"[engine_runner] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        remove_engine_diagnostics(diagnostic_handlers)
        sys.argv = previous_argv
        if previous_compat is None:
            os.environ.pop(_COMPAT_ENV, None)
        else:
            os.environ[_COMPAT_ENV] = previous_compat
        if engine == "pdf2zh_next":
            engine_logger.removeHandler(observer)


# Windows multiprocessing re-executes this script as __mp_main__ before
# unpickling the translation worker. The explicit marker is scoped to this
# runner and inherited only by its children; it lets those workers install the
# same class adapter before constructing their own OpenAI client. Fork workers
# already inherit the adapter. No sitecustomize/PYTHONPATH injection is needed.
if __name__ == "__mp_main__" and os.environ.get(_COMPAT_ENV) == "1":
    install_engine_diagnostics()
    install_openai_response_compat()
    install_rich_text_compat()


if __name__ == "__main__":
    raise SystemExit(main())
