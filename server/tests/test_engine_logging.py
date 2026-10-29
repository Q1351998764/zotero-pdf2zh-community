import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from utils.diagnostics import ENGINE_DIAGNOSTIC_PREFIX, TaskDiagnostics, safe_message
from utils.engine_logging import install_engine_diagnostics, remove_engine_diagnostics
from utils.execute import _execute_captured


class EngineLoggingTest(unittest.TestCase):
    def setUp(self):
        self.handlers = install_engine_diagnostics()
        self.addCleanup(remove_engine_diagnostics, self.handlers)

    def test_unwrapped_resource_message_survives_with_task_secret_redaction(self):
        url = "https://models.example.test/resolve/main/onnx/PP-OCRv4/ch_PP-OCRv4_det_infer.onnx"
        key = "test-provider-secret-without-prefix"
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            logging.getLogger("babeldoc.assets.assets").warning(
                "Network error occurred: Client error '404 Not Found' for url '%s' opaque=%s",
                url, key,
            )
        records = stream.getvalue().splitlines()
        self.assertEqual(len(records), 1)
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = TaskDiagnostics("resource", secrets=(key,), log_dir=directory)
            diagnostic.record(records[0])
            # A later, narrower Rich copy cannot replace the full error summary.
            diagnostic.record("WARNING Network error occurred: Client error '404 Not")
            diagnostic.record("                 Found' for url")
            diagnostic.record("                 'https://models.example.test/resolve/main/")
            result = diagnostic.finish(1)
            text = Path(result.log_path).read_text(encoding="utf-8")
        self.assertIn(url, result.summary)
        self.assertIn(url, text)
        self.assertNotIn(key, text)
        self.assertNotIn(ENGINE_DIAGNOSTIC_PREFIX, text)

    def test_multiline_payload_and_traceback_locals_are_not_serialized(self):
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            logger = logging.getLogger("pdf2zh_next.high_level")
            logger.error("Failure: request_body={'content': 'PRIVATE_BODY'}\nPRIVATE_CONTINUATION")
            try:
                local_private = "PRIVATE_LOCAL"
                raise ValueError("response choices is null\nPRIVATE_EXCEPTION_DETAIL")
            except ValueError:
                logger.exception("Translation failed")
        output = stream.getvalue()
        for private in ("PRIVATE_BODY", "PRIVATE_CONTINUATION", "PRIVATE_LOCAL", "PRIVATE_EXCEPTION_DETAIL"):
            self.assertNotIn(private, output)
        self.assertIn("ValueError: response choices is null", output)
        self.assertNotIn("Traceback", output)

    def test_actual_babeldoc_input_output_warnings_are_redacted_end_to_end(self):
        # These are the warning templates in BabelDOC 0.6.2's
        # il_translator_llm_only.py, including its non-string result fallback.
        warnings = [
            "Translation result edit distance is too small. distance: 2, input: PRIVATE_SOURCE, output: PRIVATE_TRANSLATION",
            "Translation result is not a string. Output: ['PRIVATE_NONSTRING']",
        ]
        script = f"""
import sys, logging
sys.path.insert(0, {str(SERVER)!r})
from utils.engine_logging import install_engine_diagnostics
install_engine_diagnostics()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('babeldoc.format.pdf.document_il.midend.il_translator_llm_only')
for warning in {warnings!r}:
    logger.warning(warning)
"""
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as console:
            result = _execute_captured([sys.executable, "-c", script], dict(os.environ), None, log_dir=directory)
            saved = Path(result.log_path).read_text(encoding="utf-8")
        for text in (saved, console.getvalue(), result.tail, result.summary):
            for private in ("PRIVATE_SOURCE", "PRIVATE_TRANSLATION", "PRIVATE_NONSTRING"):
                self.assertNotIn(private, text)
        self.assertIn("edit distance is too small. distance: 2", saved)
        self.assertIn("Translation result is not a string.", saved)
        self.assertIn("[请求/正文内容已省略]", saved)

    def test_babeldoc_object_reprs_and_term_extractor_payloads_are_redacted(self):
        messages = [
            "Style is None. Composition: PdfParagraphComposition(unicode='PRIVATE'). Paragraph: details",
            "Cannot passthrough unicode. TypesettingUnit: TypesettingUnit(unicode='PRIVATE')",
            "Unexpected character PdfCharacter(char_unicode='PRIVATE')",
            "Invalid object PdfSameStyleUnicodeCharacters(unicode='PRIVATE')",
            "Skipping malformed item in LLM JSON response: {'term': 'PRIVATE'}",
            "JSON Parsing Error: invalid. Problematic LLM Response after cleaning (start): PRIVATE",
            "Input: PRIVATE\nMORE_PRIVATE",
            "Output: PRIVATE\nMORE_PRIVATE",
        ]
        for message in messages:
            with self.subTest(message=message):
                cleaned = safe_message(message)
                self.assertNotIn("PRIVATE", cleaned)
                self.assertIn("[请求/正文内容已省略]", cleaned)
        url = "https://models.example.test/resources/input:font/output:weights.onnx"
        self.assertEqual(safe_message("ConnectTimeout: download " + url), "ConnectTimeout: download " + url)

    def test_install_is_idempotent_and_does_not_change_root_handlers(self):
        before = list(logging.getLogger().handlers)
        second = install_engine_diagnostics()
        self.assertEqual(second, [])
        remove_engine_diagnostics(second)
        self.assertEqual(logging.getLogger().handlers, before)
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            logging.getLogger("babeldoc.assets.assets").warning("download failed")
        self.assertEqual(stream.getvalue().count(ENGINE_DIAGNOSTIC_PREFIX), 1)

    def test_invalid_envelopes_are_discarded_not_logged_as_raw_payload(self):
        payloads = [
            {}, [], {"level": [], "logger": "babeldoc", "message": "ERROR PRIVATE"},
            {"level": "ERROR", "logger": "httpx", "message": "PRIVATE"},
            {"level": "ERROR", "logger": "babeldoc", "message": "failure\nPRIVATE"},
            {"level": "ERROR", "logger": "babeldoc", "message": "failure\u2028PRIVATE"},
            {"level": "ERROR", "logger": "babeldoc", "message": "PRIVATE", "body": "PRIVATE"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = TaskDiagnostics("malformed", log_dir=directory)
            for payload in payloads:
                self.assertEqual(diagnostic.record(ENGINE_DIAGNOSTIC_PREFIX + json.dumps(payload)), "")
            self.assertEqual(diagnostic.record(ENGINE_DIAGNOSTIC_PREFIX + "ERROR PRIVATE not JSON"), "")
            result = diagnostic.finish(1)
            self.assertNotIn("PRIVATE", Path(result.log_path).read_text(encoding="utf-8"))

    def test_asset_status_is_kept_without_accepting_general_info_or_indentation(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = TaskDiagnostics("asset", log_dir=directory)
            self.assertTrue(diagnostic.record("[asset_download] INFO verified font.ttf"))
            self.assertEqual(diagnostic.record("INFO PRIVATE_PAPER"), "")
            self.assertEqual(diagnostic.record("    PRIVATE_CONTINUATION"), "")
            diagnostic.finish(0)

    def test_nonstandard_level_does_not_bypass_task_log_validation(self):
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            logger = logging.getLogger("babeldoc.assets.assets")
            logger.log(45, "request_body={'content': 'PRIVATE_BODY'}")
        self.assertNotIn("PRIVATE_BODY", stream.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = TaskDiagnostics("custom-level", log_dir=directory)
            for line in stream.getvalue().splitlines():
                self.assertEqual(diagnostic.record(line), "")
            result = diagnostic.finish(0)
            self.assertNotIn("PRIVATE_BODY", Path(result.log_path).read_text(encoding="utf-8"))

    @unittest.skipUnless(importlib.util.find_spec("rich"), "Rich integration needs engine dependencies")
    def test_actual_rich_narrow_console_does_not_truncate_captured_resource_url(self):
        url = "https://models.example.test/models/very-long-resource-path/SourceHanSansCN-Regular.ttf"
        script = f"""
import sys, logging
sys.path.insert(0, {str(SERVER)!r})
from rich.console import Console
from rich.logging import RichHandler
from utils.engine_logging import install_engine_diagnostics
install_engine_diagnostics()
logging.basicConfig(level=logging.INFO, handlers=[RichHandler(console=Console(width=45, force_terminal=True))])
logging.getLogger('babeldoc.assets.assets').warning('ConnectTimeout: resource download failed for %s', {url!r})
"""
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            result = _execute_captured([sys.executable, "-c", script], dict(os.environ), None, log_dir=directory)
            self.assertIn(url, result.summary)
            self.assertIn(url, Path(result.log_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
