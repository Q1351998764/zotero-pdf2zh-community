import hashlib
import io
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils import asset_downloads as assets


class AssetDownloadsTest(unittest.TestCase):
    payload = b"verified-asset"
    urls = ("https://primary.example/font.ttf", "https://backup.example/font.ttf")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.asset = assets.Asset("font.ttf", "fonts", hashlib.sha3_256(self.payload).hexdigest(),
                                  self.urls, len(self.payload))
        self.destination = self.root / "fonts" / "font.ttf"
        self.log = io.StringIO()
        capture = redirect_stderr(self.log)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_verified_cache_avoids_network(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(self.payload)
        opener = Mock(side_effect=AssertionError("No network allowed"))
        result = assets.prepare_assets([self.asset], self.root, opener=opener)
        self.assertEqual(result, {"status": "cached", "downloaded": 0, "verified": 1})
        opener.assert_not_called()

    def test_timeout_falls_back_before_retrying_same_source(self):
        opener = Mock(side_effect=[TimeoutError(), io.BytesIO(self.payload)])
        result = assets.prepare_assets([self.asset], self.root, opener=opener)
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual([call.args[0].full_url for call in opener.call_args_list], list(self.urls))
        self.assertEqual(opener.call_args_list[0].kwargs["timeout"], 60)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertIn("font.ttf source=primary.example error=TimeoutError", self.log.getvalue())
        self.assertEqual(list(self.destination.parent.glob("*.download")), [])

    def test_bad_digest_falls_back_without_publishing_bad_bytes(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"old-cache")

        def opener(request, **kwargs):
            self.assertEqual(self.destination.read_bytes(), b"old-cache")
            return io.BytesIO(b"wrong-payload!" if request.full_url == self.urls[0] else self.payload)

        assets.prepare_assets([self.asset], self.root, opener=opener)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertIn("IntegrityError", self.log.getvalue())

    def test_all_failures_preserve_old_cache_and_remove_partial_files(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"old-cache")
        opener = Mock(side_effect=lambda *args, **kwargs: io.BytesIO(b"too-large-payload-should-abort"))
        with self.assertRaisesRegex(assets.AssetDownloadError, "fonts/font.ttf.*primary.example.*backup.example"):
            assets.prepare_assets([self.asset], self.root, opener=opener)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(self.destination.read_bytes(), b"old-cache")
        self.assertEqual(list(self.destination.parent.glob("*.download")), [])

    def test_http_404_tries_backup_and_does_not_retry_missing_url(self):
        opener = Mock(side_effect=urllib.error.HTTPError("url", 404, "missing", {}, None))
        with self.assertRaises(assets.AssetDownloadError):
            assets.prepare_assets([self.asset], self.root, opener=opener)
        self.assertEqual(opener.call_count, 2)
        self.assertIn("HTTP 404", self.log.getvalue())

    def test_transient_retries_are_bounded_and_do_not_log_proxy_credentials(self):
        opener = Mock(side_effect=urllib.error.URLError("http://user:private-password@proxy.example"))
        with self.assertRaises(assets.AssetDownloadError) as caught:
            assets.prepare_assets([self.asset], self.root, opener=opener)
        self.assertEqual(opener.call_count, 4)
        self.assertEqual([call.args[0].full_url for call in opener.call_args_list], list(self.urls) * 2)
        self.assertNotIn("private-password", self.log.getvalue() + str(caught.exception))

    def test_partial_socket_failure_never_publishes_file(self):
        class Partial(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise TimeoutError()
                return super().read(3)

            read1 = read

        with self.assertRaises(assets.AssetDownloadError):
            assets.prepare_assets([self.asset], self.root, opener=lambda *args, **kwargs: Partial(self.payload))
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob("*.download")), [])

    def test_trickling_response_uses_single_reads_and_checks_deadline(self):
        clock = [0]

        class Drip(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError("Buffered read could exceed the download deadline")

            def read1(self, size=-1):
                clock[0] = 301
                return b"v"

        self.destination.parent.mkdir()
        with patch.object(assets.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                assets._download(self.urls[0], self.destination, self.asset,
                                 lambda *a, **k: Drip(), threading.Event(),
                                 socket_timeout=60, download_timeout=300)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob("*.download")), [])

    def test_download_deadline_is_enforced(self):
        with patch.object(assets.time, "monotonic", side_effect=[0, 301]):
            with self.assertRaises(TimeoutError):
                self.destination.parent.mkdir()
                assets._download(self.urls[0], self.destination, self.asset,
                                 lambda *a, **k: io.BytesIO(self.payload), threading.Event(),
                                 socket_timeout=60, download_timeout=300)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob("*.download")), [])

    def test_download_concurrency_is_bounded(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = maximum = 0

        class SlowResponse(io.BytesIO):
            def __enter__(inner):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                barrier.wait(timeout=3)
                time.sleep(0.01)
                return inner

            def __exit__(inner, *args):
                nonlocal active
                with lock:
                    active -= 1
                super().__exit__(*args)

        manifest = [assets.Asset(f"font-{i}.ttf", "fonts", self.asset.sha3_256, self.urls, len(self.payload)) for i in range(4)]
        result = assets.prepare_assets(manifest, self.root, opener=lambda *a, **k: SlowResponse(self.payload))
        self.assertEqual(result["downloaded"], 4)
        self.assertEqual(maximum, 2)

    def test_failure_does_not_schedule_rest_of_manifest(self):
        manifest = [assets.Asset(f"font-{i}.ttf", "fonts", self.asset.sha3_256, self.urls) for i in range(20)]
        opener = Mock(side_effect=urllib.error.HTTPError("url", 404, "missing", {}, None))
        with self.assertRaises(assets.AssetDownloadError):
            assets.prepare_assets(manifest, self.root, opener=opener, max_workers=1)
        self.assertEqual(opener.call_count, 2)


class AssetManifestTest(unittest.TestCase):
    def metadata(self):
        digest = "a" * 64
        return SimpleNamespace(
            EMBEDDING_FONT_METADATA={"font.ttf": {"sha3_256": digest, "size": 123}},
            FONT_URL_BY_UPSTREAM={"huggingface": lambda name: f"https://hf.example/{name}",
                                  "github": lambda name: f"https://github.example/{name}"},
            CMAP_METADATA={"cmap.json": {"sha3_256": digest, "size": 12}},
            CMAP_URL_BY_UPSTREAM={"github": lambda name: f"https://github.example/{name}"},
            DOC_LAYOUT_ONNX_MODEL_URL={"huggingface": "https://hf.example/model", "modelscope": "https://modelscope.example/model"},
            DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256=digest,
            TIKTOKEN_CACHES={hashlib.sha1(assets._TIKTOKEN_URL.encode()).hexdigest(): digest},
        )

    def test_manifest_includes_fonts_models_cmaps_and_tokenizer(self):
        manifest = assets._manifest(self.metadata())
        self.assertEqual({asset.directory for asset in manifest}, {"fonts", "models", "cmap", "tiktoken"})
        self.assertEqual(manifest[0].urls[0], "https://github.example/font.ttf")
        self.assertEqual(manifest[2].urls[0], "https://modelscope.example/model")
        self.assertEqual(manifest[0].size, 123)

    def test_unknown_tokenizer_contract_is_not_silently_incomplete(self):
        metadata = self.metadata()
        metadata.TIKTOKEN_CACHES["unknown"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "tokenizer"):
            assets._manifest(metadata)

    def test_unknown_versions_preserve_upstream_behavior_without_import_or_download(self):
        with (patch.object(assets.importlib.metadata, "version", return_value="0.5.0"),
              patch.object(assets.importlib, "import_module") as load,
              patch.object(assets, "prepare_assets") as prepare,
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(assets.prepare_babeldoc_assets()["status"], "skipped")
        load.assert_not_called()
        prepare.assert_not_called()
        self.assertIn("0.5.0", stderr.getvalue())

    def test_supported_version_uses_installed_cache_and_manifest(self):
        with (patch.object(assets.importlib.metadata, "version", return_value="0.6.2"),
              patch.object(assets.importlib, "import_module", side_effect=[self.metadata(), SimpleNamespace(CACHE_FOLDER=Path("test-cache"))]),
              patch.object(assets, "prepare_assets", return_value={"status": "ready"}) as prepare):
            self.assertEqual(assets.prepare_babeldoc_assets()["status"], "ready")
        self.assertEqual(prepare.call_args.args[1], Path("test-cache"))
        self.assertEqual(len(prepare.call_args.args[0]), 4)

    def test_filenames_cannot_escape_cache_directory(self):
        for filename in ("../private", "..\\private", "C:private", "x\nERROR", ".."):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                assets.Asset(filename, "fonts", "a" * 64, ("https://example.com/font",))


if __name__ == "__main__":
    unittest.main()
