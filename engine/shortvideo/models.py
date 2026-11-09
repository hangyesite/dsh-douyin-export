"""Downloadable model bundles (kept out of the plugin package)."""
from __future__ import annotations

import os
import tarfile
import urllib.request

from .util import ShortVideoError, ensure_dir

SENSEVOICE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
)
SENSEVOICE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSEVOICE_WANTED = ("model.int8.onnx", "tokens.txt")


def _stream_extract(url: str, dest_root: str, wanted_suffixes: tuple[str, ...],
                    log) -> list[str]:
    ensure_dir(dest_root)
    log(f"下载模型: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "shortvideo-export/0.2.0"})
    written: list[str] = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        with tarfile.open(fileobj=resp, mode="r|bz2") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                if not member.name.endswith(wanted_suffixes):
                    continue
                target = os.path.join(dest_root, os.path.basename(member.name))
                src = tf.extractfile(member)
                if src is None:
                    continue
                with open(target, "wb") as out:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                written.append(target)
                log(f"  ✓ {os.path.basename(target)} "
                    f"{os.path.getsize(target)/1048576:.1f} MB")
    return written


def install_model(cfg, engine: str = "sensevoice", log=print) -> dict:
    if engine not in ("sensevoice", "sv"):
        raise ShortVideoError(f"暂不支持自动安装 {engine} 的模型", code="unsupported",
                       hint="faster-whisper 模型首次使用时由 HuggingFace 自动下载。")
    dest = os.path.join(cfg.home, "models", SENSEVOICE_DIR)
    plus = os.path.join(cfg.home, "models")
    files = _stream_extract(SENSEVOICE_URL, plus, SENSEVOICE_WANTED, log)
    if not files:
        raise ShortVideoError("模型下载失败", code="download_failed")
    cfg.set("asr.sensevoice.modelDir", dest)
    cfg.save()
    return {"engine": "sensevoice", "modelDir": dest, "files": files}
