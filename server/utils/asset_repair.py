"""Repair the retired RapidOCR URL used by older BabelDOC runtimes.

No installed package is patched. Only the exact upstream asset identified by
its SHA3-256 is cached, and only when the installed sources still request it.
"""
from __future__ import annotations

import ast
import hashlib
import http.client
import importlib.metadata
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


RAPIDOCR_FILENAME = "ch_PP-OCRv4_det_infer.onnx"
RAPIDOCR_SHA3_256 = "062f4619afe91b33147c033acadecbb53f2a7b99ac703d157b96d5b10948da5e"
RAPIDOCR_SIZE = 4_745_517
RAPIDOCR_URLS = (
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/master/onnx/PP-OCRv4/det/ch_PP-OCRv4_det_mobile.onnx",
    "https://hf-mirror.com/spaces/RapidAI/RapidOCR/resolve/main/models/text_det/ch_PP-OCRv4_det_infer.onnx",
    "https://huggingface.co/spaces/RapidAI/RapidOCR/resolve/main/models/text_det/ch_PP-OCRv4_det_infer.onnx",
)


def _babeldoc_package_root() -> Path | None:
    try:
        distribution = importlib.metadata.distribution("BabelDOC")
        return Path(distribution.locate_file("babeldoc"))
    except importlib.metadata.PackageNotFoundError:
        return None


def _legacy_asset_required() -> bool:
    """Recognize the known contract without importing BabelDOC or its assets.

    New BabelDOC releases can remove or replace this model. Checking both the
    recorded hash and the actual cache lookup prevents downloading obsolete
    models merely because old text happens to remain in a metadata file.
    """
    package_root = _babeldoc_package_root()
    if package_root is None:
        return False
    metadata = package_root / "assets" / "embedding_assets_metadata.py"
    assets = package_root / "assets" / "assets.py"
    if not metadata.is_file() or not assets.is_file():
        return False
    try:
        tree = ast.parse(metadata.read_text(encoding="utf-8"))
        found_hash = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id == "TABLE_DETECTION_RAPIDOCR_MODEL_SHA3_256"
                for target in node.targets
            ):
                found_hash = ast.literal_eval(node.value)
                break
        if found_hash != RAPIDOCR_SHA3_256:
            return False
        tree = ast.parse(assets.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_cache_file_path"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Constant)
                and node.args[0].value == RAPIDOCR_FILENAME
                and node.args[1].value == "models"
            ):
                return True
    except (OSError, SyntaxError, ValueError):
        return False
    return False


def _valid_asset(path: Path) -> bool:
    try:
        if path.stat().st_size != RAPIDOCR_SIZE:
            return False
        digest = hashlib.sha3_256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(256 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == RAPIDOCR_SHA3_256
    except OSError:
        return False


def _download_verified_asset(
    url: str,
    destination: Path,
    *,
    timeout: float,
    opener,
) -> None:
    """Validate a private temporary download before atomically publishing it."""
    temporary = None
    try:
        deadline = time.monotonic() + timeout
        request = urllib.request.Request(
            url, headers={"User-Agent": "zotero-pdf2zh-community/asset-repair"},
        )
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise OSError(f"HTTP {status}")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".rapidocr-", suffix=".download",
                dir=destination.parent, delete=False,
            ) as target:
                temporary = Path(target.name)
                downloaded = 0
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("RapidOCR asset download exceeded time limit")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > RAPIDOCR_SIZE:
                        raise ValueError("RapidOCR asset exceeds expected size")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        if not _valid_asset(temporary):
            raise ValueError("RapidOCR asset size or SHA3-256 verification failed")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_legacy_rapidocr_asset(
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    timeout: float = 20,
    retries: int = 2,
    opener=None,
) -> dict:
    """Return status/message/required for the runner's startup diagnostics.

    ``cache_dir`` is the BabelDOC cache root (not its models subdirectory).
    The optional opener and explicit cache root support isolated offline tests.
    Each mirror gets at most ``retries`` attempts, with a hard size/hash gate.
    """
    if not _legacy_asset_required():
        return {
            "status": "skipped", "required": False,
            "message": "当前 BabelDOC 不使用此旧版 RapidOCR 资产，无需修复。",
        }
    if timeout <= 0 or retries < 1 or retries > 3:
        raise ValueError("timeout must be positive and retries must be between 1 and 3")
    root = Path(cache_dir) if cache_dir is not None else Path.home() / ".cache" / "babeldoc"
    destination = root / "models" / RAPIDOCR_FILENAME
    if _valid_asset(destination):
        return {
            "status": "ok", "required": True, "path": str(destination),
            "message": "RapidOCR 模型缓存已通过 SHA3-256 校验。",
        }
    failures = []
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        open_url = opener or urllib.request.urlopen
        for url in RAPIDOCR_URLS:
            for attempt in range(1, retries + 1):
                try:
                    _download_verified_asset(
                        url, destination, timeout=timeout, opener=open_url,
                    )
                    return {
                        "status": "repaired", "required": True,
                        "path": str(destination), "source": url,
                        "message": "已修复旧版 RapidOCR 模型下载地址，缓存通过 SHA3-256 校验。",
                    }
                except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as exc:
                    failures.append(f"{url} (尝试 {attempt}): {type(exc).__name__}: {exc}")
                    # A missing asset or a wrong payload will not improve by
                    # immediately retrying the same URL. Try the next mirror.
                    if isinstance(exc, ValueError) or (
                        isinstance(exc, urllib.error.HTTPError)
                        and 400 <= exc.code < 500 and exc.code not in {408, 429}
                    ):
                        break
    except OSError as exc:
        failures.append(f"cache: {type(exc).__name__}: {exc}")
    return {
        "status": "failed", "required": True, "path": str(destination),
        "message": "旧版 RapidOCR 必需模型无法下载或校验失败；已保留原缓存，请检查网络后重试。",
        "errors": failures,
    }
