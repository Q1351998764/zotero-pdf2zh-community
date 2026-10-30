"""Turn terminal repaint frames into bounded, meaningful progress snapshots."""
from dataclasses import dataclass
import re
import time


_NOT_PROGRESS = re.compile(
    r"(?i)https?://|\b(?:warning|warn|error|exception|failed|failure|timeout|traceback)\b"
    r"|\b\w*(?:Error|Exception|Timeout)\s*:|^\[|^(?:INFO|DEBUG|CRITICAL|FATAL)\b"
)
_STEP = re.compile(r"^([\w .…-]{1,100}?)\s*\((\d+)\s*/\s*(\d+)\)\s+.*?(\d+)\s*/\s*(\d+)(?:\s.*)?$")
_MAIN = re.compile(r"^(translate|Running|Parse)\s+[^a-zA-Z0-9(]*?(\d+)\s*/\s*(\d+)(?:\s.*)?$", re.I)
_TQDM = re.compile(r"^([^|]{0,120}?)\d{1,3}%\|[^|]*\|\s*(\d+)\s*/\s*(\d+)\s*\[.*\]$")


@dataclass(frozen=True)
class ProgressRecord:
    label: str
    current: int
    total: int
    part: tuple[int, int] | None = None
    overall: bool = True

    @property
    def key(self):
        return self.label.casefold(), self.part, self.overall

    @property
    def text(self):
        part = f" ({self.part[0]}/{self.part[1]})" if self.part else ""
        return f"{self.label}{part} {self.current}/{self.total}"


def parse_progress_line(text):
    """Recognize standalone progress rows after ANSI/secret sanitization.

    Diagnostic messages mentioning a fraction are not progress events. Ignore
    animation, elapsed time, speed and changing column padding.
    """
    text = text.strip()
    if _NOT_PROGRESS.search(text):
        return None
    match = _STEP.fullmatch(text)
    if match:
        label, part, parts, current, total = match.groups()
        return ProgressRecord(" ".join(label.split()), int(current), int(total),
                              (int(part), int(parts)), overall=False)
    match = _MAIN.fullmatch(text)
    if match:
        label, current, total = match.groups()
        return ProgressRecord(label, int(current), int(total))
    match = _TQDM.fullmatch(text)
    if match:
        label, current, total = match.groups()
        return ProgressRecord(label.strip().rstrip(": ") or "translate", int(current), int(total))
    return None


class ProgressSampler:
    """Track every update; print at most one snapshot per interval and at exit.

    Rich redraws completed stages together with the active stage. Remember each
    stage independently so old rows do not keep changing the task's message.
    Each instance belongs to one child process, never to the shared terminal.
    """
    def __init__(self, interval=2.0, clock=None):
        self.interval = interval
        self.clock = clock or time.monotonic
        self.seen = {}
        self.overall = None
        self.stage = None
        self.last_printed = ""
        self.last_print_time = None

    def update(self, record):
        if self.seen.get(record.key) == record:
            return None
        self.seen[record.key] = record
        if len(self.seen) > 128:
            self.seen.pop(next(iter(self.seen)))
        if record.overall:
            self.overall = record
        else:
            self.stage = record
        current = self.stage or self.overall
        updates = {"status": "running", "message": current.text}
        if self.overall and self.overall.total > 0:
            updates["progress"] = min(99, int(self.overall.current / self.overall.total * 100))
        return updates

    def snapshot(self, *, force=False):
        text = " | ".join(record.text for record in (self.overall, self.stage) if record)
        if not text or text == self.last_printed:
            return ""
        now = self.clock()
        if not force and self.last_print_time is not None and now - self.last_print_time < self.interval:
            return ""
        self.last_printed, self.last_print_time = text, now
        return text
