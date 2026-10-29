"""Preserve managed-engine warning/error records before Rich wraps them.

Only the runner's process and its workers install this handler. The engine's
normal console/progress handlers remain intact. The pipe carries a bounded
single-line JSON envelope, which TaskDiagnostics validates and redacts again
with the actual task credentials. No traceback locals or logging arguments
are serialized, and multiline message continuations are deliberately omitted.
"""
from __future__ import annotations

import json
import logging
import sys

from utils.diagnostics import ENGINE_DIAGNOSTIC_PREFIX, MAX_LINE_CHARS, safe_message


class _EngineDiagnosticHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record):
        try:
            # Original HTTP error messages contain a full URL on the first
            # line; subsequent Rich layout wrapping must not split this data.
            message = safe_message(record.getMessage()).splitlines()
            message = message[0] if message else ""
            if record.exc_info and record.exc_info[1] is not None:
                exception = record.exc_info[1]
                detail = safe_message(f"{type(exception).__name__}: {exception}").splitlines()[0]
                if detail not in message:
                    message += " | " + detail
            event = {"level": record.levelname, "logger": record.name, "message": message}
            line = ENGINE_DIAGNOSTIC_PREFIX + json.dumps(event, ensure_ascii=False)
            if len(line) > MAX_LINE_CHARS:
                event["message"] = "过长的诊断消息已省略（可能包含请求/正文）。"
                line = ENGINE_DIAGNOSTIC_PREFIX + json.dumps(event, ensure_ascii=False)
            # One write prevents Rich from applying terminal width or styling.
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            # logging.Handler.handleError prints raw records/arguments, so do
            # not use it when a provider response or a broken stream is involved.
            pass


def install_engine_diagnostics():
    """Return only newly installed handlers so teardown never removes others."""
    installed = []
    for name in ("babeldoc", "pdf2zh_next"):
        logger = logging.getLogger(name)
        if any(isinstance(handler, _EngineDiagnosticHandler) for handler in logger.handlers):
            continue
        handler = _EngineDiagnosticHandler()
        logger.addHandler(handler)
        installed.append((logger, handler))
    return installed


def remove_engine_diagnostics(installed):
    for logger, handler in installed:
        logger.removeHandler(handler)
        handler.close()
