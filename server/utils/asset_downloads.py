"""Prepare BabelDOC 0.6.2 assets before its unbounded async warmup.

The pinned runtime embeds the hashes, font metrics and public mirror URLs we
need. Use those local records instead of racing online metadata servers, then
let the unchanged engine consume the verified cache. No installed package or
global HTTP client is patched. Unknown runtimes keep their own asset behavior.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib
import importlib.metadata
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_VERSION = "0.6.2"
SOCKET_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300
MAX_WORKERS = 2
MAX_ROUNDS = 2
_TIKTOKEN_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
_MODEL_NAME = "doclayout_yolo_docstructbench_imgsz1024.onnx"
_LOG_LOCK = threading.Lock()


class AssetDownloadError(RuntimeError):
    """A mandatory asset could not be obtained from any public source."""


class _DownloadStopped(Exception):
    pass


@dataclass(frozen=True)
class Asset:
    name: str
    directory: str
    sha3_256: str
    urls: tuple[str, ...]
    size: int | None = None

    def __post_init__(self):
        if not self.name or self.name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', self.name):
            raise ValueError("Invalid asset filename")
        if self.directory not in {"fonts", "models", "cmap", "tiktoken"}:
            raise ValueError("Invalid asset directory")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha3_256):
            raise ValueError("Invalid asset SHA3-256")
        if self.size is not None and (not isinstance(self.size, int) or self.size <= 0):
            raise ValueError("Invalid asset size")
        if not self.urls:
            raise ValueError("Asset has no download source")
        for url in self.urls:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Asset source must be a public HTTPS URL without credentials")


def _log(level, message):
    with _LOG_LOCK:
        print(f"[asset_download] {level} {message}", file=sys.stderr, flush=True)


def _ordered_sources(mapping, preferred):
    return [name for name in preferred if name in mapping] + [
        name for name in mapping if name not in preferred
    ]


def _manifest(metadata):
    assets = []
    for folder, entries, sources in (
        ("fonts", metadata.EMBEDDING_FONT_METADATA, metadata.FONT_URL_BY_UPSTREAM),
        ("cmap", metadata.CMAP_METADATA, metadata.CMAP_URL_BY_UPSTREAM),
    ):
        order = _ordered_sources(sources, ("github", "modelscope", "huggingface", "hf-mirror"))
        for name, record in entries.items():
            assets.append(Asset(name, folder, record["sha3_256"],
                                tuple(sources[source](name) for source in order), record.get("size")))
    models = metadata.DOC_LAYOUT_ONNX_MODEL_URL
    order = _ordered_sources(models, ("modelscope", "huggingface", "hf-mirror"))
    assets.append(Asset(_MODEL_NAME, "models", metadata.DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
                        tuple(models[source] for source in order)))
    # The 0.6.2 warmup requests gpt-4o, whose o200k_base cache key is the SHA1
    # of the canonical URL. Never guess an unrecorded tokenizer or trust bytes
    # solely because they came from that URL.
    tokenizer_name = hashlib.sha1(_TIKTOKEN_URL.encode()).hexdigest()
    if set(metadata.TIKTOKEN_CACHES) != {tokenizer_name}:
        raise ValueError("Unrecognized BabelDOC tokenizer cache contract")
    assets.append(Asset(tokenizer_name, "tiktoken", metadata.TIKTOKEN_CACHES[tokenizer_name], (_TIKTOKEN_URL,)))
    return assets


def _verified(path, asset):
    try:
        if asset.size is not None and path.stat().st_size != asset.size:
            return False
        digest = hashlib.sha3_256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(256 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == asset.sha3_256
    except OSError:
        return False


def _failure_description(exc):
    # Raw network exceptions can contain proxy credentials or signed redirect
    # URLs. The exception class/status plus the original public host suffices.
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"URLError/{type(exc.reason).__name__}"
    if isinstance(exc, ValueError):
        return "IntegrityError(size/SHA3-256)"
    return type(exc).__name__


def _download(url, destination, asset, opener, stop, *, socket_timeout, download_timeout):
    temporary = None
    deadline = time.monotonic() + download_timeout
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "zotero-pdf2zh-community/assets"})
        with opener(request, timeout=socket_timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise urllib.error.HTTPError(url, status, "Asset download failed", {}, None)
            # HTTPResponse.read(size) may keep filling its buffer forever on
            # a trickling connection. read1 makes at most one socket read, so
            # the wall-clock deadline is checked between network reads.
            # A blocking socket operation can still extend this budget until
            # it finishes or reaches the socket inactivity timeout.
            read_once = getattr(response, "read1", response.read)
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".asset-", suffix=".download",
                                             dir=destination.parent, delete=False) as target:
                temporary = Path(target.name)
                downloaded = 0
                digest = hashlib.sha3_256()
                while True:
                    if stop.is_set():
                        raise _DownloadStopped()
                    if time.monotonic() > deadline:
                        raise TimeoutError("Asset download deadline exceeded")
                    chunk = read_once(256 * 1024)
                    if time.monotonic() > deadline:
                        raise TimeoutError("Asset download deadline exceeded")
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if asset.size is not None and downloaded > asset.size:
                        raise ValueError("Asset exceeds expected size")
                    digest.update(chunk)
                    target.write(chunk)
                if (asset.size is not None and downloaded != asset.size) or digest.hexdigest() != asset.sha3_256:
                    raise ValueError("Asset size or SHA3-256 verification failed")
                target.flush()
                os.fsync(target.fileno())
        if stop.is_set():
            raise _DownloadStopped()
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ensure_asset(asset, root, opener, stop, *, socket_timeout, download_timeout):
    destination = root / asset.directory / asset.name
    if _verified(destination, asset):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    retryable = list(asset.urls)
    for round_number in range(1, MAX_ROUNDS + 1):
        next_round = []
        for url in retryable:
            if stop.is_set():
                raise _DownloadStopped()
            host = urllib.parse.urlsplit(url).hostname
            _log("INFO", f"下载 {asset.directory}/{asset.name} source={host} round={round_number}/{MAX_ROUNDS}")
            try:
                _download(url, destination, asset, opener, stop,
                          socket_timeout=socket_timeout, download_timeout=download_timeout)
                _log("INFO", f"已校验 {asset.directory}/{asset.name} source={host} SHA3-256=OK")
                return True
            except (OSError, ValueError, http.client.HTTPException) as exc:
                description = _failure_description(exc)
                failures.append(f"{host}: {description}")
                _log("WARNING", f"下载失败 {asset.directory}/{asset.name} source={host} error={description}；按备用源与重试预算继续")
                permanent = isinstance(exc, ValueError) or (
                    isinstance(exc, urllib.error.HTTPError)
                    and 400 <= exc.code < 500 and exc.code not in {408, 429}
                )
                if not permanent:
                    next_round.append(url)
        retryable = next_round
        if not retryable:
            break
    raise AssetDownloadError(
        f"必需资源 {asset.directory}/{asset.name} 下载失败；已尝试所有可用来源："
        + "; ".join(failures)
        + "。请检查网络后重试；已校验缓存会保留，无需重新安装。"
    )


def prepare_assets(assets, cache_root, *, opener=None, max_workers=MAX_WORKERS,
                   socket_timeout=SOCKET_TIMEOUT, download_timeout=DOWNLOAD_TIMEOUT):
    """Verify a manifest and download missing entries with bounded concurrency.

    At most two pending jobs are scheduled. On failure no further assets are
    scheduled; another active download stops at its next chunk/socket timeout.
    ``opener`` permits fully offline fallback/integrity/concurrency regression
    tests. Existing files are replaced only after a verified private download.
    """
    if not 1 <= max_workers <= MAX_WORKERS or socket_timeout <= 0 or download_timeout <= 0:
        raise ValueError("Invalid asset download limits")
    root = Path(cache_root)
    pending_assets = [asset for asset in assets if not _verified(root / asset.directory / asset.name, asset)]
    if not pending_assets:
        _log("INFO", f"资源缓存校验通过（{len(assets)} 项），无需下载。")
        return {"status": "cached", "downloaded": 0, "verified": len(assets)}
    _log("INFO", f"首次资源准备：待下载 {len(pending_assets)} 项，最多 {max_workers} 个并发下载。")
    stop = threading.Event()
    downloaded = 0
    iterator = iter(pending_assets)
    open_url = opener or urllib.request.urlopen
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        def submit_next():
            asset = next(iterator, None)
            if asset is None:
                return None
            return pool.submit(_ensure_asset, asset, root, open_url, stop,
                               socket_timeout=socket_timeout, download_timeout=download_timeout)

        pending = {future for _ in range(max_workers) if (future := submit_next()) is not None}
        try:
            while pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                # Consume all finished results before scheduling more: if one
                # failed, another completed job must not trigger new downloads.
                for future in finished:
                    downloaded += bool(future.result())
                for _ in finished:
                    if (future := submit_next()) is not None:
                        pending.add(future)
        except BaseException:
            stop.set()
            for future in pending:
                future.cancel()
            raise
    _log("INFO", f"资源准备完成：新下载 {downloaded} 项，全部 {len(assets)} 项已校验。")
    return {"status": "ready", "downloaded": downloaded, "verified": len(assets)}


def prepare_babeldoc_assets():
    """Preflight only the pinned, verified runtime; leave other versions alone."""
    try:
        version = importlib.metadata.version("BabelDOC")
    except importlib.metadata.PackageNotFoundError:
        return {"status": "skipped", "reason": "BabelDOC is not installed"}
    if version != SUPPORTED_VERSION:
        _log("WARNING", f"BabelDOC {version} 不在资源预检适配范围内，保留该版本原有资源下载流程。")
        return {"status": "skipped", "reason": "unsupported version"}
    metadata = importlib.import_module("babeldoc.assets.embedding_assets_metadata")
    const = importlib.import_module("babeldoc.const")
    try:
        assets = _manifest(metadata)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise AssetDownloadError(f"BabelDOC {version} 资源清单与预期不符（{type(exc).__name__}）；请重新安装受支持的引擎。") from None
    return prepare_assets(assets, const.CACHE_FOLDER)
