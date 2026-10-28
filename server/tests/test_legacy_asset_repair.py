import hashlib
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from utils import asset_repair


class LegacyAssetRepairTest(unittest.TestCase):
    payload = b"verified-test-model"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.destination = self.cache / "models" / asset_repair.RAPIDOCR_FILENAME
        for target, value in (
            ("_legacy_asset_required", True),
            ("RAPIDOCR_SIZE", len(self.payload)),
            ("RAPIDOCR_SHA3_256", hashlib.sha3_256(self.payload).hexdigest()),
        ):
            context = patch.object(asset_repair, target, return_value=value) if target.startswith("_") else patch.object(asset_repair, target, value)
            context.start()
            self.addCleanup(context.stop)

    def test_valid_cache_avoids_network(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(self.payload)
        opener = Mock(side_effect=AssertionError("network must not be used"))
        result = asset_repair.ensure_legacy_rapidocr_asset(cache_dir=self.cache, opener=opener)
        self.assertEqual(result["status"], "ok")
        opener.assert_not_called()

    def test_new_runtime_without_asset_does_not_create_cache(self):
        opener = Mock()
        with patch.object(asset_repair, "_legacy_asset_required", return_value=False):
            result = asset_repair.ensure_legacy_rapidocr_asset(cache_dir=self.cache, opener=opener)
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["required"])
        self.assertFalse(self.cache.exists())
        opener.assert_not_called()

    def test_404_falls_back_to_next_mirror_and_publishes_verified_file(self):
        opener = Mock(side_effect=[urllib.error.HTTPError("url", 404, "missing", {}, None), io.BytesIO(self.payload)])
        result = asset_repair.ensure_legacy_rapidocr_asset(cache_dir=self.cache, opener=opener)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(opener.call_args_list[1].args[0].full_url, asset_repair.RAPIDOCR_URLS[1])
        self.assertEqual(list(self.destination.parent.glob("*.download")), [])

    def test_transient_failure_has_bounded_retry(self):
        opener = Mock(side_effect=[urllib.error.URLError("temporary"), io.BytesIO(self.payload)])
        result = asset_repair.ensure_legacy_rapidocr_asset(cache_dir=self.cache, opener=opener)
        self.assertEqual(result["status"], "repaired")
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(opener.call_args_list[0].args[0].full_url, opener.call_args_list[1].args[0].full_url)

    def test_bad_download_never_replaces_existing_cache(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"old-cache")
        opener = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(b"bad-checksum"))
        result = asset_repair.ensure_legacy_rapidocr_asset(cache_dir=self.cache, opener=opener)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["required"])
        self.assertEqual(self.destination.read_bytes(), b"old-cache")
        self.assertEqual(opener.call_count, len(asset_repair.RAPIDOCR_URLS))
        self.assertEqual(list(self.destination.parent.glob("*.download")), [])

    def test_repeated_failures_stop_after_attempt_budget(self):
        opener = Mock(side_effect=urllib.error.URLError("offline"))
        result = asset_repair.ensure_legacy_rapidocr_asset(cache_dir=self.cache, opener=opener, retries=2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(opener.call_count, 2 * len(asset_repair.RAPIDOCR_URLS))
        self.assertFalse(self.destination.exists())


class AssetContractDetectionTest(unittest.TestCase):
    def test_only_the_exact_installed_asset_contract_is_recognized(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder)
            assets = package / "assets"
            assets.mkdir()
            metadata = assets / "embedding_assets_metadata.py"
            metadata.write_text(
                "TABLE_DETECTION_RAPIDOCR_MODEL_SHA3_256 = " + repr(asset_repair.RAPIDOCR_SHA3_256), encoding="utf-8",
            )
            consumer = assets / "assets.py"
            consumer.write_text(
                'def model():\n    return get_cache_file_path("ch_PP-OCRv4_det_infer.onnx", "models")\n', encoding="utf-8",
            )
            with patch.object(asset_repair, "_babeldoc_package_root", return_value=package):
                self.assertTrue(asset_repair._legacy_asset_required())
                consumer.write_text("# new runtime removed this asset\n", encoding="utf-8")
                self.assertFalse(asset_repair._legacy_asset_required())


if __name__ == "__main__":
    unittest.main()
