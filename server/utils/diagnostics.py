"""Bounded, task-local diagnostics. Never persist command lines or request bodies."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid
from utils.progress_output import parse_progress_line

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
MAX_LOG_BYTES = 512 * 1024
MAX_LOG_FILES = 100
MAX_LOG_AGE = 7 * 24 * 3600
MAX_LINE_CHARS = 4096
ENGINE_DIAGNOSTIC_PREFIX = "[PDF2ZH_DIAGNOSTIC] "
_LOCK = threading.Lock()
_ACTIVE: set[Path] = set()
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_SECRET_NAME = re.compile(r"api.?key|access.?key|token|secret|password|authorization|credential|auth", re.I)
_SECRET_VALUE = re.compile(
    r'''(?ix)(["']?[\w.-]*(?:api[_-]?key|access[_-]?key|token|secret|password|authorization|credential|auth)[\w.-]*["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)'''
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_PAYLOAD = re.compile(r'''(?ix)(?:["']?(?:
    messages|prompt|content|request_body|response_body|body|paragraph|
    source_text|translated_text|input_value|input|output|llm_api|extra_data|config|
    composition|char_unicode|unicode|typesettingunit|
    llm\s+(?:json\s+)?response(?:\s+after\s+cleaning(?:\s*\(start\))?)?
)["']?\s*[:=])''')
_EXCEPTION = re.compile(r"\b(?:[\w.]+(?:Error|Exception)|ReadTimeout|ConnectTimeout|TimeoutError)\s*:")
_DIAGNOSTIC = re.compile(r"\b(?:ERROR|WARNING|WARN|CRITICAL|FATAL|Traceback)\b|^\s*(?:error|failed|failure|timeout|timed out|retry)\b|^[⚠️❌\s]*(?:失败|错误|超时|重试)")
_ASSET_STATUS = re.compile(r"^\[asset_download\] (?:INFO|WARNING|ERROR)\b")


def _decode_engine_event(line):
    """Validate the bounded warning/error envelope before using any fields."""
    try:
        event = json.loads(line[len(ENGINE_DIAGNOSTIC_PREFIX):])
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or set(event) not in (
        {"level", "logger", "message"}, {"level", "logger", "message", "event_id"},
    ):
        return None
    if "event_id" in event and (
        not isinstance(event["event_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", event["event_id"])
    ):
        return None
    level, name, message = (event[key] for key in ("level", "logger", "message"))
    if not isinstance(level, str) or level not in {"WARNING", "ERROR", "CRITICAL"} or not isinstance(name, str):
        return None
    if not re.fullmatch(r"(?:babeldoc|pdf2zh_next)(?:\.[A-Za-z0-9_]+)*", name):
        return None
    if not isinstance(message, str) or len(message.splitlines()) > 1 or "\n" in message or "\r" in message:
        return None
    return event


def _format_engine_event(event, secrets=()):
    return (f"[engine] {event['level']} {event['logger']}: "
            f"{safe_message(event['message'], secrets)}")[:MAX_LINE_CHARS]


def decode_engine_diagnostic(line, secrets=()):
    """Decode a validated pre-Rich record and apply task-specific redaction."""
    event = _decode_engine_event(line)
    return _format_engine_event(event, secrets) if event is not None else ""


def _is_final_paragraph_failure(event):
    # The LLM-only translator can recover through simple translation, so its
    # errors and fallback notices must never be counted as final failures.
    return (
        event is not None
        and event["logger"] == "babeldoc.format.pdf.document_il.midend.il_translator"
        and event["level"] in {"ERROR", "CRITICAL"}
        and event["message"].startswith("Error translating paragraph.")
    )


def redact(text, secrets=()):
    text = _ANSI.sub("", str(text)).replace("\x00", "")
    for value in sorted(set(secrets), key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "[REDACTED]")
    # Both quoted JSON headers and plain Authorization: Bearer ... are common.
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "[AUTH REDACTED]", text)
    text = re.sub(r"\b(?:sk-|nvapi-)[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = _SECRET_VALUE.sub(r"\1[REDACTED]", text)

    def safe_url(match):
        try:
            parsed = urlsplit(match.group())
            host = parsed.hostname or ""
            if ":" in host:
                host = "[" + host + "]"
            if parsed.port:
                host += ":" + str(parsed.port)
            return urlunsplit((parsed.scheme, host, parsed.path, "[REDACTED]" if parsed.query else "", ""))
        except ValueError:
            return "[URL REDACTED]"
    return _URL.sub(safe_url, text)


def safe_message(text, secrets=()):
    """Exclude common request/config dumps, even when embedded in an exception."""
    result = []
    for line in redact(text, secrets).splitlines():
        # BabelDOC logs input/output text and IL object reprs even at WARNING
        # level. These are payload boundaries, not useful diagnostic details.
        # Preserve resource URLs whose *path* happens to contain e.g. input:.
        urls = [(url.start(), url.end()) for url in _URL.finditer(line)]
        match = next((candidate for candidate in _PAYLOAD.finditer(line)
                      if not any(start <= candidate.start() < end for start, end in urls)), None)
        if match:
            line = line[:match.start()] + "[请求/正文内容已省略]"
        result.append(line[:MAX_LINE_CHARS])
        if match:
            # A multiline payload can continue without a repeated field label.
            break
    return "\n".join(result)


def collect_secrets(cmd, env):
    values = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET_NAME.search(str(key)) and isinstance(item, str):
                    values.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(env)
    for index, part in enumerate(cmd):
        if part.startswith("--") and _SECRET_NAME.search(part.split("=", 1)[0]):
            if "=" in part:
                values.append(part.split("=", 1)[1])
            elif index + 1 < len(cmd):
                values.append(cmd[index + 1])
        if part in {"--config", "--config-file"} and index + 1 < len(cmd):
            path = Path(cmd[index + 1])
            try:
                if path.stat().st_size > 1024 * 1024:
                    continue
                with path.open("rb") as handle:
                    if path.suffix.lower() == ".json":
                        visit(json.load(handle))
                    else:
                        import tomllib
                        visit(tomllib.load(handle))
            except (OSError, ValueError, ImportError):
                pass
    return tuple(str(value) for value in values if len(str(value)) >= 4)


def error_summary(lines):
    candidates = []
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith(("File ", "Traceback", "^")):
            continue
        # Routine final HTTP access logs must never mask an earlier exception.
        if re.search(r"HTTP Request:|HTTP/\d", line, re.I):
            if not re.search(r"\b[45]\d\d\b", line):
                continue
            score = 1
        elif _EXCEPTION.search(line):
            score = 5
        elif re.search(r"\b(?:ERROR|CRITICAL|FATAL|failed|failure|timeout|timed out)\b|失败|错误|超时", line, re.I):
            score = 3
        else:
            continue
        # Prefer an intact pre-Rich record over its later wrapped console copy.
        if line.startswith("[engine] "):
            score += 1
        candidates.append((score, index, line))
    if not candidates:
        return ""
    return max(candidates)[2].strip()[:1000]


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    log_path: str
    summary: str
    tail: str
    failed_paragraphs: int = 0


class TranslationProcessError(subprocess.CalledProcessError):
    def __init__(self, returncode, result):
        # Do not retain the original command, which can contain API credentials.
        super().__init__(returncode, "translation engine", stderr=result.tail)
        self.log_path = result.log_path
        self.summary = result.summary or f"翻译引擎退出码 {returncode}，未输出可识别的错误。"

    def __str__(self):
        return self.summary


class IncompleteTranslationError(TranslationProcessError):
    """The child exited zero after leaving failed paragraphs untranslated."""
    def __init__(self, result):
        super().__init__(1, result)
        self.failed_paragraphs = result.failed_paragraphs


class MissingOutputError(RuntimeError):
    def __init__(self, summary, log_path=""):
        self.log_path = log_path
        self.summary = "翻译引擎未生成预期 PDF。" + (f" 原因：{summary}" if summary else " 日志中没有可识别的具体错误，请检查输出选项。")
        super().__init__(self.summary)


def missing_output_error(task_id, result=None):
    if result is not None:
        return MissingOutputError(result.summary, result.log_path)
    from utils.task_manager import task_manager
    task = task_manager.get_task(task_id) or {}
    return MissingOutputError(task.get("diagnosticSummary", ""), task.get("logPath", ""))


class TaskDiagnostics:
    """One bounded log per task; multiple attempts append and are separated."""
    def __init__(self, task_id=None, secrets=(), log_dir=None, max_bytes=MAX_LOG_BYTES):
        self.task_id = str(task_id or uuid.uuid4())
        self.secrets = secrets
        self.max_bytes = max_bytes
        self.lines = deque(maxlen=80)
        self.errors = deque(maxlen=40)
        self.last_line = None
        self.failed_paragraphs = 0
        self.paragraph_failure_summary = ""
        self._paragraph_failure_ids = set()
        directory = Path(log_dir) if log_dir else LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r"[^a-zA-Z0-9_-]", "_", self.task_id)[:100] or str(uuid.uuid4())
        self.path = directory / f"{filename}.log"
        with _LOCK:
            _ACTIVE.add(self.path)
            self._prune(directory)
        self._append(f"=== {datetime.now().isoformat()} task={filename} 翻译诊断开始；不记录命令/配置/正文 ===\n")

    @staticmethod
    def _prune(directory):
        files = sorted(directory.glob("*.log*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            active_base = Path(str(path).removesuffix(".1"))
            if active_base in _ACTIVE:
                continue
            try:
                if index >= MAX_LOG_FILES or time.time() - path.stat().st_mtime > MAX_LOG_AGE:
                    path.unlink()
            except OSError:
                pass

    def _append(self, text):
        encoded = text.encode("utf-8", errors="replace")
        with _LOCK:
            if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                self.path.replace(Path(str(self.path) + ".1"))
            with self.path.open("ab") as handle:
                handle.write(encoded)

    def record(self, line):
        return self._record(line)

    def record_progress(self, snapshot):
        """Record a parsed progress snapshot, still applying all redaction.

        Normalized snapshots no longer have the original Rich/tqdm syntax.
        Only the progress sampler should use this explicit entry point.
        """
        return self._record(snapshot, is_progress=True)

    def _record(self, line, *, is_progress=False):
        line = str(line).strip()
        structured = line.startswith(ENGINE_DIAGNOSTIC_PREFIX)
        if structured:
            event = _decode_engine_event(line)
            line = _format_engine_event(event, self.secrets) if event is not None else ""
            if _is_final_paragraph_failure(event):
                event_id = event.get("event_id")
                if event_id is not None:
                    # QueueHandler forwards the worker's LogRecord to the
                    # parent. Its second envelope is the same failure event.
                    if event_id in self._paragraph_failure_ids:
                        return ""
                    self._paragraph_failure_ids.add(event_id)
                # Different paragraphs often have identical redacted messages.
                # Count actual structured events before display deduplication;
                # Rich's plain-text replay never enters this branch.
                self.failed_paragraphs += 1
                self.paragraph_failure_summary = error_summary([line])
        else:
            line = safe_message(line, self.secrets).strip()
        if not line or line == self.last_line:
            return ""
        # Source text, Rich traceback local-variable dumps and full configs are
        # intentionally excluded. This is a diagnostic log, not a raw transcript.
        if not (is_progress or structured or _ASSET_STATUS.match(line)
                or _DIAGNOSTIC.search(line[:180]) or _EXCEPTION.search(line[:180])
                or parse_progress_line(line) is not None
                or re.search(r'HTTP(?: Request:|/\d).*?\b[45]\d\d\b', line)
                or re.match(r'^File "[^\"]+", line \d+', line)):
            return ""
        self.last_line = line
        self.lines.append(line)
        if error_summary([line]):
            self.errors.append(line)
        self._append(line + "\n")
        return line

    def finish(self, returncode):
        summary = self.paragraph_failure_summary or error_summary(self.errors)
        failure_count = ""
        if self.failed_paragraphs:
            summary = (
                f"有 {self.failed_paragraphs} 个段落翻译失败并保留原文，任务未完成。"
                "可能生成的部分 PDF 保留在后端输出目录。"
                + (f" 原因：{summary}" if summary else "")
            )
            failure_count = f" paragraph_failures={self.failed_paragraphs}"
        self._append(f"=== exit={returncode}{failure_count}" + (f" 摘要：{summary}" if summary else "") + " ===\n")
        with _LOCK:
            _ACTIVE.discard(self.path)
        return ExecutionResult(returncode, str(self.path), summary, "\n".join(self.lines)[-12000:],
                               failed_paragraphs=self.failed_paragraphs)


def record_task_error(task_id, exc):
    """Save setup/post-processing failures too, without printing raw tracebacks."""
    summary = safe_message(getattr(exc, "summary", "") or str(exc))[:2000] or type(exc).__name__
    if getattr(exc, "log_path", ""):
        return summary, exc.log_path
    diagnostic = TaskDiagnostics(task_id)
    diagnostic.record(f"{type(exc).__name__}: {summary}")
    result = diagnostic.finish(-1)
    return summary, result.log_path
