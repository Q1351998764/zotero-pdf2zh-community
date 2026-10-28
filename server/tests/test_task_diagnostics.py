import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from utils.diagnostics import (TaskDiagnostics, TranslationProcessError,
                               collect_secrets, missing_output_error, redact)
from utils.execute import _execute_captured
from utils.task_manager import TaskManager, task_manager


class TaskDiagnosticsTests(unittest.TestCase):
    def run_child(self, directory, task_id, script, **env_extra):
        task_manager.add_task(task_id, {"taskId": task_id, "active": True, "progress": 0})
        return _execute_captured(
            [sys.executable, "-u", "-c", script],
            {**os.environ, "PYTHONIOENCODING": "utf-8", **env_extra},
            task_id, log_dir=directory,
        )

    def test_real_child_progress_across_chunks_and_success(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            result = self.run_child(directory, "progress", "import sys,time; sys.stdout.write('trans'); sys.stdout.flush(); time.sleep(.03); sys.stdout.write('late 1/2\\rtranslate 2/2\\n'); sys.stdout.flush()")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(task_manager.get_task("progress")["progress"], 99)
            self.assertIn("translate 2/2", Path(result.log_path).read_text(encoding="utf-8"))

    def test_nonzero_preserves_error_and_redacts_stdout_stderr(self):
        secret = "private-provider-credential-unique"
        script = "import sys,os; print('ERROR Authorization: Bearer '+os.environ['OPENAI_API_KEY']); print('ValueError: model response choices is null',file=sys.stderr); print('INFO HTTP Request: POST https://user:pass@example.test/v1/chat?api_key=privatequery HTTP/1.1 200 OK'); sys.exit(7)"
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as console:
            with self.assertRaises(TranslationProcessError) as raised:
                self.run_child(directory, "bad", script, OPENAI_API_KEY=secret)
            error = raised.exception
            self.assertEqual(error.returncode, 7)
            self.assertIn("choices is null", str(error))
            text = Path(error.log_path).read_text(encoding="utf-8") + console.getvalue() + str(error)
            for value in (secret, "privatequery", "user:pass"):
                self.assertNotIn(value, text)
            self.assertNotIn(script, str(error))

    def test_zero_exit_without_files_uses_prior_error_not_last_http_line(self):
        script = "print('ERROR Translation failed'); print('TypeError: response choices is None'); print('INFO HTTP Request: POST https://example.test/v1 HTTP/1.1 200 OK')"
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            result = self.run_child(directory, "ignored", script)
            error = missing_output_error("ignored", result)
            self.assertIn("choices is None", str(error))
            self.assertNotIn("200 OK", str(error))
            self.assertEqual(error.log_path, result.log_path)
            # Association also survives a caller that did not retain the result.
            self.assertIn("choices is None", str(missing_output_error("ignored")))

    def test_no_raw_body_config_or_oversized_lines(self):
        script = "print('A private paper paragraph without diagnostics.'); print('ERROR request_body={\"messages\":[{\"content\":\"PRIVATE_PAPER\"}]}'); print('X'*10000+'PRIVATE_END'); print('RuntimeError: safe diagnostic')"
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as console:
            result = self.run_child(directory, "private", script)
            text = Path(result.log_path).read_text(encoding="utf-8") + console.getvalue()
            self.assertNotIn("PRIVATE_PAPER", text)
            self.assertNotIn("PRIVATE_END", text)
            self.assertNotIn("private paper paragraph", text)
            self.assertIn("safe diagnostic", result.summary)

    def test_concurrent_tasks_have_separate_logs(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.run_child, directory, name,
                                      f"import time; print('RuntimeError: {name}'); time.sleep(.05)")
                           for name in ("task_alpha", "task_beta")]
                first, second = [future.result() for future in futures]
            self.assertNotIn("task_beta", Path(first.log_path).read_text(encoding="utf-8"))
            self.assertNotIn("task_alpha", Path(second.log_path).read_text(encoding="utf-8"))

    def test_log_rotation_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = TaskDiagnostics("rotate", log_dir=directory, max_bytes=512)
            for number in range(60):
                diagnostic.record(f"WARNING: repeated attempt {number} " + "x" * 60)
            result = diagnostic.finish(1)
            files = list(Path(directory).glob("*.log*"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(path.stat().st_size <= 512 for path in files))
            self.assertTrue(Path(result.log_path).exists())

    def test_config_and_environment_secrets_are_recognized(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"providers": [{"apiKey": "config-secret-value"}]}))
            values = collect_secrets(["python", "--config", str(config), "--api-key=cli-secret-value"], {"ACCESS_TOKEN": "env-secret-value"})
            text = redact("config-secret-value cli-secret-value env-secret-value", values)
            self.assertEqual(text, "[REDACTED] [REDACTED] [REDACTED]")

    def test_history_keeps_message_and_log_but_not_full_tail(self):
        manager = TaskManager()
        manager.add_task("id", {"active": True, "message": "running", "logFile": "id.log", "logPath": "/logs/id.log", "diagnosticTail": "details"})
        with patch.object(manager, "_delayed_remove"):
            manager.complete_task("id", "failed", "ValueError: choices is null", error="api_key=privatekey")
        history = manager.get_history()[0]
        self.assertEqual(history["logFile"], "id.log")
        self.assertIn("choices is null", history["message"])
        self.assertNotIn("privatekey", history["error"])
        self.assertNotIn("diagnosticTail", history)


if __name__ == "__main__":
    unittest.main()
