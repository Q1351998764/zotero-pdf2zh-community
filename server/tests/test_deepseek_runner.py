import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import toml

from utils import deepseek_thinking as thinking


class DeepSeekRunnerInvocationTest(unittest.TestCase):
    def tearDown(self):
        thinking._RUNTIME_CAPABILITY_CACHE.clear()

    def test_managed_runner_prefix_preserves_python_and_options(self):
        runner = str(Path(__file__).resolve().parents[1] / "engine_runner.py")
        prefix = [sys.executable, "-u", runner, "pdf2zh_next"]
        command = [*prefix, "paper.pdf", "--deepseek"]
        self.assertEqual(thinking._invocation_prefix(command), prefix)
        self.assertEqual(thinking._python_for_invocation_prefix(prefix), Path(sys.executable))

    def test_module_and_console_launchers_remain_supported(self):
        for flags in ([], ["-u"], ["-I", "-u"]):
            with self.subTest(flags=flags):
                prefix = [sys.executable, *flags, "-m", "pdf2zh_next"]
                self.assertEqual(thinking._invocation_prefix([*prefix, "paper.pdf"]), prefix)
                self.assertEqual(thinking._python_for_invocation_prefix(prefix), Path(sys.executable))
        self.assertEqual(thinking._invocation_prefix(["pdf2zh_next", "paper.pdf"]), ["pdf2zh_next"])
        self.assertEqual(thinking._invocation_prefix([]), [])
        self.assertIsNone(thinking._python_for_invocation_prefix([]))

    def test_later_script_argument_is_not_misidentified_as_a_runner(self):
        command = ["pdf2zh_next", "paper.pdf", "engine_runner.py", "pdf2zh_next"]
        self.assertEqual(thinking._invocation_prefix(command), ["pdf2zh_next"])

    def test_runner_capability_probe_inspects_python_without_launching_cli(self):
        prefix = [sys.executable, "-u", "engine_runner.py", "pdf2zh_next"]
        environment = {"PYTHONNOUSERSITE": "1"}
        with (
            patch("utils.environment_lifecycle.runtime_supports_deepseek_thinking", return_value=True) as probe,
            patch.object(thinking.subprocess, "run") as launch,
        ):
            self.assertTrue(thinking._runtime_supports_thinking(prefix, env=environment))
        probe.assert_called_once_with(Path(sys.executable), env=environment, isolated=True)
        launch.assert_not_called()

    def test_runtime_flags_are_appended_after_runner_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text(toml.dumps({"deepseek_detail": {
                "deepseek_model": "deepseek-v4-flash",
                "deepseek_thinking_mode": "enabled",
                "deepseek_reasoning_effort": "high",
            }}), encoding="utf-8")
            prefix = [sys.executable, "-u", "engine_runner.py", "pdf2zh_next"]
            command = [*prefix, "paper.pdf", "--deepseek", "--config-file", str(config)]
            with patch.object(thinking, "_runtime_supports_thinking", return_value=True) as probe:
                result = thinking.prepare_deepseek_runtime_command(command)
            probe.assert_called_once_with(prefix, env=None)
            self.assertEqual(result[:len(command)], command)
            self.assertEqual(result[-4:], [
                "--deepseek-thinking-mode", "enabled", "--deepseek-reasoning-effort", "high"
            ])

    def test_unsupported_runner_runtime_blocks_requested_thinking(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text(toml.dumps({"deepseek_detail": {
                "deepseek_model": "deepseek-v4-flash", "deepseek_thinking_mode": "enabled",
            }}), encoding="utf-8")
            command = [sys.executable, "-u", "engine_runner.py", "pdf2zh_next",
                       "paper.pdf", "--deepseek", "--config-file", str(config)]
            with patch.object(thinking, "_runtime_supports_thinking", return_value=False):
                with self.assertRaisesRegex(ValueError, "API 调用前停止"):
                    thinking.prepare_deepseek_runtime_command(command)


if __name__ == "__main__":
    unittest.main()
