"""官方字幕引擎：直接用平台自带字幕（目前是小红书的中文轨）。

好处：零成本、逐字准确、自带时间轴。
代价：只有部分平台/部分笔记有；没有时由调用方回落到别的引擎。
"""

from __future__ import annotations

import re
import ssl
import urllib.request
from typing import Any, Dict, List, Optional

from ..util import ShortVideoError, log

_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_TAG = re.compile(r"<[^>]+>")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


def _sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def parse_srt(text: str) -> List[Dict[str, Any]]:
    """把 SRT 文本解析成 [{start,end,text}]。"""
    segs: List[Dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", (text or "").strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        idx = next((i for i, ln in enumerate(lines) if _TS.search(ln)), None)
        if idx is None:
            continue
        m = _TS.search(lines[idx])
        body = _TAG.sub("", " ".join(lines[idx + 1:])).strip()
        if not body:
            continue
        segs.append({
            "start": round(_sec(*m.groups()[:4]), 3),
            "end": round(_sec(*m.groups()[4:]), 3),
            "text": body,
        })
    # 相邻重复行（同一句被拆两行且内容相同）合并
    out: List[Dict[str, Any]] = []
    for s in segs:
        if out and out[-1]["text"] == s["text"] and s["start"] - out[-1]["end"] < 1.5:
            out[-1]["end"] = s["end"]
            continue
        out.append(s)
    return out


_ABBR = re.compile(r"(?<![A-Za-z])([A-Za-z])[，,]([A-Za-z])[，,]([A-Za-z])(?![A-Za-z])")


def merge_split_abbr(text: str) -> str:
    """平台字幕把缩写拆成了 "n，P,D"，合回 "NPD"。"""
    if not text:
        return text
    return _ABBR.sub(lambda m: (m.group(1) + m.group(2) + m.group(3)).upper(), text)


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.xiaohongshu.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return r.read().decode("utf-8", "replace")


class OfficialEngine:
    """平台官方字幕（小红书中文轨优先）。"""

    name = "official"
    remote = True
    needs_row = True
    needs_audio = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.model_name = "platform-subtitle"

    def transcribe(self, wav_path: str, language: str = "zh",
                   row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        row = row or {}
        platform = (row.get("platform") or "douyin")
        item_id = row.get("aweme_id") or ""
        if platform != "xiaohongshu":
            raise ShortVideoError(
                f"{platform} 没有官方字幕可用", code="no_official_subtitle")
        direct = (row.get("subtitle_url") or "").strip()
        if direct.startswith("http"):
            segs = parse_srt(fetch_text(direct))
            for _s in segs:
                _s["text"] = merge_split_abbr(_s.get("text") or "")
            if segs:
                text = "".join(s["text"] for s in segs)
                return {"engine": "official", "model": self.model_name,
                        "language": "zh", "segments": segs, "text": text,
                        "duration": segs[-1].get("end") or 0.0}

        from ..fetch_xhs import XhsApi, api_available

        if not api_available():
            raise ShortVideoError("官方字幕需要 xhshow（pip install xhshow）",
                                  code="no_official_subtitle")
        api = XhsApi(self.cfg)
        tok = row.get("xsec_token") or ""
        if not tok:
            try:
                import json as _json
                tok = (_json.loads(row.get("raw_json") or "{}") or {}).get("xsec_token") or ""
            except Exception:  # noqa: BLE001
                tok = ""
        card = api.feed(item_id, tok)
        subs = XhsApi.subtitles(card) or {}
        url = subs.get("zh") or subs.get("en")
        if not url:
            raise ShortVideoError(f"这条笔记没有官方字幕：{item_id}",
                                  code="no_official_subtitle")
        segs = parse_srt(fetch_text(url))
        for _s in segs:
            _s["text"] = merge_split_abbr(_s.get("text") or "")
        if not segs:
            raise ShortVideoError(f"官方字幕解析为空：{item_id}",
                                  code="no_official_subtitle")
        lang = "zh" if subs.get("zh") else "en"
        text = "".join(s["text"] for s in segs)
        if lang == "zh" and text and text[-1] not in "。！？!?…":
            text += "。"
        log(f"官方字幕 {len(segs)} 段（{lang}）")
        return {
            "engine": self.name,
            "model": "platform-subtitle",
            "language": lang,
            "segments": segs,
            "text": text,
            "duration": segs[-1]["end"],
        }
