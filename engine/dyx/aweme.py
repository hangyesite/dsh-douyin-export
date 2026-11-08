"""Normalise a raw Douyin `aweme` object into our storage record."""
from __future__ import annotations

import json
import re
from typing import Any

HEX_ONLY = re.compile(r"^[0-9a-f]{64,}$", re.I)


def _first_url(obj: Any) -> str | None:
    """url_list entries are sometimes obfuscated hex blobs; keep only real URLs."""
    if not isinstance(obj, dict):
        return None
    for u in obj.get("url_list") or []:
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def hashtags_of(aweme: dict) -> list[str]:
    tags: list[str] = []
    for t in aweme.get("text_extra") or []:
        name = (t or {}).get("hashtag_name")
        if name:
            tags.append(name)
    if not tags:
        tags = re.findall(r"#([^\s#]{1,30})", aweme.get("desc") or "")
    out, seen = [], set()
    for t in tags:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def best_variant(video: dict) -> tuple[str | None, str]:
    """Return (video_uri, gear_label) of the highest-bitrate rendition."""
    cands = []
    for b in video.get("bit_rate") or []:
        pa = b.get("play_addr") or {}
        uri = pa.get("uri")
        if uri:
            cands.append((int(b.get("bit_rate") or 0), uri,
                          f"{b.get('gear_name') or 'gear'}_{b.get('height') or ''}".strip("_")))
    if cands:
        cands.sort(key=lambda x: -x[0])
        return cands[0][1], cands[0][2]
    uri = (video.get("play_addr") or {}).get("uri")
    return uri, "play_addr"


def normalise(aweme: dict) -> dict:
    video = aweme.get("video") or {}
    author = aweme.get("author") or {}
    stats = aweme.get("statistics") or {}
    images = aweme.get("images") or []
    uri, gear = best_variant(video)

    keep = {
        "aweme_id": aweme.get("aweme_id"),
        "desc": aweme.get("desc"),
        "create_time": aweme.get("create_time"),
        "aweme_type": aweme.get("aweme_type"),
        "author": {k: author.get(k) for k in
                   ("nickname", "sec_uid", "uid", "unique_id", "signature")},
        "statistics": {k: stats.get(k) for k in
                       ("digg_count", "comment_count", "share_count", "collect_count",
                        "play_count")},
        "music": ((aweme.get("music") or {}).get("title")),
        "duration": video.get("duration"),
        "ratio": video.get("ratio"),
        "width": video.get("width"),
        "height": video.get("height"),
        "image_count": len(images),
        "text_extra": [{"hashtag_name": (t or {}).get("hashtag_name")}
                       for t in (aweme.get("text_extra") or [])],
    }

    return {
        "aweme_id": aweme.get("aweme_id"),
        "sec_uid": author.get("sec_uid"),
        "author_name": author.get("nickname"),
        "desc": (aweme.get("desc") or "").strip(),
        "create_time": aweme.get("create_time"),
        "duration_ms": video.get("duration"),
        "width": video.get("width"),
        "height": video.get("height"),
        "play_uri": uri,
        "ratio": gear,
        "cover": _first_url(video.get("cover")) or _first_url(video.get("origin_cover")),
        "image_count": len(images),
        "is_image_post": 1 if images else 0,
        "stats_json": json.dumps(keep["statistics"], ensure_ascii=False),
        "tags": ",".join(hashtags_of(aweme)),
        "raw_json": json.dumps(keep, ensure_ascii=False),
        "_images": [u for img in images for u in (img.get("url_list") or [])
                    if isinstance(u, str) and u.startswith("http")],
    }


def author_record(user: dict) -> dict:
    return {
        "sec_uid": user.get("sec_uid"),
        "uid": user.get("uid"),
        "nickname": user.get("nickname"),
        "unique_id": user.get("unique_id") or user.get("short_id"),
        "signature": user.get("signature"),
        "avatar": _first_url(user.get("avatar_larger")) or _first_url(user.get("avatar_thumb")),
        "aweme_count": user.get("aweme_count"),
        "follower_count": user.get("follower_count"),
        "total_favorited": user.get("total_favorited"),
    }
