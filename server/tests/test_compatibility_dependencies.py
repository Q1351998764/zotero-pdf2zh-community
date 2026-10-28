import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import environment_lifecycle as lifecycle
from utils import venv


class CompatibilityDependenciesTest(unittest.TestCase):
    def test_persisted_old_configuration_uses_compatible_release_set(self):
        config = {
            "pdf2zh_next": {"uv": {
                "python_version": "3.12",
                "packages": ["pdf2zh_next>=2.9", "BabelDOC", "PyMuPDF", "bitstring>=4", "pypdf"],
            }},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "venv.json"
            original = json.dumps(config)
            path.write_text(original, encoding="utf-8")
            python, requirements = lifecycle.load_requirements("pdf2zh_next", "uv", path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(python, "3.12")
        self.assertEqual(requirements, [*lifecycle.MANAGED_NEXT_REQUIREMENTS, "pypdf"])

    def test_legacy_engine_only_overrides_incompatible_bitstring_requirement(self):
        original = ["pdf2zh==1.9.11", "BabelDOC<0.3.0", "PyMuPDF", "numpy==2.2.0", "bitstring>=5"]
        config = {"pdf2zh": {"uv": {"packages": original}}}
        with patch.object(lifecycle, "load_environment_config", return_value=config):
            _, requirements = lifecycle.load_requirements("pdf2zh", "uv")
        self.assertEqual(requirements, ["bitstring==4.3.1", *original[:-1]])
        self.assertEqual(config["pdf2zh"]["uv"]["packages"], original)

    def test_shipped_legacy_uv_and_conda_requirements_include_bitstream_pin(self):
        for tool in ("uv", "conda"):
            with self.subTest(tool=tool):
                _, requirements = lifecycle.load_requirements("pdf2zh", tool)
                self.assertEqual(requirements.count("bitstring==4.3.1"), 1)
                self.assertIn("pdf2zh==1.9.11", requirements)
                self.assertNotIn("BabelDOC==0.6.2", requirements)

    def test_bitstream_repair_keeps_an_older_otherwise_working_runtime(self):
        manager = venv.VirtualEnvManager("missing.json", None, "uv", skip_install=True)
        manager.skip_install = False
        python = Path("managed-python")
        existing = ("uv", Path("managed"), python)
        with (
            patch.object(manager, "_existing", return_value=existing),
            patch.object(venv, "load_requirements", return_value=("3.12", [])),
            patch.object(venv, "managed_runtime_health", side_effect=[(False, "bitstring has no BitStream"), (True, "ok")]),
            patch.object(venv, "repair_bitstream_dependency", return_value=True) as repair,
            patch.object(venv, "transactional_install_or_update") as update,
        ):
            self.assertTrue(manager.ensure_env("pdf2zh_next"))
        repair.assert_called_once_with("uv", python, engine="pdf2zh_next", preferred_index=None)
        update.assert_not_called()

    def test_auto_discovers_a_healthy_environment_before_repairing_another(self):
        manager = venv.VirtualEnvManager("missing.json", None, "auto", skip_install=True)
        manager.skip_install = False
        with (
            patch.object(manager, "_existing", side_effect=lambda engine, tool: (tool, Path(tool), Path(tool) / "python")),
            patch.object(venv, "load_requirements", return_value=("3.12", [])),
            patch.object(venv, "managed_runtime_health", side_effect=[(False, "bitstring has no BitStream"), (True, "ok")]),
            patch.object(venv, "repair_bitstream_dependency") as repair,
        ):
            self.assertTrue(manager.ensure_env("pdf2zh_next"))
        self.assertEqual(manager.curr_envtool, "conda")
        repair.assert_not_called()

    def test_skip_install_does_not_repair_dependencies(self):
        manager = venv.VirtualEnvManager("missing.json", None, "uv", skip_install=True)
        with (
            patch.object(venv, "load_requirements", return_value=("3.12", [])),
            patch.object(venv, "managed_runtime_health", return_value=(False, "bitstring has no BitStream")),
            patch.object(venv, "repair_bitstream_dependency") as repair,
        ):
            self.assertFalse(manager._requirements_ok("pdf2zh_next", "uv", Path("python")))
        repair.assert_not_called()

    def test_narrow_repair_installs_only_the_bitstream_compatibility_dependency(self):
        python = Path("managed-python")
        with (
            patch.object(lifecycle, "choose_install_sources", return_value=["https://pypi.org/simple"]),
            patch.object(lifecycle, "_run_install", return_value=True) as install,
            patch.object(lifecycle, "managed_runtime_health", return_value=(True, "ok")),
        ):
            self.assertTrue(lifecycle.repair_bitstream_dependency("uv", python))
        install.assert_called_once_with(
            "uv", python, ["bitstring==4.3.1"], ["https://pypi.org/simple"], reinstall=True,
        )

    def test_legacy_bitstream_failure_gets_narrow_repair_without_engine_upgrade(self):
        manager = venv.VirtualEnvManager("missing.json", None, "uv", skip_install=True)
        manager.skip_install = False
        python = Path("managed-python")
        with (
            patch.object(manager, "_existing", return_value=("uv", Path("managed"), python)),
            patch.object(venv, "load_requirements", return_value=("3.12", [])),
            patch.object(venv, "managed_runtime_health", side_effect=[(False, "bitstring has no BitStream"), (True, "ok")]),
            patch.object(venv, "repair_bitstream_dependency", return_value=True) as repair,
            patch.object(venv, "transactional_install_or_update") as update,
        ):
            self.assertTrue(manager.ensure_env("pdf2zh"))
        repair.assert_called_once_with("uv", python, engine="pdf2zh", preferred_index=None)
        update.assert_not_called()

    def test_legacy_narrow_repair_checks_legacy_runtime_only(self):
        python = Path("managed-python")
        with (
            patch.object(lifecycle, "choose_install_sources", return_value=["https://pypi.org/simple"]) as sources,
            patch.object(lifecycle, "_run_install", return_value=True) as install,
            patch.object(lifecycle, "managed_runtime_health", return_value=(True, "ok")) as health,
        ):
            self.assertTrue(lifecycle.repair_bitstream_dependency("uv", python, engine="pdf2zh"))
        self.assertEqual(sources.call_args.args[0], "pdf2zh")
        self.assertEqual(install.call_args.args[2], ["bitstring==4.3.1"])
        health.assert_called_once_with("pdf2zh", python)

    def test_legacy_health_rejects_removed_bitstream_api_in_a_real_subprocess(self):
        real_run = subprocess.run
        for available in (False, True):
            with self.subTest(available=available):
                def simulate_bitstring(command, **kwargs):
                    # Supply a synthetic bitstring only inside the short-lived
                    # probe child. Neither installed packages nor sys.path change.
                    fields = ", BitStream=object" if available else ""
                    prefix = (
                        "import importlib, types, sys; "
                        "fake=types.SimpleNamespace(__file__=sys.prefix+'/bitstring.py'" + fields + "); "
                        "importlib.import_module=lambda name: fake if name=='bitstring' else None; "
                    )
                    return real_run([*command[:-1], prefix + command[-1]], **kwargs)
                with patch.object(lifecycle.subprocess, "run", side_effect=simulate_bitstring):
                    healthy, reason = lifecycle.managed_runtime_health("pdf2zh", Path(sys.executable))
                self.assertEqual(healthy, available, reason)
                if not available:
                    self.assertEqual(
                        reason,
                        "RuntimeError: bitstring 不提供 BitStream；需要 bitstring==4.3.1",
                    )

    def test_legacy_health_preserves_chinese_reason_with_utf8_parent(self):
        # A UTF-8 parent and an isolated child can choose different Windows
        # pipe encodings: -I ignores the inherited PYTHONUTF8 setting.
        result = subprocess.run(
            [
                sys.executable, "-m", "unittest", "discover", "-s", "tests",
                "-p", "test_compatibility_dependencies.py",
                "-k", "test_legacy_health_rejects_removed_bitstream_api_in_a_real_subprocess",
            ],
            cwd=lifecycle.SERVER_ROOT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("UnicodeDecodeError", result.stderr)

    def test_next_engine_uses_environment_python_and_compatibility_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "utils").mkdir()
            runner = root / "engine_runner.py"
            runner.write_text("# test runner\n", encoding="utf-8")
            python = root / "managed" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            manager = venv.VirtualEnvManager("missing.json", None, "uv", skip_install=True)
            manager.curr_envtool = "uv"
            with (
                patch.object(venv, "__file__", str(root / "utils" / "venv.py")),
                patch.object(manager, "ensure_env", return_value=True),
                patch.object(manager, "_existing", return_value=("uv", python.parent.parent, python)),
            ):
                command, env = manager.get_command_and_env(["pdf2zh_next", "paper.pdf", "--openai"])
            self.assertEqual(command, [str(python), "-u", str(runner.resolve()), "pdf2zh_next", "paper.pdf", "--openai"])
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")


if __name__ == "__main__":
    unittest.main()
