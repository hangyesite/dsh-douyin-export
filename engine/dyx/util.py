"""Shared helpers: paths, logging, subprocess, HTTP."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class DyxError(Exception):
    """Base error carrying a machine-readable code."""

    def __init__(self, message: str, code: str = "dyx_error", hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": str(self)}
        if self.hint:
            d["hint"] = self.hint
        return d


class LoginRequired(DyxError):
    def __init__(self, what: str):
        super().__init__(
            f"{what} 需要登录态",
            code="login_required",
            hint="运行 dyx_login 工具，在打开的浏览器里用任意抖音账号扫码登录一次即可（不需要作者本人账号）。",
        )


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def slugify(text: str, limit: int = 48) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text)
    text = re.sub(r"[#＃]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" ", "_")
    if not text:
        text = "untitled"
    return text[:limit].strip("._") or "untitled"


def human_duration(ms: int | float | None) -> str:
    if not ms:
        return "00:00"
    total = int(round(float(ms) / 1000.0))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_ts(seconds: float, style: str = "srt") -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if style == "vtt":
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    if style == "clock":
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------- subprocess

def which(name: str, extra: list[str] | None = None) -> str | None:
    for cand in (extra or []):
        if cand and os.path.isfile(cand):
            return cand
    found = shutil.which(name)
    return found


def find_ffmpeg(configured: str | None = None) -> tuple[str, str]:
    """Return (ffmpeg, ffprobe)."""
    extras = []
    if configured:
        extras.append(configured)
    extras += [r"D:\Tools\bin\ffmpeg.exe", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    ff = which("ffmpeg", extras)
    if not ff:
        raise DyxError(
            "找不到 ffmpeg",
            code="ffmpeg_missing",
            hint="安装 ffmpeg 并加入 PATH，或在 dyx_settings 里设置 ffmpegPath。",
        )
    base = os.path.dirname(ff)
    fp = which("ffprobe", [configured.replace("ffmpeg", "ffprobe") if configured else "", os.path.join(base, "ffprobe.exe"), os.path.join(base, "ffprobe")])
    if not fp:
        raise DyxError("找不到 ffprobe（应与 ffmpeg 同目录）", code="ffprobe_missing")
    return ff, fp


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def ffprobe_json(ffprobe: str, path: str) -> dict:
    p = run([ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path], timeout=120)
    if p.returncode != 0:
        raise DyxError(f"ffprobe 失败: {p.stderr.strip()[:300]}", code="probe_failed")
    return json.loads(p.stdout or "{}")


# ---------------------------------------------------------------- http

def http_get(url: str, headers: dict | None = None, timeout: int = 60,
             retries: int = 3) -> bytes:
    hdrs = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404):
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise DyxError(f"下载失败 {url[:120]}: {last}", code="http_failed")


def http_download(url: str, dest: str, headers: dict | None = None,
                  timeout: int = 300, on_progress=None) -> int:
    hdrs = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}
    if headers:
        hdrs.update(headers)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers=hdrs)
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            if on_progress:
                on_progress(total)
    os.replace(tmp, dest)
    return total


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def write_json(path: str, data) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
