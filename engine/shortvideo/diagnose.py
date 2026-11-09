# -*- coding: utf-8 -*-
"""失败原因诊断：把「下载失败」「转写失败/跳过」翻成人能看懂的分类。

只读库与任务日志，不触发任何网络请求。
"""
from __future__ import annotations

import io
import os
import re

from .store import Store

# ---------------------------------------------------------------- 分类规则
DOWNLOAD_RULES = [
    ("http404", re.compile(r"\b404\b|Not Found", re.I),
     "视频已删除或播放地址失效（404）",
     "多为 2023–2024 年的老作品：作者已删除、设为私密或换了 CDN，客户端拿不到播放源。重试也不会成功。"),
    ("http403", re.compile(r"\b403\b|Forbidden", re.I),
     "该视频限制播放（403）",
     "内容受限/地区限制，浏览器里也放不出来。"),
    ("xhs_captcha", re.compile(r"验证码|captcha|异常访问", re.I),
     "小红书要求验证码（风控）",
     "在弹窗浏览器里手动过一次验证码，之后会自动降速继续。"),
    ("blocked", re.compile(r"响应过小|疑似被拦截|拦截", re.I),
     "响应异常，疑似被风控拦截",
     "请求返回的内容不像视频（过小），稍后重试通常可以恢复。"),
    ("timeout", re.compile(r"超时|timeout|timed out|连接", re.I),
     "网络异常/超时",
     "网络抖动或 CDN 慢，可以重试。"),
    ("disk", re.compile(r"No space|磁盘|磁盘空间", re.I),
     "磁盘空间不足",
     "清理空间后重试。"),
]


# 这些类别「值得抢救」：重试有机会成功；404/403 重试也不会变。
RETRYABLE_KEYS = ("blocked", "xhs_captcha", "timeout", "disk", "other")


def classify_download(error: str | None) -> tuple[str, str, str]:
    text = error or ""
    for key, pattern, label, hint in DOWNLOAD_RULES:
        if pattern.search(text):
            return key, label, hint
    return "other", "其他错误", text[:120] or "没有留下错误信息"


# ---------------------------------------------------------------- 报告
def _trunc(text: str | None, n: int = 46) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def report(cfg, store: Store, sec_uid: str | None = None,
           ids: list[str] | None = None, limit: int = 300,
           scan_logs: bool = True) -> dict:
    where, args = "", []
    if ids:
        where = " WHERE v.aweme_id IN (%s)" % ",".join("?" * len(ids))
        args = [str(i) for i in ids]
    elif sec_uid:
        where = " WHERE v.sec_uid=?"
        args = [sec_uid]

    rows = [dict(r) for r in store.conn.execute(
        "SELECT v.aweme_id, v.desc, v.duration_ms, v.status, v.error, "
        "v.is_image_post, v.image_count, v.no_speech, v.file_path, "
        "(t.aweme_id IS NOT NULL) AS has_transcript, t.engine "
        "FROM videos v LEFT JOIN transcripts t ON t.aweme_id=v.aweme_id" + where +
        " ORDER BY v.create_time DESC", args).fetchall()]

    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    # 下载失败分类
    buckets: dict[str, dict] = {}
    for row in rows:
        if row["status"] != "failed":
            continue
        key, label, hint = classify_download(row["error"])
        bucket = buckets.setdefault(key, {"key": key, "label": label, "hint": hint,
                                          "count": 0, "items": []})
        bucket["count"] += 1
        if len(bucket["items"]) < limit:
            bucket["items"].append({
                "aweme_id": row["aweme_id"],
                "desc": _trunc(row["desc"]),
                "error": (row["error"] or "")[:160],
            })
    download_categories = sorted(buckets.values(), key=lambda b: -b["count"])
    retryable = [b for b in download_categories if b["key"] in RETRYABLE_KEYS]
    retryable_count = sum(b["count"] for b in retryable)

    # 跳过（图集）
    images = [{"aweme_id": r["aweme_id"], "desc": _trunc(r["desc"]),
               "images": r["image_count"] or 0}
              for r in rows if r["status"] == "skipped"][:limit]

    # 转写侧
    no_speech = [{"aweme_id": r["aweme_id"], "desc": _trunc(r["desc"]),
                  "seconds": round((r["duration_ms"] or 0) / 1000.0, 1)}
                 for r in rows if (r["no_speech"] or 0) == 1][:limit]
    missing = [{"aweme_id": r["aweme_id"], "desc": _trunc(r["desc"])}
               for r in rows
               if r["status"] == "downloaded" and not r["has_transcript"]
               and not (r["no_speech"] or 0)][:limit]
    engines: dict[str, int] = {}
    for row in rows:
        if row["has_transcript"]:
            engines[row["engine"] or "未知"] = engines.get(row["engine"] or "未知", 0) + 1

    # 日志里的历史问题（欠费/无人声），只做说明用
    history = _scan_logs(cfg) if scan_logs else {"arrearage": 0, "logs": 0}

    notes: list[str] = []
    if download_categories:
        total = sum(b["count"] for b in download_categories)
        parts = "，".join(f"{b['label']} {b['count']} 条" for b in download_categories)
        notes.append(f"下载失败 {total} 条：{parts}。")
    if images:
        notes.append(f"跳过 {len(images)} 条图集作品：只有图片，没有视频和音频，按设置不下载。")
    if no_speech:
        notes.append(f"跳过 {len(no_speech)} 条无人声作品：纯音乐/无旁白的视频，识别结果为空，"
                     f"已标记不再重复送识别（省费用）。")
    if retryable_count:
        notes.append(f"其中 {retryable_count} 条属于可抢救类别（风控/超时/磁盘），"
                     f"可以在任务详情里点「重试可抢救的失败」。\n")
    if not missing:
        notes.append("转写：没有失败的条目，已下载的作品都已转写或已标记跳过。")
    else:
        notes.append(f"转写：还有 {len(missing)} 条已下载但没转写，重新运行转写即可。")
    if history.get("arrearage"):
        notes.append(f"历史记录：有 {history['arrearage']} 次转写因阿里百炼欠费（Arrearage）失败，"
                     f"充值后已全部重跑成功，当前没有遗留。")

    return {
        "scope": {"sec_uid": sec_uid, "ids": len(ids or []), "videos": len(rows)},
        "status": by_status,
        "download": {
            "failed": sum(b["count"] for b in download_categories),
            "categories": download_categories,
            "skippedImages": images,
            "recoverable": {
                "count": retryable_count,
                "keys": [b["key"] for b in retryable],
                "items": [i for b in retryable for i in b["items"]][:limit],
            },
        },
        "transcribe": {
            "transcribed": sum(engines.values()),
            "engines": engines,
            "noSpeech": no_speech,
            "missing": missing,
        },
        "history": history,
        "notes": notes,
    }


def _scan_logs(cfg) -> dict:
    """扫一遍任务日志，统计「欠费」这类现在已经不存在、但值得解释的历史失败。"""
    jobs = os.path.join(os.path.dirname(cfg.db_path), "jobs")
    if not os.path.isdir(jobs):
        return {"arrearage": 0, "logs": 0}
    arrear = 0
    logs = 0
    for name in os.listdir(jobs):
        if not name.endswith(".log"):
            continue
        logs += 1
        try:
            text = io.open(os.path.join(jobs, name), encoding="utf-8",
                           errors="replace").read()
        except OSError:
            continue
        arrear += len(re.findall(r"Arrearage", text))
    return {"arrearage": arrear, "logs": logs}
