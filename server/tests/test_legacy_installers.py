import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from warmup import install_managed


class LegacyInstallersTest(unittest.TestCase):
    def test_upgrade_flag_uses_shared_install_boundary_once_per_engine(self):
        with (
            patch.object(install_managed, "transactional_install_or_update", return_value=(True, "uv", Path("next-env"))) as install,
            patch.object(install_managed.subprocess, "run") as run,
        ):
            self.assertEqual(install_managed.main(["--env-tool", "uv", "--upgrade", "--no-mirror"]), 0)
        self.assertEqual([call.args[0] for call in install.call_args_list], ["pdf2zh", "pdf2zh_next"])
        self.assertEqual(install.call_args.kwargs["preferred_index"], "https://pypi.org/simple")
        run.assert_not_called()

    def test_failed_install_does_not_run_warmup_or_switch_managers(self):
        with (
            patch.object(install_managed, "transactional_install_or_update", return_value=(False, "conda", None)) as install,
            patch.object(install_managed.subprocess, "run") as run,
        ):
            self.assertEqual(install_managed.main(["--env-tool", "conda", "--warmup"]), 1)
        self.assertEqual(install.call_count, 1)
        self.assertEqual(install.call_args.kwargs["env_tool"], "conda")
        run.assert_not_called()

    def test_warmup_uses_selected_environment_and_compatibility_runner(self):
        environment = Path("next-env")
        python = environment / "python.exe"
        with (
            patch.object(install_managed, "transactional_install_or_update", return_value=(True, "conda", environment)),
            patch.object(install_managed, "resolve_environment_python", return_value=python),
            patch.object(install_managed, "environment_path_entries", return_value=[environment]),
            patch.object(install_managed.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            self.assertEqual(install_managed.main(["--env-tool", "conda", "--warmup"]), 0)
        self.assertEqual(run.call_args.args[0], [str(python), "-u", str(install_managed.SERVER_ROOT / "engine_runner.py"), "pdf2zh_next", "--warmup"])
        self.assertEqual(run.call_args.kwargs["cwd"], install_managed.SERVER_ROOT)
        self.assertNotIn("PYTHONPATH", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
