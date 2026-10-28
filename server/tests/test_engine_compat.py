import importlib.util
import json
import logging
import os
import runpy
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import engine_runner
from utils.engine_compat import TranslationResponseError
from utils.engine_compat import install_openai_response_compat
from utils.engine_compat import normalize_chat_completion


def completion(content="你好", finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish_reason
        )],
        usage=SimpleNamespace(total_tokens=12, prompt_tokens=8, completion_tokens=4),
    )


def completion_payload(content="你好", finish_reason="stop"):
    return {
        "id": "test-completion", "object": "chat.completion", "created": 1,
        "model": "test-model",
        "choices": [{
            "index": 0, "finish_reason": finish_reason,
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {"total_tokens": 12, "prompt_tokens": 8, "completion_tokens": 4},
    }


class CompletionValidationTest(unittest.TestCase):
    def test_standard_response_identity_and_usage_are_preserved(self):
        response = completion()
        self.assertIs(normalize_chat_completion(response), response)
        self.assertEqual(response.usage.total_tokens, 12)

    def test_data_envelope_unwraps_without_mutating_original(self):
        payload = completion_payload()
        response = SimpleNamespace(choices=None, data=payload)
        normalized = normalize_chat_completion(response)
        self.assertEqual(normalized.choices[0].message.content, "你好")
        self.assertEqual(normalized.usage.total_tokens, 12)
        self.assertIsNone(response.choices)
        self.assertIs(response.data, payload)

    def test_outer_usage_is_preserved_if_envelope_does_not_include_it(self):
        payload = completion_payload()
        del payload["usage"]
        usage = completion().usage
        normalized = normalize_chat_completion(
            SimpleNamespace(choices=None, data=payload, usage=usage)
        )
        self.assertIs(normalized.usage, usage)

    def test_top_level_choices_take_precedence_over_data(self):
        response = completion("标准译文")
        response.data = completion_payload("其他数据")
        self.assertIs(normalize_chat_completion(response), response)
        self.assertEqual(response.choices[0].message.content, "标准译文")

    def test_missing_null_and_empty_choices_have_safe_errors(self):
        for response in (
            SimpleNamespace(), SimpleNamespace(choices=None),
            SimpleNamespace(choices=[]), SimpleNamespace(choices="invalid"),
            SimpleNamespace(choices=[], data=completion_payload()),
            SimpleNamespace(choices=None, data={"error": "SECRET_API_KEY"}),
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(TranslationResponseError, "choices") as caught:
                    normalize_chat_completion(response)
                self.assertNotIn("SECRET_API_KEY", str(caught.exception))

    def test_missing_or_empty_content_fails_explicitly(self):
        for response in (
            completion(None), completion(""), completion(" \n "), completion([]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")]),
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(TranslationResponseError, "message.content"):
                    normalize_chat_completion(response)

    def test_length_finish_reason_rejects_partial_and_empty_text(self):
        for text in ("未完成的译文", None, ""):
            with self.subTest(text=text):
                with self.assertRaisesRegex(TranslationResponseError, "finish_reason=length"):
                    normalize_chat_completion(completion(text, "length"))

    def test_incompatible_envelope_error_does_not_echo_validation_input(self):
        class InvalidResponse:
            choices = None
            data = {**completion_payload(), "private": "SECRET_API_KEY"}

            @classmethod
            def model_validate(cls, payload):
                raise ValueError(str(payload))

        with self.assertRaisesRegex(TranslationResponseError, "data 包装") as caught:
            normalize_chat_completion(InvalidResponse())
        self.assertNotIn("SECRET_API_KEY", str(caught.exception))


class TranslatorAdapterTest(unittest.TestCase):
    @staticmethod
    def translator_class():
        class FakeTranslator:
            def __init__(self, resource):
                self.client = SimpleNamespace(chat=SimpleNamespace(completions=resource))

            def do_translate(self, text):
                response = self.client.chat.completions.create(
                    model="text-model", messages=[{"role": "user", "content": text}]
                )
                return response.choices[0].message.content.strip()

            def do_llm_translate(self, text):
                response = self.client.chat.completions.create(
                    model="glossary-model", messages=[{"role": "user", "content": text}]
                )
                return response.choices[0].message.content.strip()

        return FakeTranslator

    def test_both_text_and_glossary_requests_use_same_response_adapter(self):
        cls = self.translator_class()
        create = Mock(return_value=SimpleNamespace(choices=None, data=completion_payload()))
        resource = SimpleNamespace(create=create, sentinel="original resource")
        self.assertTrue(install_openai_response_compat(cls))
        self.assertFalse(install_openai_response_compat(cls))
        translator = cls(resource)
        self.assertEqual(translator.do_translate("Hello"), "你好")
        self.assertEqual(translator.do_llm_translate("Extract glossary"), "你好")
        self.assertEqual(create.call_count, 2)
        self.assertEqual(create.call_args_list[0].kwargs["model"], "text-model")
        self.assertEqual(create.call_args_list[1].kwargs["model"], "glossary-model")
        self.assertEqual(translator.client.chat.completions.sentinel, "original resource")
        self.assertIs(resource.create, create, "the original resource is not patched globally")

    def test_streams_and_sdk_exceptions_pass_through(self):
        cls = self.translator_class()
        stream = iter(["chunk"])
        create = Mock(return_value=stream)
        install_openai_response_compat(cls)
        translator = cls(SimpleNamespace(create=create))
        self.assertIs(translator.client.chat.completions.create(stream=True), stream)
        expected = ConnectionError("temporary network failure")
        create.side_effect = expected
        with self.assertRaises(ConnectionError) as caught:
            translator.do_translate("Hello")
        self.assertIs(caught.exception, expected)

    def test_changed_upstream_client_contract_fails_clearly(self):
        class ChangedTranslator:
            def __init__(self):
                self.client = None

        install_openai_response_compat(ChangedTranslator)
        with self.assertRaisesRegex(RuntimeError, "客户端结构"):
            ChangedTranslator()


@unittest.skipUnless(
    importlib.util.find_spec("openai") and importlib.util.find_spec("httpx"),
    "optional OpenAI SDK contract tests require the engine dependencies",
)
class RealSDKContractTest(unittest.TestCase):
    def test_sdk_wrapped_and_standard_responses_with_mock_transport(self):
        import httpx
        from openai import OpenAI

        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                payload = completion_payload()
                body = {"data": payload} if wrapped else payload
                requests = []

                def respond(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json=body)

                with OpenAI(
                    api_key="test-only-not-a-real-key", base_url="https://example.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(respond)),
                ) as client:
                    response = client.chat.completions.create(
                        model="test-model", messages=[{"role": "user", "content": "Hello"}]
                    )
                    normalized = normalize_chat_completion(response)
                self.assertEqual(normalized.choices[0].message.content, "你好")
                self.assertEqual(normalized.usage.total_tokens, 12)
                self.assertEqual(normalized.usage.prompt_tokens, 8)
                self.assertEqual(normalized.model, "test-model")
                self.assertEqual(len(requests), 1)
                if not wrapped:
                    self.assertIs(normalized, response)

    def test_sdk_missing_choices_and_truncation_report_actionable_errors(self):
        import httpx
        from openai import OpenAI

        for body, expected in (
            ({"error": {"message": "PRIVATE_PROVIDER_ERROR"}}, "choices"),
            ({"data": {"choices": None}}, "choices"),
            ({"data": {"choices": []}}, "choices"),
            (completion_payload("partial", "length"), "finish_reason=length"),
            ({"data": completion_payload("partial", "length")}, "finish_reason=length"),
            (completion_payload(None), "message.content"),
            ({"data": completion_payload(None)}, "message.content"),
        ):
            with self.subTest(expected=expected):
                with OpenAI(
                    api_key="test-only-not-a-real-key", base_url="https://example.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, json=body)
                    )),
                ) as client:
                    response = client.chat.completions.create(
                        model="test-model", messages=[{"role": "user", "content": "Hello"}]
                    )
                    with self.assertRaisesRegex(TranslationResponseError, expected) as caught:
                        normalize_chat_completion(response)
                self.assertNotIn("PRIVATE_PROVIDER_ERROR", str(caught.exception))


@unittest.skipUnless(
    importlib.util.find_spec("pdf2zh_next") and importlib.util.find_spec("babeldoc"),
    "optional upstream-translator contract tests require the full engine environment",
)
class RealEngineTranslatorContractTest(unittest.TestCase):
    def test_actual_upstream_text_and_glossary_methods_accept_wrapped_sdk_responses(self):
        import httpx
        from pdf2zh_next.config.model import SettingsModel
        from pdf2zh_next.config.translate_engine_model import OpenAISettings
        from pdf2zh_next.translator.translator_impl import openai as upstream

        original_init = upstream.OpenAITranslator.__init__
        self.addCleanup(setattr, upstream.OpenAITranslator, "__init__", original_init)
        install_openai_response_compat(upstream.OpenAITranslator)
        settings = SettingsModel(translate_engine_settings=OpenAISettings(
            openai_api_key="test-only-not-a-real-key",
            openai_base_url="https://example.invalid/v1", openai_model="test-model",
        ))
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"data": completion_payload()})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            # Replace the translator module's HTTP factory only. SDK internals
            # still see the real httpx module; no global transport is patched.
            with (
                patch.object(upstream, "httpx", SimpleNamespace(Client=lambda **kwargs: client, Limits=httpx.Limits)),
                patch("pdf2zh_next.translator.base_translator.TranslationCache", return_value=Mock()),
            ):
                translator = upstream.OpenAITranslator(settings, SimpleNamespace(wait=lambda *args: None))
            self.assertEqual(translator.do_translate("Hello"), "你好")
            self.assertEqual(translator.do_llm_translate("Extract terminology"), "你好")
            self.assertEqual(translator.token_count.value, 24)
            self.assertEqual(translator.prompt_token_count.value, 16)
            self.assertEqual(translator.completion_token_count.value, 8)
        self.assertEqual(len(requests), 2)
        self.assertIn("Hello", requests[0]["messages"][0]["content"])
        self.assertEqual(requests[1]["messages"][0]["content"], "Extract terminology")


class EngineRunnerTest(unittest.TestCase):
    def setUp(self):
        self.asset_module = SimpleNamespace(ensure_legacy_rapidocr_asset=Mock(return_value={
            "status": "skipped", "message": "not needed", "required": False,
        }))

    def test_entry_point_argv_and_nonzero_exit_are_preserved(self):
        original_argv = sys.argv

        def command():
            self.assertEqual(sys.argv, ["pdf2zh_next", "paper.pdf", "--lang-out", "zh-CN"])
            self.assertEqual(os.environ[engine_runner._COMPAT_ENV], "1")
            raise SystemExit(7)

        with (
            patch.dict(sys.modules, {"utils.asset_repair": self.asset_module}),
            patch.object(engine_runner, "install_openai_response_compat") as install,
            patch.object(engine_runner, "_entry_point", return_value=command),
        ):
            self.assertEqual(engine_runner.main(["pdf2zh_next", "paper.pdf", "--lang-out", "zh-CN"]), 7)
        install.assert_called_once_with()
        self.assertIs(sys.argv, original_argv)

    def test_logged_whole_file_failure_cannot_exit_successfully(self):
        def command():
            logging.getLogger("pdf2zh_next.high_level").error("Error translating file paper.pdf: failed")
            return 0

        with (
            patch.dict(sys.modules, {"utils.asset_repair": self.asset_module}),
            patch.object(engine_runner, "install_openai_response_compat"),
            patch.object(engine_runner, "_entry_point", return_value=command),
            redirect_stderr(StringIO()) as stderr,
        ):
            self.assertEqual(engine_runner.main(["pdf2zh_next", "paper.pdf"]), 1)
        self.assertIn("翻译失败", stderr.getvalue())

    def test_legacy_engine_does_not_apply_next_adapters(self):
        with (
            patch.object(engine_runner, "install_openai_response_compat") as install,
            patch.object(engine_runner, "_entry_point", return_value=lambda: None),
        ):
            self.assertEqual(engine_runner.main(["pdf2zh", "--help"]), 0)
        install.assert_not_called()

    def test_help_and_version_do_not_repair_assets_or_install_adapters(self):
        for option in ("--help", "-h", "--version"):
            with self.subTest(option=option):
                with (
                    patch.dict(sys.modules, {"utils.asset_repair": self.asset_module}),
                    patch.object(engine_runner, "install_openai_response_compat") as install,
                    patch.object(engine_runner, "_entry_point", return_value=lambda: None),
                ):
                    self.assertEqual(engine_runner.main(["pdf2zh_next", option]), 0)
                install.assert_not_called()
        self.asset_module.ensure_legacy_rapidocr_asset.assert_not_called()

    def test_required_asset_failure_stops_before_loading_engine(self):
        self.asset_module.ensure_legacy_rapidocr_asset.return_value = {
            "status": "failed", "message": "模型资产校验失败", "required": True,
        }
        with (
            patch.dict(sys.modules, {"utils.asset_repair": self.asset_module}),
            patch.object(engine_runner, "_entry_point") as load,
            redirect_stderr(StringIO()) as stderr,
        ):
            self.assertEqual(engine_runner.main(["pdf2zh_next", "paper.pdf"]), 1)
        load.assert_not_called()
        self.assertIn("模型资产校验失败", stderr.getvalue())

    def test_multiprocessing_spawn_reapplies_adapter_before_worker_runs(self):
        with (
            patch.dict(os.environ, {engine_runner._COMPAT_ENV: "1"}),
            patch("utils.engine_compat.install_openai_response_compat") as install,
        ):
            runpy.run_path(str(Path(engine_runner.__file__)), run_name="__mp_main__")
        install.assert_called_once_with()

    def test_invalid_engine_is_rejected(self):
        with redirect_stderr(StringIO()):
            self.assertEqual(engine_runner.main(["untrusted-command"]), 2)


class SpawnedEngineIntegrationTest(unittest.TestCase):
    def test_real_spawn_worker_receives_compatibility_without_external_requests(self):
        # The fake installed distribution exercises metadata entry-point loading
        # and actual spawn re-execution, including on Windows. It cannot reach a
        # network service: its completions resource only returns an in-memory
        # response and its BabelDOC metadata has no legacy asset contract.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "pdf2zh_next" / "translator" / "translator_impl"
            package.mkdir(parents=True)
            for directory in (package, package.parent, package.parent.parent):
                (directory / "__init__.py").write_text("", encoding="utf-8")
            (package / "openai.py").write_text(textwrap.dedent('''\
                from types import SimpleNamespace

                class OpenAITranslator:
                    def __init__(self):
                        self.client = SimpleNamespace(chat=SimpleNamespace(completions=self))

                    def create(self, **kwargs):
                        return SimpleNamespace(choices=None, data={
                            "choices": [{"message": {"content": "translated"}, "finish_reason": "stop"}],
                            "usage": {"total_tokens": 5},
                        })

                    def do_translate(self, text):
                        return self.client.chat.completions.create().choices[0].message.content

                    def do_llm_translate(self, text):
                        return self.client.chat.completions.create().choices[0].message.content
                '''), encoding="utf-8")
            (root / "fake_engine.py").write_text(textwrap.dedent('''\
                import multiprocessing

                def worker():
                    from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator
                    translator = OpenAITranslator()
                    assert translator.do_translate("Hello") == "translated"
                    assert translator.do_llm_translate("Glossary") == "translated"
                    print("SPAWN_TEXT_AND_GLOSSARY_OK", flush=True)

                def cli():
                    process = multiprocessing.get_context("spawn").Process(target=worker)
                    process.start()
                    process.join(15)
                    if process.is_alive():
                        process.terminate()
                        process.join()
                        return 9
                    return process.exitcode
                '''), encoding="utf-8")
            # Use an otherwise empty BabelDOC distribution to prevent the real
            # asset helper from inspecting/downloading assets from a host env.
            metadata = root / "BabelDOC-0.0.0.dist-info"
            metadata.mkdir()
            (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: BabelDOC\nVersion: 0.0.0\n", encoding="utf-8")
            scripts = root / "fake_engine-0.0.0.dist-info"
            scripts.mkdir()
            (scripts / "METADATA").write_text("Metadata-Version: 2.1\nName: fake-engine\nVersion: 0.0.0\n", encoding="utf-8")
            (scripts / "entry_points.txt").write_text(
                "[console_scripts]\npdf2zh_next = fake_engine:cli\n", encoding="utf-8"
            )
            # Existing pdf2zh installations may also publish the same entry
            # point. A tiny bootstrap filters discovery to the fake test entry
            # point while retaining the real runner as __main__ for spawn.
            bootstrap = root / "bootstrap.py"
            bootstrap.write_text(textwrap.dedent('''\
                import importlib.metadata
                import runpy
                import sys

                if __name__ == "__main__":
                    original = importlib.metadata.entry_points
                    def entries(**kwargs):
                        return [entry for entry in original(**kwargs)
                                if entry.value == "fake_engine:cli"]
                    importlib.metadata.entry_points = entries
                    runner = sys.argv[1]
                    sys.argv = [runner, "pdf2zh_next", "test-input.pdf"]
                    runpy.run_path(runner, run_name="__main__")
                '''), encoding="utf-8")
            env = dict(os.environ)
            env.pop("PYTHONHOME", None)
            env["PYTHONPATH"] = os.pathsep.join([str(root), str(Path(engine_runner.__file__).parent)])
            env["PYTHONNOUSERSITE"] = "1"
            result = subprocess.run(
                [sys.executable, str(bootstrap), str(Path(engine_runner.__file__).resolve())],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SPAWN_TEXT_AND_GLOSSARY_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
