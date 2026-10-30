"""Exercise API errors with a real child but no translation provider calls."""
import contextlib
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from flask import Flask
from pypdf import PdfReader
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

    def run_partial_job(self, *, count=1, async_job=False, create_pdf=True, verify_download=False):
        translator = self.translator_type.__new__(self.translator_type)
        app = Flask(__name__)
        app.add_url_rule('/translatedFile/<path:filename>', 'download', translator.download_file)
        task_id = "api_partial_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            output = Path(directory) / "partial.pdf"
            script = (
                f"import sys; sys.path.insert(0, {str(SERVER)!r}); "
                "import logging; from pathlib import Path; "
                "from utils.engine_logging import install_engine_diagnostics; "
                "install_engine_diagnostics(); "
                "logger = logging.getLogger('babeldoc.format.pdf.document_il.midend.il_translator'); "
                f"[logger.error('Error translating paragraph. ConnectionError: Connection error.') for _ in range({count})]; "
            )
            if create_pdf:
                if verify_download:
                    script += (
                        "from pypdf import PdfWriter; writer = PdfWriter(); "
                        "writer.add_blank_page(width=300, height=300); "
                        f"writer.write({str(output)!r})"
                    )
                else:
                    script += f"Path({str(output)!r}).write_bytes(b'%PDF-1.4\\n%%EOF\\n')"
            completed = threading.Event()

            def worker():
                try:
                    _execute_captured([sys.executable, "-c", script],
                                      {**os.environ, "PYTHONIOENCODING": "utf-8"},
                                      task_id, log_dir=directory)
                    return translator._complete_job_files(task_id, [str(output)], "done")
                finally:
                    completed.set()

            try:
                download_result = {}
                with (
                    patch.object(task_manager, "_delayed_remove"),
                    patch.dict(translator.download_file.__globals__, {"output_folder": directory}),
                ):
                    with app.test_request_context("/translate", json={"asyncJob": async_job}):
                        response, code = translator._start_accepted_job(
                            task_id, {"active": True, "taskId": task_id}, worker, "/translate",
                        )
                        response_payload = response.get_json()
                    self.assertTrue(completed.wait(10), "Real child did not finish")
                    state = task_manager.get_task(task_id)
                    history = next(item for item in task_manager.get_history() if item["taskId"] == task_id)
                    # Exercise the same JSON endpoints the accepted-job client
                    # polls, not only TaskManager's private in-memory fields.
                    with app.test_request_context("/api/tasks"):
                        tasks_payload = translator.get_tasks().get_json()
                    with app.test_request_context("/api/history"):
                        history_payload = translator.get_history().get_json()
                    if verify_download:
                        filename = response_payload["fileList"][0]
                        with app.test_client() as client:
                            downloaded = client.get('/translatedFile/' + filename)
                            try:
                                download_result = {
                                    "download_status": downloaded.status_code,
                                    "download_bytes": downloaded.get_data(),
                                    "original_bytes": output.read_bytes(),
                                }
                            finally:
                                downloaded.close()
                return {
                    "code": code, "response": response_payload,
                    "state": state, "history": history,
                    "task_api": next(item for item in tasks_payload["tasks"] if item.get("taskId") == task_id),
                    "history_api": next(item for item in history_payload["history"] if item["taskId"] == task_id),
                    "pdf_exists": output.exists(), "output_path": str(output),
                    **download_result,
                }
            finally:
                with task_manager.lock:
                    task_manager.active_tasks.pop(task_id, None)
                    task_manager.progress_history[:] = [
                        item for item in task_manager.progress_history if item["taskId"] != task_id
                    ]

    def assert_deliverable_with_warning(self, payload, count, output_path):
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["fileList"], ["partial.pdf"])
        self.assertEqual(payload["filePaths"], [output_path])
        self.assertTrue(payload["warning"])
        self.assertIn(str(count), payload["warning"])
        self.assertIn("保留原文", payload["warning"])
        self.assertNotIn("任务未完成", payload["warning"])
        report = payload["translationReport"]
        self.assertEqual(report["failedParagraphs"], count)
        self.assertEqual(sum(item["count"] for item in report["reasons"]), count)
        self.assertEqual(report["reasons"][0]["code"], "connection")

    def test_sync_partial_pdf_is_delivered_with_warning_and_history(self):
        captured = self.run_partial_job(verify_download=True)
        self.assertEqual(captured["code"], 200)
        self.assertTrue(captured["pdf_exists"])
        for payload in (captured["response"], captured["history"], captured["history_api"]):
            self.assert_deliverable_with_warning(payload, 1, captured["output_path"])
        self.assertEqual(captured["state"]["progress"], 100)
        self.assertFalse(captured["state"]["active"])
        self.assertEqual(captured["state"]["warning"], captured["response"]["warning"])
        self.assertEqual(captured["download_status"], 200)
        self.assertEqual(captured["download_bytes"], captured["original_bytes"])
        self.assertEqual(len(PdfReader(io.BytesIO(captured["download_bytes"])).pages), 1)

    def test_async_partial_pdf_remains_successful_even_with_many_failed_paragraphs(self):
        captured = self.run_partial_job(count=20, async_job=True)
        self.assertEqual(captured["code"], 200)
        self.assertEqual(captured["response"]["status"], "accepted")
        self.assertTrue(captured["pdf_exists"])
        for payload in (captured["state"]["result"], captured["task_api"]["result"],
                        captured["history"], captured["history_api"]):
            self.assert_deliverable_with_warning(payload, 20, captured["output_path"])
        self.assertTrue(captured["state"]["finished"])
        self.assertFalse(captured["state"]["active"])
        self.assertEqual(captured["state"]["progress"], 100)

    def test_paragraph_warning_cannot_turn_missing_pdf_into_success(self):
        captured = self.run_partial_job(create_pdf=False)
        self.assertEqual(captured["code"], 500)
        self.assertFalse(captured["pdf_exists"])
        self.assertEqual(captured["response"]["status"], "error")
        self.assertIn("未生成预期 PDF", captured["response"]["message"])
        self.assertEqual(captured["history"]["status"], "failed")
        self.assertNotIn("fileList", captured["history"])


if __name__ == "__main__":
    unittest.main()
