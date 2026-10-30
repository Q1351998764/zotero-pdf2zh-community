"""A generated PDF and child exit zero must not hide final paragraph failures."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
import uuid

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from utils.diagnostics import ENGINE_DIAGNOSTIC_PREFIX, TranslationProcessError
from utils.execute import _execute_captured
from utils.task_manager import task_manager


class PartialTranslationFailureTest(unittest.TestCase):
    def run_child(self, body):
        task_id = "partial_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as console:
            root = Path(directory)
            script = root / "fake_translation.py"
            script.write_text(
                "import sys, logging\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(SERVER)!r})\n"
                "from utils.engine_logging import install_engine_diagnostics\n"
                + textwrap.dedent(body), encoding="utf-8",
            )
            task_manager.add_task(task_id, {"taskId": task_id, "active": True, "progress": 0})
            error, result = None, None
            try:
                try:
                    result = _execute_captured(
                        [sys.executable, "-u", str(script)],
                        {**os.environ, "PYTHONIOENCODING": "utf-8",
                         "OPENAI_API_KEY": "test-private-provider-key"},
                        task_id, cwd=root, log_dir=root / "logs",
                    )
                except TranslationProcessError as exc:
                    error = exc
                state = task_manager.get_task(task_id)
                log_path = error.log_path if error else result.log_path
                return {
                    "error": error,
                    "result": result,
                    "state": state,
                    "log": Path(log_path).read_text(encoding="utf-8"),
                    "console": console.getvalue(),
                    "pdf_created": (root / "output.pdf").is_file(),
                }
            finally:
                with task_manager.lock:
                    task_manager.active_tasks.pop(task_id, None)

    def assert_final_failure(self, captured, count):
        error = captured["error"]
        self.assertIsInstance(error, TranslationProcessError)
        self.assertNotEqual(error.returncode, 0)
        self.assertEqual(error.failed_paragraphs, count)
        self.assertIn(f"{count} 个段落翻译失败", str(error))
        self.assertIn("保留原文", str(error))
        self.assertIn("exit=0", captured["log"])
        self.assertIn(f"paragraph_failures={count}", captured["log"])
        self.assertEqual(captured["state"]["diagnosticSummary"], error.summary)
        self.assertTrue(captured["pdf_created"])
        self.assertNotIn(ENGINE_DIAGNOSTIC_PREFIX, captured["log"])

    def test_zero_exit_with_pdf_and_two_identical_final_errors_is_failure(self):
        captured = self.run_child("""
            install_engine_diagnostics()
            # Preserve normal console logging too: each failure has both the
            # structured record and a raw copy, but must be counted only once.
            logging.basicConfig(level=logging.INFO)
            logger = logging.getLogger('babeldoc.format.pdf.document_il.midend.il_translator')
            for _ in range(2):
                try:
                    raise ConnectionError('Connection error.')
                except ConnectionError:
                    logger.exception('Error translating paragraph. Paragraph: PRIVATE_PAPER. Error: test-private-provider-key')
            Path('output.pdf').write_bytes(b'%PDF-1.4\\n%%EOF\\n')
        """)
        self.assert_final_failure(captured, 2)
        self.assertIn("ConnectionError: Connection error.", str(captured["error"]))
        for text in (captured["log"], captured["console"], str(captured["error"])):
            self.assertNotIn("PRIVATE_PAPER", text)
            self.assertNotIn("test-private-provider-key", text)

    def test_recoverable_fallback_and_network_retry_warning_do_not_fail(self):
        captured = self.run_child("""
            install_engine_diagnostics()
            fallback = logging.getLogger('babeldoc.format.pdf.document_il.midend.il_translator_llm_only')
            try:
                raise ValueError('Invalid translated response')
            except ValueError:
                fallback.exception('Error translating paragraph. Error: Invalid translated response.')
            fallback.warning('Fallback to simple translation. paragraph id: fake-id')
            logger = logging.getLogger('babeldoc.format.pdf.document_il.midend.il_translator')
            logger.warning('Network connection interrupted; retrying translation')
            # The logger alone is insufficient: only final ERROR events count.
            logger.warning('Error translating paragraph. Retrying before final result.')
            sys.stdout.write('translate 2/2\\r')
            Path('output.pdf').write_bytes(b'%PDF-1.4\\n%%EOF\\n')
        """)
        self.assertIsNone(captured["error"])
        self.assertEqual(captured["result"].returncode, 0)
        self.assertEqual(captured["result"].failed_paragraphs, 0)
        self.assertTrue(captured["pdf_created"])
        self.assertIn("Fallback to simple translation", captured["log"])
        self.assertIn("retrying translation", captured["console"])

    def test_structured_error_after_unterminated_progress_stays_complete(self):
        captured = self.run_child("""
            install_engine_diagnostics()
            sys.stdout.write('\\rtranslate 1/2')
            sys.stdout.flush()
            logger = logging.getLogger('babeldoc.format.pdf.document_il.midend.il_translator')
            try:
                raise ConnectionError('Server disconnected without sending a response.')
            except ConnectionError:
                logger.exception('Error translating paragraph. Paragraph: PRIVATE_PAPER')
            sys.stdout.write('\\rtranslate 2/2')
            sys.stdout.flush()
            Path('output.pdf').write_bytes(b'%PDF-1.4\\n%%EOF\\n')
        """)
        self.assert_final_failure(captured, 1)
        self.assertIn("[engine] ERROR babeldoc.format.pdf.document_il.midend.il_translator:", captured["log"])
        self.assertIn("Server disconnected without sending a response.", captured["log"])
        self.assertIn("translate 2/2", captured["console"])
        self.assertEqual(captured["state"]["progress"], 99)
        self.assertNotIn("PRIVATE_PAPER", captured["log"])

    def test_spawn_worker_final_failure_survives_parent_exit_zero(self):
        # A real spawn subprocess exercises Windows's independent interpreter
        # and stderr pipe inheritance without an engine or network request.
        captured = self.run_child("""
            import multiprocessing

            def worker():
                install_engine_diagnostics()
                logger = logging.getLogger('babeldoc.format.pdf.document_il.midend.il_translator')
                try:
                    raise ConnectionError('Worker translation connection failed.')
                except ConnectionError:
                    logger.exception('Error translating paragraph. Paragraph: PRIVATE_WORKER_PAPER')

            if __name__ == '__main__':
                process = multiprocessing.get_context('spawn').Process(target=worker)
                process.start()
                process.join(20)
                if process.is_alive():
                    process.kill()
                    process.join()
                    raise RuntimeError('Test worker did not exit')
                assert process.exitcode == 0, process.exitcode
                Path('output.pdf').write_bytes(b'%PDF-1.4\\n%%EOF\\n')
        """)
        self.assert_final_failure(captured, 1)
        self.assertIn("Worker translation connection failed.", captured["log"])
        self.assertNotIn("PRIVATE_WORKER_PAPER", captured["log"])

    @unittest.skipUnless(importlib.util.find_spec("babeldoc") and importlib.util.find_spec("openai"),
                         "Actual paragraph handler needs engine dependencies")
    def test_actual_il_translator_retains_source_and_advances_progress_after_api_failure(self):
        captured = self.run_child("""
            from types import SimpleNamespace
            from unittest.mock import Mock
            import httpx
            import openai
            from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator

            install_engine_diagnostics()
            # Use the real paragraph exception handler and PbarContext. Only
            # the provider call and unrelated document preparation are mocked.
            translator = ILTranslator.__new__(ILTranslator)
            translator.translation_config = SimpleNamespace(raise_if_cancelled=lambda: None)
            translator.use_as_fallback = False
            translator.support_llm_translate = False
            source = 'PRIVATE_SYNTHETIC_ORIGINAL paragraph remains in English.'
            paragraph = SimpleNamespace(unicode=source, debug_id='synthetic')
            translator.pre_translate_paragraph = Mock(return_value=(source, object()))
            provider = Mock(side_effect=openai.APIConnectionError(
                request=httpx.Request('POST', 'https://example.invalid/v1/chat/completions'),
            ))
            translator.translate_engine = SimpleNamespace(translate=provider)
            translator.post_translate_paragraph = Mock()
            progress = Mock()
            progress.advance.side_effect = lambda: sys.stdout.write('translate 1/1\\r')
            result = translator.translate_paragraph(
                paragraph, SimpleNamespace(), pbar=progress, tracker=Mock(),
            )

            assert result is None  # BabelDOC swallows the provider exception.
            assert paragraph.unicode == source
            provider.assert_called_once()
            translator.post_translate_paragraph.assert_not_called()
            progress.advance.assert_called_once_with()
            Path('output.pdf').write_bytes(b'%PDF-1.4\\n%%EOF\\n')
        """)
        self.assert_final_failure(captured, 1)
        self.assertEqual(captured["state"]["progress"], 99)
        self.assertIn("translate 1/1", captured["console"])
        self.assertIn("APIConnectionError: Connection error.", str(captured["error"]))
        self.assertNotIn("PRIVATE_SYNTHETIC_ORIGINAL", captured["log"])
        self.assertNotIn("PRIVATE_SYNTHETIC_ORIGINAL", captured["console"])


if __name__ == "__main__":
    unittest.main()
