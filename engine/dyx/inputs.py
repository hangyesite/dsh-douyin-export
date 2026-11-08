"""Accept whatever a person pastes and turn it into authors and video ids.

Supported shapes, in any mix, separated by newlines / spaces / full-width commas:

  * 作者主页 URL   https://www.douyin.com/user/MS4wLjABAAAA...
  * 纯 sec_uid     MS4wLjABAAAA...
  * 作品 URL       https://www.douyin.com/video/7700158942361664842
  * 图文作品 URL   https://www.douyin.com/note/7700158942361664842
  * 纯作品 ID      7700158942361664842
  * 抖音分享口令   "7.42 复制打开抖音，看看【…】 https://v.douyin.com/iRxxxxx/ …"

Short links are resolved by following the redirect instead of guessing.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request

from .util import DESKTOP_UA, DyxError, log

SEC_RE = re.compile(r"MS4wLjABAAAA[\w\-]{10,}")
USER_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:douyin|iesdouyin)\.com/(?:share/)?user/(MS4wLjABAAAA[\w\-]+)")
VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:douyin|iesdouyin)\.com/(?:share/)?(?:video|note|slides)/(\d{15,25})")
SHORT_URL_RE = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_\-]+")
ID_RE = re.compile(r"(?<!\d)(\d{15,25})(?!\d)")


def _follow_short(url: str, hops: int = 5) -> str:
    """Resolve a v.douyin.com short link to its final URL by following redirects."""
    current = url
    for _ in range(hops):
        req = urllib.request.Request(current, method="GET",
                                     headers={"User-Agent": DESKTOP_UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                final = resp.geturl()
                if final and final != current:
                    return final
                body = resp.read(200_000).decode("utf-8", "replace")
                # the share page carries the target in a meta refresh or router data
                m = (VIDEO_URL_RE.search(body) or USER_URL_RE.search(body)
                     or ID_RE.search(body) or SEC_RE.search(body))
                return m.group(1) if m and m.groups() else (m.group(0) if m else current)
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if loc:
                current = loc
                continue
            raise DyxError(f"短链解析失败：{url}（HTTP {e.code}）", code="bad_short_link") from e
        except Exception as e:  # noqa: BLE001
            raise DyxError(f"短链解析失败：{url}（{e}）", code="bad_short_link") from e
    return current


def classify(raw: str, resolve_short: bool = True) -> dict:
    """Classify one pasted chunk into {'kind', 'value', 'raw'}."""
    text = (raw or "").strip()
    if not text:
        return {"kind": "empty", "value": None, "raw": raw}

    m = USER_URL_RE.search(text)
    if m:
        return {"kind": "author", "value": m.group(1), "raw": raw}

    m = VIDEO_URL_RE.search(text)
    if m:
        return {"kind": "video", "value": m.group(1), "raw": raw}

    m = SHORT_URL_RE.search(text)
    if m:
        if not resolve_short:
            return {"kind": "short", "value": m.group(0), "raw": raw}
        final = _follow_short(m.group(0))
        hit = classify(final, resolve_short=False)
        if hit["kind"] in ("author", "video"):
            return {"kind": hit["kind"], "value": hit["value"], "raw": raw,
                    "resolved": final}
        return {"kind": "unknown", "value": final, "raw": raw, "resolved": final}

    m = SEC_RE.search(text)
    if m:
        return {"kind": "author", "value": m.group(0), "raw": raw}

    m = ID_RE.search(text)
    if m:
        return {"kind": "video", "value": m.group(1), "raw": raw}

    return {"kind": "unknown", "value": None, "raw": raw}


def split_chunks(text: str) -> list[str]:
    """Split a pasted blob into one chunk per non-empty line."""
    return [ln for ln in re.split(r"[\r\n]+", text or "") if ln.strip()]


def parse_targets(text: str, resolve_short: bool = True) -> dict:
    """Return {'authors': [...], 'videos': [...], 'unknown': [...], 'lines': n}."""
    authors: list[str] = []
    videos: list[str] = []
    unknown: list[str] = []
    chunks = split_chunks(text)
    for chunk in chunks:
        try:
            hit = classify(chunk, resolve_short=resolve_short)
        except DyxError as e:
            log(f"跳过：{e}")
            unknown.append(chunk)
            continue
        if hit["kind"] == "author" and hit["value"] not in authors:
            authors.append(hit["value"])
        elif hit["kind"] == "video" and hit["value"] not in videos:
            videos.append(hit["value"])
        elif hit["kind"] in ("unknown", "empty"):
            unknown.append(chunk)
    return {"authors": authors, "videos": videos, "unknown": unknown,
            "lines": len(chunks)}
