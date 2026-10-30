"""Capture each child's own output on every OS; never inspect console buffers."""
import codecs
import os
import re
import subprocess
import sys
import threading

from utils.deepseek_thinking import prepare_deepseek_runtime_command
from utils.diagnostics import (
    MAX_LINE_CHARS, IncompleteTranslationError, TaskDiagnostics,
    TranslationProcessError, collect_secrets, safe_message,
)
from utils.environment_lifecycle import managed_python_env
from utils.progress_output import ProgressSampler, parse_progress_line
from utils.task_manager import task_manager

_CONSOLE_LOCK = threading.Lock()


def _detect_terminal_size(default_cols=80, default_rows=24):
    try:
        size = os.get_terminal_size()
        return max(40, size.columns), max(10, size.lines)
    except (OSError, ValueError, AttributeError):
        return default_cols, default_rows


def _apply_terminal_size_env(env, cols, rows):
    env["COLUMNS"] = str(cols)
    env["LINES"] = str(rows)


def _child_terminal_size(cols, rows):
    # The child writes to a pipe, not the visible terminal. Give Rich enough
    # virtual width to keep counters intact beside its longest stage names;
    # only short sampled snapshots are printed in the user's actual window.
    return max(200, int(cols) - 1), max(10, int(rows))


def execute_with_progress(cmd, task_id, args, env_manager, *, cwd=None):
    final_cmd = list(cmd)
    final_env = os.environ.copy()
    cols, rows = _detect_terminal_size()
    cols, rows = _child_terminal_size(cols, rows)
    if getattr(args, "enable_venv", False) and env_manager:
        final_cmd, venv_env = env_manager.get_command_and_env(cmd)
        final_env.update(venv_env)
        final_env = managed_python_env(final_env)
    final_env.update({
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
        "FORCE_COLOR": "1", "FORCE_TERMINAL": "1", "TERM": "xterm-256color",
    })
    final_env.pop("NO_COLOR", None)
    _apply_terminal_size_env(final_env, cols, rows)
    final_cmd = prepare_deepseek_runtime_command(final_cmd, final_env)
    kwargs = {"cwd": cwd} if cwd else {}
    if sys.platform != "win32":
        return _execute_with_pty(final_cmd, final_env, task_id, cols, rows, **kwargs)
    return _execute_with_inherit(final_cmd, final_env, task_id, cols, **kwargs)


def _execute_captured(final_cmd, final_env, task_id, *, cwd=None, log_dir=None):
    diagnostic = TaskDiagnostics(task_id, collect_secrets(final_cmd, final_env), log_dir=log_dir)
    task_manager.update_task(task_id, {"logFile": diagnostic.path.name, "logPath": str(diagnostic.path)})
    process = None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    oversized = False
    progress = ProgressSampler()

    def emit(line, *, progress_snapshot=False):
        safe = diagnostic.record_progress(line) if progress_snapshot else diagnostic.record(line)
        if safe:
            # One atomic line with task identity avoids mixing concurrent output.
            with _CONSOLE_LOCK:
                try:
                    print(f"[{diagnostic.task_id[:8]}] {safe}", flush=True)
                except (OSError, UnicodeError):
                    pass

    def consume(line):
        record = parse_progress_line(safe_message(line, diagnostic.secrets))
        if record is not None:
            updates = progress.update(record)
            if updates and task_id is not None:
                task_manager.update_task(task_id, updates)
            # Live task state is independent of terminal/log sampling. Rich
            # redraws every old stage on each tick; never persist those frames.
            emit(progress.snapshot(), progress_snapshot=True)
        else:
            # Warning/error output bypasses progress sampling completely.
            emit(line)

    def feed(text):
        nonlocal pending, oversized
        # CR progress updates and LF log records are both logical records.
        for part in re.split(r"([\r\n])", text):
            if part in {"\r", "\n"}:
                if oversized:
                    consume("WARNING: 过长的输出行已省略（可能包含请求/正文）。")
                elif pending:
                    consume(pending)
                pending, oversized = "", False
            elif not oversized:
                pending += part
                if len(pending) > MAX_LINE_CHARS:
                    pending, oversized = "", True

    try:
        process = subprocess.Popen(
            final_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=final_env, cwd=cwd, bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        with process.stdout:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                feed(decoder.decode(chunk))
        feed(decoder.decode(b"", final=True))
        if pending and not oversized:
            consume(pending)
        emit(progress.snapshot(force=True), progress_snapshot=True)
        result = diagnostic.finish(process.wait())
    except BaseException as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        emit(progress.snapshot(force=True), progress_snapshot=True)
        diagnostic.record(f"{type(exc).__name__}: {exc}")
        result = diagnostic.finish(-1)
        task_manager.update_task(task_id, {
            "diagnosticSummary": result.summary, "diagnosticTail": result.tail,
        })
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise TranslationProcessError(-1, result) from None
    task_manager.update_task(task_id, {
        "diagnosticSummary": result.summary, "diagnosticTail": result.tail,
    })
    if result.returncode:
        raise TranslationProcessError(result.returncode, result)
    if result.failed_paragraphs:
        # BabelDOC may keep the source paragraph and still create PDFs/exit 0.
        # A file's existence alone does not make that an accepted translation.
        raise IncompleteTranslationError(result)
    return result


def _execute_with_pty(final_cmd, final_env, task_id, cols, rows, *, cwd=None):
    """Compatibility entry point. Pipes also work when the server has no TTY."""
    return _execute_captured(final_cmd, final_env, task_id, cwd=cwd)


def _execute_with_inherit(final_cmd, final_env, task_id, cols, *, cwd=None):
    """Windows uses the same isolated stream as Linux, not the shared console."""
    return _execute_captured(final_cmd, final_env, task_id, cwd=cwd)
