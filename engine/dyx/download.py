"""Media acquisition: watermark-free MP4 via the public play redirector + ffmpeg."""
from __future__ import annotations

import os
import time

from .util import DyxError, ensure_dir, ffprobe_json, find_ffmpeg, http_download, log, run, slugify

PLAY_ENDPOINT = "https://www.iesdouyin.com/aweme/v1/play/"
FALLBACK_ENDPOINTS = (
    "https://aweme.snssdk.com/aweme/v1/play/",
    "https://www.douyin.com/aweme/v1/play/",
)


def play_url(uri: str, ratio: str = "1080p", endpoint: str = PLAY_ENDPOINT) -> str:
    return f"{endpoint}?video_id={uri}&ratio={ratio}&line=0"


def date_prefix(create_time: int | None) -> str:
    if not create_time:
        return "00000000"
    return time.strftime("%Y%m%d", time.localtime(int(create_time)))


def target_paths(cfg, video: dict, base_dir: str | None = None) -> tuple[str, str]:
    """给了 base_dir（任务目录）时：视频进 video/，转码音频进 audio/；否则沿用旧的平铺布局。"""
    author = slugify(video.get("author_name") or "unknown", 32)
    stem = f"{date_prefix(video.get('create_time'))}_{video['aweme_id']}_{slugify(video.get('desc') or '', 40)}"
    if base_dir:
        vdir = ensure_dir(os.path.join(base_dir, "video", author))
        adir = ensure_dir(os.path.join(base_dir, "audio", author))
    else:
        vdir = adir = ensure_dir(os.path.join(cfg.output_dir, author))
    return os.path.join(vdir, stem + ".mp4"), os.path.join(adir, stem + ".wav")


def download_video(cfg, video: dict, want_audio: bool = True,
                  base_dir: str | None = None) -> dict:
    uri = video.get("play_uri")
    if not uri:
        raise DyxError(f"作品 {video.get('aweme_id')} 没有可用的流地址",
                       code="no_stream", hint="尝试用 dyx_transcribe 重新抓取详情。")

    ratio = cfg.get("media.preferRatio", "1080p") or "1080p"
    mp4, wav = target_paths(cfg, video, base_dir)
    os.makedirs(os.path.dirname(mp4), exist_ok=True)

    attempts = [ratio, "720p", "540p"]
    last: Exception | None = None
    size = 0
    for r in attempts:
        for ep in (PLAY_ENDPOINT,) + FALLBACK_ENDPOINTS:
            url = play_url(uri, r, ep)
            try:
                log(f"下载 {video['aweme_id']} ratio={r} …")
                size = http_download(url, mp4)
                if size < 20_000:
                    raise DyxError("响应过小，疑似被拦截", code="tiny_body")
                last = None
                break
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"  ratio={r} 失败: {str(e)[:110]}")
                continue
        if last is None:
            break
    if last is not None:
        raise DyxError(f"下载失败: {last}", code="download_failed")

    ffmpeg, ffprobe = find_ffmpeg(cfg.get("media.ffmpegPath"))
    info = ffprobe_json(ffprobe, mp4)
    vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    fmt = info.get("format", {})

    audio_path = None
    if want_audio and any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        p = run([ffmpeg, "-y", "-v", "error", "-i", mp4, "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", wav], timeout=900)
        if p.returncode == 0 and os.path.exists(wav):
            audio_path = wav
        else:
            log(f"  提取音频失败: {p.stderr.strip()[:150]}")

    return {
        "file": mp4,
        "size": os.path.getsize(mp4),
        "audio": audio_path,
        "width": vstream.get("width"),
        "height": vstream.get("height"),
        "codec": vstream.get("codec_name"),
        "duration": float(fmt.get("duration") or 0),
        "bit_rate": fmt.get("bit_rate"),
    }


def download_images(cfg, video: dict, urls: list[str],
                    base_dir: str | None = None) -> list[str]:
    folder = ensure_dir(os.path.join(base_dir or cfg.output_dir,
                                     slugify(video.get("author_name") or "unknown", 32),
                                     f"{date_prefix(video.get('create_time'))}_{video['aweme_id']}_images"))
    out = []
    for i, u in enumerate(urls, 1):
        dest = os.path.join(folder, f"{i:02d}.jpg")
        try:
            http_download(u, dest, timeout=120)
            out.append(dest)
        except Exception as e:  # noqa: BLE001
            log(f"  图 {i} 下载失败: {str(e)[:80]}")
    return out
