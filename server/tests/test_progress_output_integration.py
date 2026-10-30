"""Real pipe/Rich regressions for quiet progress without losing task updates."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from utils import execute
from utils.execute import _execute_captured
from utils.progress_output import parse_progress_line
from utils.task_manager import task_manager


class ProgressOutputIntegrationTest(unittest.TestCase):
    def test_child_virtual_width_is_independent_of_narrow_server_terminal(self):
        for terminal_width in (40, 79):
            with (
                self.subTest(terminal_width=terminal_width),
                patch.object(execute, "_detect_terminal_size", return_value=(terminal_width, 24)),
                patch.object(execute, "_execute_captured", return_value="captured") as captured,
            ):
                result = execute.execute_with_progress(
                    [sys.executable, "-c", "pass"], "virtual_width",
                    SimpleNamespace(enable_venv=False), None,
                )
                self.assertEqual(result, "captured")
                environment = captured.call_args.args[1]
                self.assertGreaterEqual(int(environment["COLUMNS"]), 200)

    @unittest.skipUnless(importlib.util.find_spec("babeldoc"), "BabelDOC handler needs engine dependencies")
    def test_actual_babeldoc_long_stage_keeps_overall_counter_at_virtual_width(self):
        from babeldoc.main import create_progress_handler
        from rich.console import Console
        from rich.progress import Progress

        buffer = io.StringIO()
        virtual_width, virtual_height = execute._child_terminal_size(40, 24)
        console = Console(file=buffer, width=virtual_width, height=virtual_height,
                          force_terminal=True, force_interactive=True,
                          _environ={"TERM": "xterm-256color"})
        with patch("babeldoc.main.Progress", side_effect=lambda *columns, **kwargs:
                   Progress(*columns, console=console, auto_refresh=False, **kwargs)):
            context, handler = create_progress_handler(SimpleNamespace(use_rich_pbar=True))
        stage = "Parse PDF and Create Intermediate Representation"
        with context:
            handler({"type": "progress_start", "stage": stage,
                     "part_index": 1, "total_parts": 1, "stage_total": 4})
            for current in range(5):
                handler({"type": "progress_update", "stage": stage,
                         "part_index": 1, "total_parts": 1, "stage_total": 4,
                         "stage_current": current, "overall_progress": current * 25})
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", buffer.getvalue())
        rows = [line.strip() for line in re.split(r"[\r\n]", plain) if line.strip()]
        main_rows = [line for line in rows if line.startswith("translate")]
        records = [parse_progress_line(line) for line in main_rows]
        self.assertTrue(records)
        self.assertTrue(all(record is not None and record.overall for record in records), main_rows)
        self.assertEqual({record.total for record in records}, {100})
        self.assertEqual(records[-1].current, 100)
        self.assertTrue(any(stage in line and parse_progress_line(line) is not None for line in rows))

    def run_child(self, script, task_id):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as console:
            task_manager.add_task(task_id, {"taskId": task_id, "active": True, "progress": 0})
            updates = []
            original_update = task_manager.update_task

            def capture_update(identity, changes):
                if identity == task_id:
                    updates.append(dict(changes))
                original_update(identity, changes)

            try:
                with patch.object(task_manager, "update_task", side_effect=capture_update):
                    result = _execute_captured(
                        [sys.executable, "-u", "-c", textwrap.dedent(script)],
                        {**os.environ, "PYTHONIOENCODING": "utf-8", "TERM": "xterm-256color",
                         "FORCE_COLOR": "1", "FORCE_TERMINAL": "1"},
                        task_id, log_dir=directory,
                    )
                state = task_manager.get_task(task_id)
                saved = Path(result.log_path).read_text(encoding="utf-8")
                return result, console.getvalue(), saved, updates, state
            finally:
                with task_manager.lock:
                    task_manager.active_tasks.pop(task_id, None)

    @unittest.skipUnless(importlib.util.find_spec("rich"), "Rich integration needs engine dependencies")
    def test_real_rich_multistage_redraws_are_sampled_and_keep_latest_stage(self):
        # These are the exact columns used by BabelDOC 0.6.2. Completed stages
        # stay on screen and are rendered again during every subsequent frame.
        # The fake Rich clock changes elapsed-time text without slow sleeps.
        script = """
            import sys
            from rich.console import Console
            from rich.progress import (Progress, TextColumn, BarColumn,
                MofNCompleteColumn, TimeElapsedColumn, TimeRemainingColumn)
            clock = [0.0]
            console = Console(file=sys.stdout, width=79, force_terminal=True, force_interactive=True)
            progress = Progress(TextColumn('[progress.description]{task.description}'),
                BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(), TimeRemainingColumn(),
                console=console, auto_refresh=False, get_time=lambda: clock[0])
            main = progress.add_task('translate', total=100)
            with progress:
                layout = progress.add_task('Parse Page Layout (1/1)', total=1)
                progress.update(layout, completed=1, refresh=True)
                progress.update(main, completed=14, refresh=True)
                paragraphs = progress.add_task('Translate Paragraphs (1/1)', total=23)
                for current in [0] * 30 + [1] * 30 + [2] * 30 + [23] * 10:
                    clock[0] += 1
                    progress.update(paragraphs, completed=current, refresh=True)
                    progress.update(main, completed=20 + current * 3, refresh=True)
                console.print('WARNING: useful diagnostic survives redraws', markup=False)
                typeset = progress.add_task('Typesetting (1/1)', total=1)
                progress.update(typeset, completed=1, refresh=True)
                progress.update(main, completed=100, refresh=True)
        """
        result, console, saved, updates, state = self.run_child(script, "rich_progress")
        self.assertEqual(result.returncode, 0)
        # Before the fix the same stream became hundreds of newline records.
        self.assertLessEqual(len(console.splitlines()), 30, console[:1500])
        self.assertLessEqual(len(saved.splitlines()), 35, saved[:1500])
        self.assertIn("translate 100/100", console)
        self.assertIn("translate 100/100", saved)
        self.assertIn("useful diagnostic survives redraws", console)
        self.assertNotIn("\x1b", console)
        self.assertEqual(state["progress"], 99)  # Completion requires verified PDF outputs.
        percentages = [update["progress"] for update in updates if "progress" in update]
        self.assertIn(14, percentages)
        self.assertIn(20, percentages)
        self.assertIn(26, percentages)
        messages = [update["message"] for update in updates if "message" in update]
        first_translation = next(index for index, message in enumerate(messages)
                                 if "Translate Paragraphs" in message)
        self.assertFalse(any("Parse Page Layout" in message
                             for message in messages[first_translation + 1:]), messages[:20])

    def test_warning_and_error_with_fraction_are_not_throttled_or_treated_as_progress(self):
        script = """
            import sys
            sys.stdout.write('translate 1/10\\r')
            sys.stdout.flush()
            print('WARNING: retry needed at translate 9/10')
            print('ValueError: translate 8/10 is not an engine counter')
            sys.stdout.write('translate 2/10\\r')
            sys.stdout.flush()
        """
        _, console, saved, updates, state = self.run_child(script, "warning_progress")
        for message in ("WARNING: retry needed at translate 9/10",
                        "ValueError: translate 8/10 is not an engine counter"):
            self.assertIn(message, console)
            self.assertIn(message, saved)
        percentages = [update["progress"] for update in updates if "progress" in update]
        self.assertEqual(percentages, [10, 20])
        self.assertEqual(state["progress"], 20)
        self.assertLess(console.index("WARNING:"), console.index("translate 2/10"))

    @unittest.skipUnless(importlib.util.find_spec("babeldoc"), "BabelDOC handler needs engine dependencies")
    def test_actual_babeldoc_non_rich_completion_is_preserved_in_console_and_log(self):
        script = """
            from types import SimpleNamespace
            from babeldoc.main import create_progress_handler
            context, handler = create_progress_handler(SimpleNamespace(use_rich_pbar=False))
            stage = 'Parse PDF and Create Intermediate Representation'
            with context:
                for current in range(5):
                    handler({'type': 'progress_update', 'stage': stage,
                             'stage_current': current, 'stage_total': 4,
                             'overall_progress': current * 25})
                handler({'type': 'progress_end', 'stage': stage})
        """
        result, console, saved, updates, state = self.run_child(script, "tqdm_progress")
        self.assertEqual(result.returncode, 0)
        completed = "Parse PDF and Create Intermediate Representation (Complete) 100/100"
        self.assertIn(completed, console)
        self.assertIn(completed, saved)
        self.assertEqual(state["progress"], 99)
        self.assertIn(50, [update["progress"] for update in updates if "progress" in update])
        self.assertLessEqual(len(console.splitlines()), 4, console[:1500])

    def test_exit_flushes_last_pending_counter_without_final_newline(self):
        script = """
            import sys
            for current in range(24):
                sys.stdout.write(f'translate {current}/100 0:00:{current:02} -:--:--\\r')
            sys.stdout.write('translate 23/100 0:00:59 -:--:--')
            sys.stdout.flush()
        """
        _, console, saved, updates, state = self.run_child(script, "final_progress")
        self.assertLessEqual(len(console.splitlines()), 3, console[:1500])
        self.assertIn("translate 23/100", console)
        self.assertIn("translate 23/100", saved)
        self.assertEqual(state["progress"], 23)
        self.assertIn(17, [update["progress"] for update in updates if "progress" in update])


if __name__ == "__main__":
    unittest.main()
