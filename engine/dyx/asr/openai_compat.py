"""Any OpenAI-compatible /audio/transcriptions endpoint (local or cloud)."""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import urllib.error
import urllib.request
import uuid

from ..util import DyxError, find_ffmpeg, log, run

CHUNK_SECONDS = 600


def _multipart(fields: dict[str, str], file_field: str, file_path: str) -> tuple[bytes, str]:
    boundary = "----dyx" + uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    name = os.path.basename(file_path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
    with open(file_path, "rb") as f:
        parts.append(f.read())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class OpenAICompatEngine:
    remote = True
    name = "openai"

    def __init__(self, cfg):
        o = cfg.data["asr"]["openai"]
        self.base = (o.get("baseUrl") or "").rstrip("/")
        self.model_name = o.get("model") or "whisper-1"
        self.key = o.get("apiKey") or os.environ.get("OPENAI_API_KEY") or ""
        self.timeout = int(o.get("timeoutSec") or 600)
        self.cfg = cfg
        if not self.base:
            raise DyxError("未配置 asr.openai.baseUrl", code="asr_unconfigured")

    # ------------------------------------------------------------------ chunks
    def _chunks(self, wav_path: str) -> list[tuple[str, float]]:
        ffmpeg, _ = find_ffmpeg(self.cfg.get("media.ffmpegPath"))
        tmpdir = os.path.dirname(wav_path)
        out: list[tuple[str, float]] = []
        i = 0
        offset = 0.0
        while True:
            dest = os.path.join(tmpdir, f".chunk_{i:03d}.mp3")
            p = run([ffmpeg, "-y", "-v", "error", "-ss", str(offset), "-t", str(CHUNK_SECONDS),
                     "-i", wav_path, "-ac", "1", "-ar", "16000", "-b:a", "64k", dest],
                    timeout=600)
            if p.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 1024:
                break
            out.append((dest, offset))
            offset += CHUNK_SECONDS
            i += 1
            if i > 200:
                break
        if not out:
            out = [(wav_path, 0.0)]
        return out

    # ------------------------------------------------------------------ request
    def _call(self, path: str, language: str) -> dict:
        url = f"{self.base}/audio/transcriptions"
        fields = {"model": self.model_name, "response_format": "verbose_json"}
        if language and language not in ("", "auto"):
            fields["language"] = language
        body, ctype = _multipart(fields, "file", path)
        headers = {"Content-Type": ctype, "User-Agent": "dyx/1.0"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise DyxError(f"ASR 接口返回 {e.code}: {detail}", code="asr_http_error") from e
        except Exception as e:  # noqa: BLE001
            raise DyxError(f"ASR 接口调用失败: {e}", code="asr_call_failed") from e

    def transcribe(self, wav_path: str, language: str = "zh") -> dict:
        segments: list[dict] = []
        total = 0.0
        for path, offset in self._chunks(wav_path):
            log(f"  云端转写 {os.path.basename(path)} (offset {offset:.0f}s)")
            data = self._call(path, language)
            for s in data.get("segments") or []:
                segments.append({
                    "start": round(float(s.get("start", 0)) + offset, 2),
                    "end": round(float(s.get("end", 0)) + offset, 2),
                    "text": (s.get("text") or "").strip(),
                })
            total = max(total, offset + float(data.get("duration") or 0))
            if path != wav_path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        segments = [s for s in segments if s["text"]]
        return {
            "engine": self.name,
            "model": self.model_name,
            "language": language or "auto",
            "duration": round(total, 2),
            "segments": segments,
            "text": "".join(s["text"] for s in segments),
        }
