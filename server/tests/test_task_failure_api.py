"""Exercise API errors with a real child but no translation provider calls."""
import contextlib
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from flask import Flask
from utils.execute import _execute_captured
from utils.task_manager import task_manager


class TaskFailureAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = runpy.run_path(str(SERVER / "server.py"))
        cls.translator_type = namespace["PDFTranslator"]

    def test_sync_failure_and_history_share_readable_error_and_log(self):
        translator = self.translator_type.__new__(self.translator_type)
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            def worker():
                return _execute_captured(
                    [sys.executable, "-c", "import sys; print('ValueError: invalid model response'); sys.exit(3)"],
                    {**os.environ, "PYTHONIOENCODING": "utf-8"}, "api_failure", log_dir=directory,
                )
            with app.test_request_context("/translate", json={}), patch.object(task_manager, "_delayed_remove"):
                response, code = translator._start_accepted_job("api_failure", {"active": True}, worker, "/translate")
                payload = response.get_json()
            self.assertEqual(code, 500)
            self.assertEqual(payload["exitCode"], 3)
            self.assertIn("invalid model response", payload["message"])
            self.assertTrue(Path(payload["logPath"]).is_file())
            history = next(item for item in task_manager.get_history() if item["taskId"] == "api_failure")
            self.assertEqual(history["logFile"], payload["logFile"])
            self.assertIn("invalid model response", history["error"])

    def test_zero_exit_missing_output_api_exposes_captured_error(self):
        translator = self.translator_type.__new__(self.translator_type)
        app = Flask(__name__)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            def worker():
                _execute_captured(
                    [sys.executable, "-c", "print('TypeError: choices cannot be None'); print('INFO HTTP Request: POST https://example.test HTTP/1.1 200 OK')"],
                    {**os.environ, "PYTHONIOENCODING": "utf-8"}, "api_missing", log_dir=directory,
                )
                return translator._complete_job_files("api_missing", [], "done")
            with app.test_request_context("/translate", json={}), patch.object(task_manager, "_delayed_remove"):
                response, code = translator._start_accepted_job("api_missing", {"active": True}, worker, "/translate")
            self.assertEqual(code, 500)
            self.assertIn("choices cannot be None", response.get_json()["message"])
            self.assertNotIn("200 OK", response.get_json()["message"])
            self.assertEqual(response.get_json()["logFile"], "api_missing.log")


if __name__ == "__main__":
    unittest.main()
