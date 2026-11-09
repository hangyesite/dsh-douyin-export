"""Media acquisition: watermark-free MP4 via the public play redirector + ffmpeg."""
from __future__ import annotations

import os
import time

from .util import (
    DESKTOP_UA,
    ShortVideoError,
    ensure_dir,
    ffprobe_json,
    find_ffmpeg,
    http_download,
    log,
    run,
    slugify,
)

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
        raise ShortVideoError(f"作品 {video.get('aweme_id')} 没有可用的流地址",
                       code="no_stream", hint="尝试用 sv_transcribe 重新抓取详情。")

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
                    raise ShortVideoError("响应过小，疑似被拦截", code="tiny_body")
                last = None
                break
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"  ratio={r} 失败: {str(e)[:110]}")
                continue
        if last is None:
            break
    if last is not None:
        raise ShortVideoError(f"下载失败: {last}", code="download_failed")

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


# ------------------------------------------------------------------ 小红书
_API_BLOCKED = False  # 一旦被风控就本轮不再试接口，直接走浏览器


def _xhs_token(row: dict) -> str:
    """从已存的行里找 xsec_token：先看顶层字段，再看 share_url，最后看 raw_json。"""
    tok = str(row.get("xsec_token") or "")
    if tok:
        return tok
    url = str(row.get("share_url") or "")
    marker = "xsec_token="
    if marker in url:
        tail = url.split(marker, 1)[1]
        return tail.split("&", 1)[0]
    raw = row.get("raw_json")
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        return str(raw.get("xsec_token") or raw.get("xsecToken") or "")
    return ""


def download_video_xhs(cfg, row: dict, session=None, base_dir: str | None = None, api=None) -> dict:
    """小红书：靠会话拦到的 mp4 地址纯 HTTP 直下（不需要 cookie/签名）。"""
    from .util import ShortVideoError, http_download

    info: dict = {}
    url = None
    if api is None:
        try:
            from .fetch_xhs import XhsApi, api_available
            if api_available():
                api = XhsApi(cfg)
        except Exception:
            api = None
    global _API_BLOCKED
    if api is not None and _API_BLOCKED:
        api = None
    if api is not None:
        try:
            card = api.feed(row["aweme_id"], _xhs_token(row))
            url = XhsApi.video_url(card)
            info = {"title": card.get("title") or card.get("display_title") or "",
                    "subtitle": (XhsApi.subtitles(card) or {}).get("zh")}
        except ShortVideoError as error:
            if getattr(error, "code", "") not in ("xhs_captcha", "xhs_blocked", "xhs_needs_token"):
                raise
            _API_BLOCKED = True
            log("xhs api blocked (" + str(getattr(error, "code", "")) + "), fallback to browser")
            url = None
    if not url:
        if session is None:
            from .fetch_xhs import XhsSession
            with XhsSession(cfg) as fresh:
                return download_video_xhs(cfg, row, session=fresh, base_dir=base_dir, api=None)
        info = session.note_media(row["aweme_id"], _xhs_token(row))
        url = info.get("mp4")
    if not url:
        raise ShortVideoError("没有拿到视频地址（可能是图文笔记，或需要登录）",
                              code="no_media")
    slug = slugify(row.get("desc") or row["aweme_id"], 26) or row["aweme_id"]
    author = slugify(row.get("author_name") or "xiaohongshu", 24)
    base = base_dir or cfg.output_dir
    vdir = os.path.join(base, "video", author) if base_dir else os.path.join(base, author)
    adir = os.path.join(base, "audio", author) if base_dir else os.path.join(base, author)
    ensure_dir(vdir)
    ensure_dir(adir)
    mp4 = os.path.join(vdir, f"{slug}.mp4")
    size = http_download(url, mp4, headers={
        "User-Agent": DESKTOP_UA,
        "Referer": "https://www.xiaohongshu.com/",
    })
    if size < 20000:
        try:
            os.remove(mp4)
        except OSError:
            pass
        raise ShortVideoError(f"响应过小，疑似被拦截（{size} 字节）", code="blocked")
    ffmpeg_bin, ffprobe_bin = find_ffmpeg(cfg.get("media.ffmpegPath"))
    meta = ffprobe_json(ffprobe_bin, mp4)
    _st = next((x for x in (meta.get("streams") or []) if x.get("codec_type") == "video"), {})
    _fmt = meta.get("format") or {}
    w = _st.get("width") or _fmt.get("width")
    h = _st.get("height") or _fmt.get("height")
    dur = float(_st.get("duration") or _fmt.get("duration") or 0.0)
    wav = os.path.join(adir, f"{slug}.wav")
    run([ffmpeg_bin, "-y", "-i", mp4, "-vn",
         "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", wav], timeout=1800)
    return {"file": mp4, "size": size, "audio": wav,
            "width": w, "height": h, "duration": dur,
            "title": info.get("title") or "", "subtitle": info.get("subtitle")}
