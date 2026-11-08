"""Pretty-printing transcripts into several formats."""
from __future__ import annotations

import html
import json
import os
import time

from .util import DyxError, fmt_ts, human_duration, slugify

SUPPORTED = ("md", "txt", "srt", "vtt", "json", "html", "docx")


# ------------------------------------------------------------------ helpers
TERMINAL = "。！？…!?;；"


def _needs_space(left: str, right: str) -> bool:
    """Insert a space when gluing two Latin fragments, never between CJK."""
    if not left or not right:
        return False
    tail = left[-1]
    head = right[0]
    def cjk(ch: str) -> bool:
        return "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef"
    return (not cjk(tail)) and (not cjk(head)) and tail.strip() != "" and head.strip() != ""


def _join(left: str, right: str) -> str:
    return left + (" " if _needs_space(left, right) else "") + right


def parse_replacements(pairs) -> list[tuple[str, str]]:
    """Turn ``["错=对", ...]`` into ordered (wrong, right) tuples.

    Lets a user repair recurring domain terms and names without transcribing
    the audio again: exports always read the current rule list.
    """
    out: list[tuple[str, str]] = []
    for item in pairs or []:
        if isinstance(item, str) and "=" in item:
            wrong, right = item.split("=", 1)
            wrong = wrong.strip()
            if wrong:
                out.append((wrong, right.strip()))
        elif isinstance(item, dict):
            wrong = str(item.get("from") or "").strip()
            if wrong:
                out.append((wrong, str(item.get("to") or "").strip()))
    return out


def apply_replacements(text: str, fixes: list[tuple[str, str]]) -> str:
    for wrong, right in fixes:
        text = text.replace(wrong, right)
    return text


def apply_fixes_to_segments(segments, fixes=None):
    """把「错→正」术语应用到每段文本（长词优先），返回新列表，不改原数据。"""
    if not fixes or not segments:
        return list(segments or [])
    order = sorted(fixes.items(), key=lambda kv: -len(kv[0]))
    out = []
    for seg in segments:
        item = dict(seg)
        text = item.get("text") or ""
        for wrong, right in order:
            if wrong and wrong in text:
                text = text.replace(wrong, right)
        item["text"] = text
        out.append(item)
    return out


def paragraphs(segments: list[dict], gap: float = 2.0, max_chars: int = 120,
               min_chars: int = 70, replacements: list | None = None) -> list[dict]:
    """Group ASR segments into readable paragraphs.

    A paragraph keeps absorbing sentences while the pause stays under ``gap``
    and the character budget is not spent. Once it already reaches
    ``min_chars`` and ends on sentence punctuation, the next sentence opens a
    new paragraph, so long monologues break on sentence boundaries instead of
    mid-clause.
    """
    fixes = parse_replacements(replacements)
    out: list[dict] = []
    for s in segments:
        text = apply_replacements((s.get("text") or "").strip(), fixes)
        if not text:
            continue
        if out:
            prev = out[-1]
            bridge = s["start"] - prev["end"]
            joined = _join(prev["text"], text)
            complete = len(prev["text"]) >= min_chars and prev["text"].endswith(tuple(TERMINAL))
            if bridge <= gap and len(joined) <= max_chars and not complete:
                prev["text"] = joined
                prev["end"] = s["end"]
                continue
        out.append({"start": s["start"], "end": s["end"], "text": text})
    return out


def _yaml_escape(v: str) -> str:
    v = (v or "").replace("\\", "\\\\").replace('"', '\\"')
    v = v.replace("\n", " ").replace("\r", " ")
    return f'"{v}"'


def _iso(ts: int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(int(ts)))


def front_matter(video: dict, transcript: dict, include_stats: bool = False) -> str:
    stats = {}
    try:
        stats = json.loads(video.get("stats_json") or "{}")
    except Exception:  # noqa: BLE001
        pass
    tags = [t for t in (video.get("tags") or "").split(",") if t]
    lines = ["---"]
    lines.append(f"title: {_yaml_escape(video.get('desc') or video['aweme_id'])}")
    lines.append(f"aweme_id: {_yaml_escape(video['aweme_id'])}")
    lines.append(f"url: {_yaml_escape('https://www.douyin.com/video/' + video['aweme_id'])}")
    lines.append(f"author: {_yaml_escape(video.get('author_name') or '')}")
    raw = {}
    try:
        raw = json.loads(video.get("raw_json") or "{}")
    except Exception:  # noqa: BLE001
        pass
    uid = (raw.get("author") or {}).get("unique_id")
    if uid:
        lines.append(f"author_id: {_yaml_escape(uid)}")
    if raw.get("author", {}).get("sec_uid"):
        lines.append(f"author_sec_uid: {_yaml_escape(raw['author']['sec_uid'])}")
    if video.get("create_time"):
        lines.append(f"published: {_yaml_escape(_iso(video['create_time']))}")
    if video.get("duration_ms"):
        lines.append(f"duration: {_yaml_escape(human_duration(video['duration_ms']))}")
        lines.append(f"duration_seconds: {round((video['duration_ms'] or 0) / 1000, 2)}")
    mw, mh = video.get("media_width"), video.get("media_height")
    w, h = (mw, mh) if (mw and mh) else (video.get("width"), video.get("height"))
    if w and h:
        lines.append(f"resolution: {_yaml_escape('%dx%d' % (w, h))}")
    if stats and include_stats:
        lines.append("stats:")
        for k, label in (("digg_count", "likes"), ("comment_count", "comments"),
                         ("share_count", "shares"), ("collect_count", "collects")):
            if stats.get(k) is not None:
                lines.append(f"  {label}: {stats.get(k)}")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {_yaml_escape(t)}")
    lines.append("transcript:")
    lines.append(f"  engine: {_yaml_escape(transcript.get('engine') or '')}")
    lines.append(f"  model: {_yaml_escape(transcript.get('model') or '')}")
    lines.append(f"  language: {_yaml_escape(transcript.get('language') or '')}")
    lines.append(f"  segments: {len(transcript.get('segments') or [])}")
    if transcript.get("duration"):
        lines.append(f"  audio_seconds: {round(float(transcript['duration']), 2)}")
    lines.append(f"exported: {_yaml_escape(time.strftime('%Y-%m-%dT%H:%M:%S%z'))}")
    lines.append("---")
    return "\n".join(lines)


# ------------------------------------------------------------------ renderers
def render_md(video: dict, transcript: dict, cfg) -> str:
    style = cfg.get("export.timestampStyle", "clock")
    gap = float(cfg.get("export.paragraphGapSec") or 2.0)
    paras = paragraphs(transcript.get("segments") or [], gap=gap,
                       replacements=cfg.get("asr.replacements"))
    title = (video.get("desc") or video["aweme_id"]).split("\n")[0][:80]
    parts = [front_matter(video, transcript, _with_stats(cfg)), "", f"# {title}", ""]
    when = published_on(video)
    meta = [video.get("author_name") or ""]
    if _with_published(cfg) and when:
        meta.append(when)
    meta.append(human_duration(video.get("duration_ms")))
    if _with_link(cfg):
        meta.append(f"[原视频](https://www.douyin.com/video/{video['aweme_id']})")
    parts.append("> " + " · ".join([m for m in meta if m]))
    parts.append("")
    if _with_stats(cfg):
        line = _stats_line(video, cfg)
        if line:
            parts.append(f"> {line}")
            parts.append("")
    for p in paras:
        prefix = f"[{fmt_ts(p['start'], 'clock')}] " if style != "none" else ""
        parts.append(f"{prefix}{p['text']}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_txt(video: dict, transcript: dict, cfg) -> str:
    style = cfg.get("export.timestampStyle", "clock")
    gap = float(cfg.get("export.paragraphGapSec") or 2.0)
    paras = paragraphs(transcript.get("segments") or [], gap=gap,
                       replacements=cfg.get("asr.replacements"))
    head = [video.get("desc") or video["aweme_id"], f"作者: {video.get('author_name') or ''}"]
    if _with_link(cfg):
        head.append(f"链接: https://www.douyin.com/video/{video['aweme_id']}")
    if _with_published(cfg) and published_on(video):
        head.append(f"发布: {published_on(video)}")
    if _with_stats(cfg):
        line = _stats_line(video, cfg)
        if line:
            head.append(line)
    head.append("")
    body = [f"[{fmt_ts(p['start'], 'clock')}] {p['text']}" if style != "none" else p["text"]
            for p in paras]
    return "\n".join(head + body) + "\n"


def fixed_segments(transcript: dict, cfg) -> list[dict]:
    """Segments with the user's term fixes applied, for the cue-based formats."""
    fixes = parse_replacements(cfg.get("asr.replacements"))
    if not fixes:
        return list(transcript.get("segments") or [])
    return [{**seg, "text": apply_replacements(str(seg.get("text") or ""), fixes)}
            for seg in transcript.get("segments") or []]


def render_srt(video: dict, transcript: dict, cfg) -> str:
    out = []
    for i, s in enumerate(fixed_segments(transcript, cfg), 1):
        out.append(str(i))
        out.append(f"{fmt_ts(s['start'], 'srt')} --> {fmt_ts(s['end'], 'srt')}")
        out.append(s["text"])
        out.append("")
    return "\n".join(out)


def render_vtt(video: dict, transcript: dict, cfg) -> str:
    out = ["WEBVTT", ""]
    for s in fixed_segments(transcript, cfg):
        out.append(f"{fmt_ts(s['start'], 'vtt')} --> {fmt_ts(s['end'], 'vtt')}")
        out.append(s["text"])
        out.append("")
    return "\n".join(out)


def render_json(video: dict, transcript: dict, cfg) -> str:
    payload = {
        "video": {k: video.get(k) for k in
                  ("aweme_id", "desc", "author_name", "sec_uid", "create_time",
                   "duration_ms", "width", "height", "play_uri", "file_path",
                   "file_size", "tags", "stats_json", "raw_json")},
        "transcript": transcript,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_html(video: dict, transcript: dict, cfg) -> str:
    gap = float(cfg.get("export.paragraphGapSec") or 2.0)
    paras = paragraphs(transcript.get("segments") or [], gap=gap,
                       replacements=cfg.get("asr.replacements"))
    title = html.escape((video.get("desc") or video["aweme_id"]))
    ts_on = _ts_on(cfg)
    rows = "\n".join(
        (f'<p><time>{fmt_ts(p["start"], "clock")}</time>{html.escape(p["text"])}</p>'
         if ts_on else f'<p>{html.escape(p["text"])}</p>')
        for p in paras)
    _wm = [html.escape(video.get('author_name') or ''), human_duration(video.get('duration_ms'))]
    if _with_link(cfg):
        _wm.append(f'<a href="https://www.douyin.com/video/{video["aweme_id"]}">原视频</a>')
    web_meta = " · ".join([x for x in _wm if x])
    stats_block = ""
    if _with_stats(cfg) and _stats_line(video, cfg):
        stats_block = f'<div class="meta">{html.escape(_stats_line(video, cfg))}</div>' 
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
:root{{color-scheme:light dark}}
body{{max-width:46rem;margin:3rem auto;padding:0 1.25rem;
font:16px/1.85 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
h1{{font-size:1.5rem;line-height:1.4}}
.meta{{color:#888;font-size:.9rem;margin-bottom:2rem}}
time{{display:inline-block;min-width:3.6rem;color:#c0392b;font-variant-numeric:tabular-nums;
font-size:.85rem;margin-right:.6rem}}
p{{margin:.55rem 0}}
</style></head><body>
<h1>{title}</h1>
<div class="meta">{web_meta}</div>
{stats_block}
{rows}
</body></html>
"""


def render_docx(video: dict, transcript: dict, cfg, path: str) -> str:
    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError as e:
        raise DyxError("未安装 python-docx", code="dep_missing",
                       hint="pip install python-docx") from e
    gap = float(cfg.get("export.paragraphGapSec") or 2.0)
    paras = paragraphs(transcript.get("segments") or [], gap=gap,
                       replacements=cfg.get("asr.replacements"))
    doc = _docx_document(cfg)
    doc.add_heading((video.get("desc") or video["aweme_id"]).split("\n")[0][:80], level=1)
    meta_text = f"{video.get('author_name') or ''} · {human_duration(video.get('duration_ms'))}"
    if _with_link(cfg):
        meta_text += f" · https://www.douyin.com/video/{video['aweme_id']}"
    meta = doc.add_paragraph(meta_text)
    meta.runs[0].font.size = Pt(9)
    if _with_stats(cfg):
        line = _stats_line(video, cfg)
        if line:
            doc.add_paragraph(line)
    ts_on = _ts_on(cfg)
    for p in paras:
        para = doc.add_paragraph()
        if ts_on:
            run = para.add_run(f"[{fmt_ts(p['start'], 'clock')}] ")
            run.bold = True
        para.add_run(p["text"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    doc.save(path)
    _docx_rewrite_fonts(path, _docx_font_name(cfg))
    return path


# ---------------------------------------------------- 导出高级设置小工具
def _stats_of(video: dict) -> dict:
    try:
        return json.loads(video.get("stats_json") or "{}") or {}
    except Exception:  # noqa: BLE001
        return {}


def _num(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    if n >= 10000:
        text = f"{n / 10000:.1f}万"
        return text.replace(".0万", "万")
    return str(n)


STATS_FIELDS = (("digg_count", "点赞"), ("comment_count", "评论"),
                ("share_count", "转发"), ("collect_count", "收藏"))


def _with_link(cfg) -> bool:
    """勾了「原文链接」才在正文里写抖音链接（front-matter 里一直有 url）。"""
    return bool(cfg.get("export.includeLink"))


def published_on_of(video: dict, cfg) -> str:
    return published_on(video) if _with_published(cfg) else ""


def _with_published(cfg) -> bool:
    """勾了「发布日期」才把发布日期写进正文（front-matter 里一直都有）。"""
    return bool(cfg.get("export.includePublished"))


def published_on(video: dict) -> str:
    """发布日的 YYYY-MM-DD；取不到就空串。"""
    raw = video.get("create_time")
    try:
        sec = int(raw)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(sec, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _merge_order(cfg) -> str:
    """合集排序：desc=最新在前（默认），asc=最老在前。"""
    return "asc" if str(cfg.get("export.mergeOrder") or "desc").lower() == "asc" else "desc"


def _ordered_items(items: list, cfg) -> list:
    rev = _merge_order(cfg) == "desc"
    return sorted(items, key=lambda it: int((it.get("video") or {}).get("create_time") or 0), reverse=rev)


def _stats_fields(cfg) -> list:
    """要导出的互动数据项；空列表=不导。只导数字，不导评论内容。"""
    raw = cfg.get("export.statsFields")
    if isinstance(raw, str):
        raw = [p for p in raw.replace(",", " ").split() if p]
    if isinstance(raw, list) and raw:
        known = {k for k, _ in STATS_FIELDS}
        picked = [k for k in (str(x).strip() for x in raw) if k in known]
        if picked:
            return picked
    # 兼容老开关：includeStats 打开但没有细项时，四项全导
    if cfg.get("export.includeStats"):
        return [k for k, _ in STATS_FIELDS]
    return []


def _stats_line(video: dict, cfg=None) -> str:
    """一行互动数据，如「点赞 8478 · 评论 181」；按配置逐项选择。"""
    st = _stats_of(video)
    fields = _stats_fields(cfg) if cfg is not None else [k for k, _ in STATS_FIELDS]
    parts = []
    for key, label in STATS_FIELDS:
        if key in fields and st.get(key) is not None:
            parts.append(f"{label} {_num(st.get(key))}")
    return " · ".join(parts)


def _with_stats(cfg) -> bool:
    return bool(_stats_fields(cfg))


def _ts_on(cfg) -> bool:
    return str(cfg.get("export.timestampStyle") or "none").lower() != "none"


RENDERERS = {
    "md": render_md, "txt": render_txt, "srt": render_srt,
    "vtt": render_vtt, "json": render_json, "html": render_html,
}


def export_one(cfg, video: dict, transcript: dict, formats: list[str],
               out_dir: str | None = None, fixes: dict | None = None) -> list[dict]:
    if fixes:
        transcript = dict(transcript or {})
        transcript["segments"] = apply_fixes_to_segments(transcript.get("segments"), fixes)
    base = (
        os.path.join(out_dir, "export") if out_dir
        else (os.path.dirname(video.get("file_path") or "") or cfg.output_dir)
    )
    stem = os.path.splitext(os.path.basename(video.get("file_path") or ""))[0]
    if not stem:
        stem = f"{video['aweme_id']}_{slugify(video.get('desc') or '', 40)}"
    os.makedirs(base, exist_ok=True)
    written = []
    for fmt in formats:
        fmt = fmt.lower().strip()
        if fmt not in SUPPORTED:
            raise DyxError(f"不支持的导出格式: {fmt}", code="bad_format",
                           hint=f"可选：{', '.join(SUPPORTED)}")
        # 多格式导出：每种格式各占一个文件夹（md/ txt/ srt/ …），找起来清爽
        folder = os.path.join(base, fmt) if out_dir else base
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{stem}.{fmt}")
        if fmt == "docx":
            render_docx(video, transcript, cfg, path)
        else:
            text = RENDERERS[fmt](video, transcript, cfg)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        written.append({"format": fmt, "path": path,
                        "bytes": os.path.getsize(path)})
    return written



def _docx_font_name(cfg) -> str:
    return str((cfg.get("export.docxFont") if cfg is not None else "") or "微软雅黑")


def _docx_rewrite_fonts(path: str, font: str) -> None:
    """把 docx 里残留的主题字体（asciiTheme/eastAsiaTheme…）与 en-US 的 eastAsia 语言全部改掉。

    python-docx 只暴露部分样式，*Char 之类的链接样式仍是主题字体，Word 会拿它渲染标题，
    加上 eastAsia 语言是 en-US，中文就会按日文字形选字（看着像异体字）。这里直接改 XML 兜底。
    """
    import re
    import shutil
    import zipfile

    src = zipfile.ZipFile(path)
    items = [(info, src.read(info.filename)) for info in src.infolist()]
    src.close()

    def fix_tag(tag: str) -> str:
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            tag = re.sub(r'\s+w:%s="[^"]*"' % attr, "", tag)
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            if re.search(r'\sw:%s="' % attr, tag):
                tag = re.sub(r'\sw:%s="[^"]*"' % attr, ' w:%s="%s"' % (attr, font), tag)
            else:
                tag = tag.replace("<w:rFonts", '<w:rFonts w:%s="%s"' % (attr, font), 1)
        return tag

    out = []
    for info, data in items:
        if info.filename in ("word/styles.xml", "word/document.xml"):
            text = data.decode("utf-8")
            text = re.sub(r"<w:rFonts[^/]*/>", lambda m: fix_tag(m.group(0)), text)
            text = re.sub(r'(<w:lang[^/]*?w:eastAsia=")[^"]*"', r'\1zh-CN"', text)
            data = text.encode("utf-8")
        out.append((info, data))

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for info, data in out:
            dst.writestr(info, data)
    shutil.move(tmp, path)


def _docx_document(cfg=None):
    """新建 DOCX 并统一中文字体（默认微软雅黑），避免 Word 用西文字体渲染中文。"""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    name = str((cfg.get("export.docxFont") if cfg is not None else "") or "微软雅黑")

    def apply(style, size=None):
        try:
            style.font.name = name
            if size:
                style.font.size = Pt(size)
            rpr = style.element.get_or_add_rPr()
            rf = rpr.find(qn("w:rFonts"))
            if rf is None:
                rf = OxmlElement("w:rFonts")
                rpr.append(rf)
            for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
                rf.set(qn(attr), name)
            rpr.set(qn("w:lang"), "zh-CN")
        except Exception:  # noqa: BLE001
            pass

    for style_name, size in (("Normal", 10.5), ("Title", 20), ("Heading 1", 16),
                             ("Heading 2", 13), ("Heading 3", 12)):
        style = doc.styles[style_name] if style_name in [s.name for s in doc.styles] else None
        if style is not None:
            apply(style, size)

    # 关键：清掉模板自带的主题字体（asciiTheme/eastAsiaTheme…）与 en-US 的 eastAsia 语言。
    # 否则 Word 会用主题字体（等线/Calibri Light）渲染中文，并按日文字形选字 → 看着像异体字。
    def fix_rpr(rpr):
        if rpr is None:
            return
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.insert(0, rf)
        for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            if rf.get(qn(attr)) is not None:
                del rf.attrib[qn(attr)]
        for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            rf.set(qn(attr), name)
        lang = rpr.find(qn("w:lang"))
        if lang is None:
            lang = OxmlElement("w:lang")
            rpr.append(lang)
        lang.set(qn("w:val"), "en-US")
        lang.set(qn("w:eastAsia"), "zh-CN")
        lang.set(qn("w:bidi"), "ar-SA")

    try:
        defaults = doc.styles.element.find(qn("w:docDefaults"))
        if defaults is not None:
            rpr_default = defaults.find(qn("w:rPrDefault"))
            if rpr_default is not None:
                fix_rpr(rpr_default.find(qn("w:rPr")))
        for style in doc.styles:
            fix_rpr(style.element.find(qn("w:rPr")))
    except Exception:  # noqa: BLE001
        pass
    return doc

# --------------------------------------------------------------- 合并导出

MERGED_FORMATS = ("md", "txt", "html", "docx")


def _video_title(video: dict) -> str:
    desc = (video.get("desc") or "").strip().split("\n")[0].strip()
    return desc or str(video.get("aweme_id") or "未命名")


def _section_paragraphs(video: dict, transcript: dict, cfg) -> list[dict]:
    segs = fixed_segments(transcript, cfg)
    gap = float(cfg.get("export.paragraphGapSec", 2.0) or 2.0)
    paras = paragraphs(segs, gap=gap, max_chars=120,
                       replacements=cfg.get("asr.replacements") or [])
    return paras


def _total_seconds(items) -> float:
    total = 0.0
    for item in items:
        tr = item.get("transcript") or {}
        total += float(tr.get("audio_seconds") or 0) or (
            float((item.get("video") or {}).get("duration_ms") or 0) / 1000.0)
    return total


def _merged_head(items, title, cfg) -> str:
    from datetime import datetime, timezone, timedelta

    first = items[0].get("video") or {}
    first_tr = items[0].get("transcript") or {}
    seconds = _total_seconds(items)
    engines = []
    for item in items:
        tr = item.get("transcript") or {}
        name = str(tr.get("engine") or "")
        if name and name not in engines:
            engines.append(name)
    eng_str = " + ".join(engines) if engines else "未知"
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    lines = [
        "---",
        f"title: {_yaml_escape(title)}",
        f"author: {_yaml_escape(str(first.get('author_name') or ''))}",
        f"videos: {len(items)}",
        f"duration: {human_duration(seconds * 1000.0)}",
        f"engine: {_yaml_escape(eng_str)}",
        f"order: {_yaml_escape('发布时间倒序' if _merge_order(cfg) == 'desc' else '发布时间正序')}",
        f"exported: {now}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    return "\n".join(lines)


def merge_markdown(items, title, cfg) -> str:
    out = [_merged_head(items, title, cfg)]
    for i, item in enumerate(items, 1):
        v = item.get("video") or {}
        out.append(f"## {i}. {_video_title(v)}")
        out.append("")
        meta = []
        if v.get("aweme_id") and _with_link(cfg):
            meta.append(f"[原视频](https://www.douyin.com/video/{v['aweme_id']})")
        if _with_published(cfg) and published_on(v):
            meta.append(published_on(v))
        if v.get("duration_ms"):
            meta.append(human_duration(v["duration_ms"]))
        if _with_stats(cfg):
            line = _stats_line(v, cfg)
            if line:
                meta.append(line)
        if meta:
            out.append("> " + " · ".join(meta))
            out.append("")
        for para in _section_paragraphs(v, item.get("transcript") or {}, cfg):
            if _ts_on(cfg):
                out.append(f"[{fmt_ts(para['start'], 'clock')}] {para['text']}")
            else:
                out.append(para["text"])
            out.append("")
    return "\n".join(out)


def merge_text(items, title, cfg) -> str:
    out = [title, f"共 {len(items)} 条 · 总时长 {human_duration(_total_seconds(items) * 1000.0)}", ""]
    for i, item in enumerate(items, 1):
        v = item.get("video") or {}
        out.append(f"{'=' * 60}")
        out.append(f"{i}. {_video_title(v)}")
        out.append(f"{'=' * 60}")
        for para in _section_paragraphs(v, item.get("transcript") or {}, cfg):
            if _ts_on(cfg):
                out.append(f"[{fmt_ts(para['start'], 'clock')}] {para['text']}")
            else:
                out.append(para["text"])
        out.append("")
    return "\n".join(out)


def merge_html(items, title, cfg) -> str:
    from html import escape

    parts = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        f"<title>{escape(title)}</title>",
        "<style>body{max-width:820px;margin:40px auto;padding:0 20px;"
        "font:16px/1.85 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222}"
        "h1{font-size:26px}h2{font-size:19px;margin-top:2.2em;border-top:1px solid #eee;padding-top:1.4em}"
        ".meta{color:#888;font-size:13px;margin:.4em 0 1em}"
        ".ts{color:#999;font-variant-numeric:tabular-nums;margin-right:.6em}"
        "nav{font-size:14px;columns:2}nav a{display:block;color:#36c;text-decoration:none;margin:.2em 0}"
        "</style></head><body>",
        f"<h1>{escape(title)}</h1>",
    ]
    for i, item in enumerate(items, 1):
        v = item.get("video") or {}
        parts.append(f"<h2 id=\"v{i}\">{i}. {escape(_video_title(v))}</h2>")
        if v.get("aweme_id") and _with_link(cfg):
            parts.append(
                f"<p class=\"meta\"><a href=\"https://www.douyin.com/video/{v['aweme_id']}\">原视频</a></p>"
            )
        for para in _section_paragraphs(v, item.get("transcript") or {}, cfg):
            if _ts_on(cfg):
                parts.append(
                    f"<p><span class=\"ts\">[{fmt_ts(para['start'], 'clock')}]</span>{escape(para['text'])}</p>"
                )
            else:
                parts.append(f"<p>{escape(para['text'])}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def merge_docx(items, title, cfg, path: str) -> None:
    doc = _docx_document(cfg)
    doc.add_heading(title, level=0)
    for i, item in enumerate(items, 1):
        v = item.get("video") or {}
        doc.add_heading(f"{i}. {_video_title(v)}", level=2)
        if v.get("aweme_id") and _with_link(cfg):
            doc.add_paragraph(f"https://www.douyin.com/video/{v['aweme_id']}")
        for para in _section_paragraphs(v, item.get("transcript") or {}, cfg):
            doc.add_paragraph(
                f"[{fmt_ts(para['start'], 'clock')}] {para['text']}" if _ts_on(cfg)
                else para["text"])
    doc.save(path)
    _docx_rewrite_fonts(path, _docx_font_name(cfg))


def export_merged(cfg, items: list[dict], formats: list[str], out_dir: str | None,
                  title: str | None = None, fixes_by_sec: dict | None = None) -> list[dict]:
    """把多条转写合并成一个大文件。items = [{'video':…, 'transcript':…}, …]"""
    if fixes_by_sec:
        patched = []
        for item in items:
            fixes = (fixes_by_sec or {}).get((item.get("video") or {}).get("sec_uid")) or {}
            tr = dict(item.get("transcript") or {})
            if fixes:
                tr["segments"] = apply_fixes_to_segments(tr.get("segments"), fixes)
            patched.append({"video": item.get("video"), "transcript": tr})
        items = patched
    if not items:
        raise DyxError("没有可合并的转写结果", code="nothing_to_merge",
                       hint="先运行转写，或去掉合并选项。")
    items = _ordered_items(items, cfg)
    base = os.path.join(out_dir, "export", "合集") if out_dir else cfg.output_dir
    os.makedirs(base, exist_ok=True)
    first = items[0].get("video") or {}
    name = title or f"{first.get('author_name') or '抖音'}·转写合集"
    written = []
    for fmt in formats:
        fmt = str(fmt).lower().strip()
        if fmt not in MERGED_FORMATS:
            raise DyxError(f"合并导出不支持 {fmt}", code="bad_format",
                           hint=f"合并导出可选：{', '.join(MERGED_FORMATS)}")
        path = _free_path(base, slugify(name, 60), fmt)
        try:
            if fmt == "md":
                text = merge_markdown(items, name, cfg)
            elif fmt == "txt":
                text = merge_text(items, name, cfg)
            elif fmt == "html":
                text = merge_html(items, name, cfg)
            else:
                merge_docx(items, name, cfg, path)
                text = None
            if text is not None:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
        except PermissionError:
            # 上一次导出的文件被 Word/资源管理器预览占用：换个带时间戳的名字，别让整次导出失败
            path = _free_path(base, f"{slugify(name, 60)}-{time.strftime('%H%M%S')}", fmt)
            if fmt == "docx":
                merge_docx(items, name, cfg, path)
            else:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write({"md": merge_markdown, "txt": merge_text,
                             "html": merge_html}[fmt](items, name, cfg))
        written.append({"format": fmt, "path": path, "bytes": os.path.getsize(path)})
    return written


def _free_path(base: str, stem: str, fmt: str) -> str:
    """挑一个当前可写的文件名：默认名被占用（Word 打开/预览锁）时自动加序号。"""
    for i in range(0, 20):
        cand = os.path.join(base, f"{stem}{'' if i == 0 else f' ({i + 1})'}.{fmt}")
        if not os.path.exists(cand):
            return cand
        try:
            with open(cand, "ab"):
                pass
            return cand
        except PermissionError:
            continue
    return os.path.join(base, f"{stem}-{time.strftime('%H%M%S')}.{fmt}")
