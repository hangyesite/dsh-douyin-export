"""High-level operations composed from browser, download, asr and export."""
from __future__ import annotations

import contextlib
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .asr import get_engine, is_fatal_asr_error
from .aweme import author_record, normalise
from .browser import BrowserSession, aweme_id_from, sec_uid_from


_XHS_ID_RE = re.compile(r"[0-9a-f]{24}")


def _author_key(author):
    """将用户给的作者标识规范化为库里的 sec_uid（抖音）或 24 位 id（小红书）。"""
    if not author:
        return None
    a = str(author).strip()
    s = sec_uid_from(a)
    if s:
        return s
    if _XHS_ID_RE.fullmatch(a):
        return a
    return None
from .download import download_video_xhs
from .fetch_xhs import (XhsSession, normalise_note, is_xhs_url, parse_xhs, note_url,
                        resolve_short, api_available)
from .diagnose import classify_download
from .download import download_images, download_video
from .export import export_one, export_merged
from .store import Store
from .progress import Progress
from .util import ShortVideoError, ffprobe_json, find_ffmpeg, log


def _progress(cb: Callable[[str], None] | None, msg: str) -> None:
    log(msg)
    if cb:
        cb(msg)


# --------------------------------------------------------------------- fetch
def fetch_one(cfg, store: Store, target: str, session: BrowserSession | None = None) -> dict:
    aid = aweme_id_from(target)
    if not aid:
        raise ShortVideoError(f"无法识别作品 ID: {target}", code="bad_target")
    own = session is None
    if own:
        session = BrowserSession(cfg).__enter__()
        session.warm()
    try:
        raw = session.aweme_detail(aid)
    finally:
        if own and session:
            session.__exit__()
    rec = normalise(raw)
    state = store.upsert_video(rec)
    return {"aweme_id": aid, "state": state, "desc": rec.get("desc")}


def sync_author(cfg, store: Store, target: str, limit: int | None = None,
                incremental: bool = True, stop_after_known: int = 25,
                download: bool = False, cb: Callable[[str], None] | None = None) -> dict:
    sec = sec_uid_from(target) or (target if target.startswith("MS4wLjABAAAA") else None)
    if not sec:
        raise ShortVideoError(f"无法识别作者主页: {target}", code="bad_target")

    existing_ids = {v["aweme_id"] for v in store.query_videos(sec_uid=sec, limit=100000)}
    started = time.time()
    new_items: list[dict] = []
    known_streak = 0
    stop_reason = "completed"
    author = None
    prog = Progress("sync", 0, "条", log_every=20)
    prog.set_phase("翻页中", "正在打开作者主页")

    with BrowserSession(cfg) as session:
        session.warm()
        user = session.user_profile(sec)
        if user:
            author = author_record(user)
            store.upsert_author(author)
            prog.total = int(author.get("aweme_count") or 0)
            prog.set_phase("翻页中", f"作者 {author['nickname']} 共 {author['aweme_count']} 个作品")
            _progress(cb, f"作者：{author['nickname']}（{author['unique_id']}）"
                          f" 作品 {author['aweme_count']} 个，粉丝 {author['follower_count']}")

        for aweme in session.iter_user_posts(sec):
            rec = normalise(aweme)
            aid = rec["aweme_id"]
            is_new = aid not in existing_ids
            store.upsert_video(rec)
            if is_new:
                existing_ids.add(aid)
                new_items.append(rec)
                known_streak = 0
            else:
                known_streak += 1
            prog.bump(ok=True, note=f"新增 {len(new_items)}",
                      log=f"新增 {len(new_items)}")

            if limit and len(new_items) >= limit:
                stop_reason = "limit"
                break
            if incremental and known_streak >= stop_after_known:
                stop_reason = "no-new-items"
                break

        if getattr(session, "stop_reason", "") == "login-required" and stop_reason == "completed":
            stop_reason = "login-required"
    prog.finish("同步完成" if stop_reason == "completed" else f"提前结束（{stop_reason}）")
    store.set_author_cursor(sec, None, complete=(stop_reason == "completed"))
    result = {
        "sec_uid": sec,
        "author": author,
        "warning": ("作品列表被登录墙截断，只能取到最新的一页（约 18 条）。"
                    "运行 sv_login 扫码登录后再同步即可拿到全量作品。")
        if stop_reason == "login-required" else None,
        "new": len(new_items),
        "stopReason": stop_reason,
        "elapsedSec": round(time.time() - started, 1),
        "items": [{"aweme_id": r["aweme_id"], "desc": (r.get("desc") or "")[:60],
                   "duration": r.get("duration_ms")} for r in new_items[:50]],
    }

    if download and new_items:
        dl = download_many(cfg, store, [r["aweme_id"] for r in new_items], cb=cb)
        result["downloaded"] = dl
    return result


# ------------------------------------------------------------------- xiaohongshu
def _xhs_author(a: dict, uid: str) -> dict:
    fans = a.get("fans") or ""
    return {
        "sec_uid": uid,
        "uid": uid,
        "nickname": a.get("nickname") or "",
        "unique_id": a.get("red_id") or "",
        "signature": a.get("desc") or "",
        "avatar": None,
        "aweme_count": 0,
        "follower_count": int("".join(ch for ch in str(fans) if ch.isdigit()) or 0)
                          if "\u4e07" not in str(fans) else int(float(str(fans).replace("\u4e07", "") or 0) * 10000),
        "total_favorited": 0,
        "synced_at": None,
    }


def sync_xhs(cfg, store: Store, target: str, limit: int | None = None,
             incremental: bool = True, stop_after_known: int = 25,
             download: bool = False, cb: Callable[[str], None] | None = None) -> dict:
    """小红书作者主页同步（需要登录态；游客只能看到 12 条且翻不动）。"""
    from .util import LoginRequired

    parsed = parse_xhs(target)
    if parsed.get("short") and not parsed.get("user_id"):
        real = resolve_short(parsed["short"])
        more = parse_xhs(real)
        parsed = {k: parsed.get(k) or more.get(k) for k in ("user_id", "note_id", "xsec_token", "short")}
    uid = parsed.get("user_id")
    if not uid:
        raise ShortVideoError(f"\u65e0\u6cd5\u8bc6\u522b\u5c0f\u7ea2\u4e66\u4f5c\u8005\uff1a{target}", code="bad_target")
    tok = parsed.get("xsec_token") or ""

    existing = {v["aweme_id"] for v in store.query_videos(sec_uid=uid, limit=100000)}
    started = time.time()
    new_items: list[dict] = []
    known_streak = 0
    stop_reason = "completed"
    author = None
    prog = Progress("sync", 0, "\u6761", log_every=20)
    prog.set_phase("\u7ffb\u9875\u4e2d", "\u6b63\u5728\u6253\u5f00\u4f5c\u8005\u4e3b\u9875")

    def _absorb(note) -> bool:
        """收一条笔记；返回 True 表示该停止翻页。"""
        nonlocal known_streak, stop_reason
        rec = normalise_note(note, uid, (author or {}).get("nickname") or "")
        aid = rec.get("aweme_id")
        if not aid:
            return False
        is_new = aid not in existing
        store.upsert_video(rec)
        if is_new:
            existing.add(aid)
            new_items.append(rec)
            known_streak = 0
        else:
            known_streak += 1
        prog.bump(ok=True, note=f"新增 {len(new_items)}", log=f"新增 {len(new_items)}")
        if limit and len(new_items) >= limit:
            stop_reason = "limit"
            return True
        if incremental and known_streak >= stop_after_known:
            stop_reason = "no-new-items"
            return True
        return False

    # 一、优先走算法签名接口：不开浏览器、不会错位、快一个量级
    used_api = False
    if api_available():
        from .fetch_xhs import XhsApi
        api = XhsApi(cfg)
        try:
            info = api.user_info(uid) or {}
            if info:
                author = _xhs_author(info, uid)
                store.upsert_author(author)
                _progress(cb, f"作者：{author['nickname']}（{author['unique_id']}）")
            prog.set_phase("翻页中", "接口模式")
            for note in api.user_posts(uid, tok, limit=limit):
                if _absorb(note):
                    break
            used_api = True
        except ShortVideoError as e:
            if str(getattr(e, "code", "") or "") in ("xhs_captcha", "xhs_blocked", "xhs_needs_token"):
                log(f"接口模式不通（{e}），改用浏览器翻页")
            else:
                raise

    # 二、接口不可用时回落浏览器
    if not used_api:
        with XhsSession(cfg) as session:
            # 不硬挡登录态：游客也能拿到 12 条，只有翻不动时才算需要登录
            logged = session.is_logged_in()
            if not logged:
                log("小红书未登录：本次可能只能取到最新一屏")
            info = session.author_info(uid, tok)
            if info:
                author = _xhs_author(info, uid)
                store.upsert_author(author)
                _progress(cb, f"作者：{author['nickname']}（{author['unique_id']}）")
            for note in session.user_notes(uid, tok, limit=limit):
                if _absorb(note):
                    break
            if getattr(session, "stop_reason", "") == "login-required" and stop_reason == "completed":
                stop_reason = "login-required"
    if author:
        author["aweme_count"] = len(existing)
        store.upsert_author(author)
    prog.finish("\u540c\u6b65\u5b8c\u6210" if stop_reason == "completed" else f"\u63d0\u524d\u7ed3\u675f\uff08{stop_reason}\uff09")
    store.set_author_cursor(uid, None, complete=(stop_reason == "completed"))
    result = {
        "platform": "xiaohongshu",
        "sec_uid": uid,
        "author": author,
        "warning": None,
        "new": len(new_items),
        "stopReason": stop_reason,
        "elapsedSec": round(time.time() - started, 1),
        "items": [{"aweme_id": r["aweme_id"], "desc": (r.get("desc") or "")[:60]} for r in new_items[:50]],
    }
    if download and new_items:
        result["downloaded"] = download_many(cfg, store, [r["aweme_id"] for r in new_items], cb=cb)
    return result


def fetch_note_xhs(cfg, store: Store, target: str, cb: Callable[[str], None] | None = None) -> dict:
    """单篇小红书笔记：打开页面拿元数据并入库（不下载）。"""
    parsed = parse_xhs(target)
    if parsed.get("short") and not parsed.get("note_id"):
        more = parse_xhs(resolve_short(parsed["short"]))
        parsed = {k: parsed.get(k) or more.get(k) for k in ("user_id", "note_id", "xsec_token", "short")}
    nid = parsed.get("note_id")
    if not nid:
        raise ShortVideoError(f"\u65e0\u6cd5\u8bc6\u522b\u5c0f\u7ea2\u4e66\u7b14\u8bb0\uff1a{target}", code="bad_target")
    tok = parsed.get("xsec_token") or ""
    with XhsSession(cfg) as session:
        info = session.note_media(nid, tok)
        if not info.get("mp4") and not info.get("imageList"):
            raise ShortVideoError("\u9875\u9762\u6ca1\u6709\u62ff\u5230\u89c6\u9891\u6216\u56fe\u7247\uff08\u53ef\u80fd\u9700\u8981\u767b\u5f55\uff09",
                                  code="no_media")
        uid = info.get("user_id") or parsed.get("user_id") or ""
        rec = normalise_note(info, uid, info.get("user") or "")
        rec["desc"] = rec.get("desc") or info.get("title") or ""
        # 笔记链接必须带 xsec_token，否则 PC 端打不开
        rec["share_url"] = note_url(nid, tok)
        store.upsert_video(rec)
        if uid and info.get("user"):
            store.upsert_author(_xhs_author({"nickname": info.get("user"), "fans": ""}, uid))
    _progress(cb, f"\u5df2\u5165\u5e93 {nid}")
    return {"platform": "xiaohongshu", "aweme_id": nid, "state": "new",
            "desc": (rec.get("desc") or "")[:80]}


# ------------------------------------------------------------------- targets
def add_targets(cfg, store: Store, text: str, download: bool = False,
                full: bool = False, limit_per_author: int | None = None,
                cb: Callable[[str], None] | None = None,
                out_dir: str | None = None) -> dict:
    """Classify pasted text (author links, video links, share text) and ingest it.

    Authors go through the paged post list (one browser pass, ~18 items/page).
    Bare video ids each need one detail request, so they are batched into a
    single browser session. Nothing here downloads unless download=True.
    """
    from .inputs import parse_targets

    result_extra = {}
    lines = [ln for ln in (text or "").splitlines()]
    xhs_lines = [ln for ln in lines if is_xhs_url(ln)]
    if xhs_lines:
        text = "\n".join(ln for ln in lines if not is_xhs_url(ln))
        got = []
        for ln in xhs_lines:
            try:
                got.append(sync_xhs(cfg, store, ln, limit=limit_per_author,
                                    incremental=not full, download=False, cb=cb))
            except Exception as e:  # noqa: BLE001
                got.append({"platform": "xiaohongshu", "target": ln, "error": str(e)})
        result_extra["xhsAuthors"] = got

    parsed = parse_targets(text)
    _progress(cb, f"识别到 作者 {len(parsed['authors'])} 个 / 作品 {len(parsed['videos'])} 个"
                  f" / 无法识别 {len(parsed['unknown'])} 行")
    result = {
        "input": {"lines": parsed["lines"], "unknown": parsed["unknown"]},
        "authorsSec": parsed["authors"],
        "videoIds": parsed["videos"],
        "authors": [],
        "videos": {"requested": len(parsed["videos"]), "fetched": 0,
                    "known": 0, "failed": []},
        "download": None,
    }
    result.update(result_extra)

    for sec in parsed["authors"]:
        try:
            r = sync_author(cfg, store, sec, limit=limit_per_author,
                            incremental=not full, download=download, cb=cb)
            result["authors"].append(r)
        except Exception as e:  # noqa: BLE001
            result["authors"].append({"sec_uid": sec, "error": str(e)})

    ids = parsed["videos"]
    if ids:
        todo = []
        for aid in ids:
            row = store.get_video(aid)
            if row and row.get("play_uri"):
                result["videos"]["known"] += 1
            else:
                todo.append(aid)
        if todo:
            with BrowserSession(cfg) as session:
                session.warm()
                for i, aid in enumerate(todo, 1):
                    try:
                        _progress(cb, f"[{i}/{len(todo)}] 取元数据 {aid}")
                        raw = session.aweme_detail(aid)
                        store.upsert_video(normalise(raw))
                        result["videos"]["fetched"] += 1
                    except Exception as e:  # noqa: BLE001
                        result["videos"]["failed"].append(
                            {"aweme_id": aid, "error": str(e)})
        if download:
            result["download"] = download_many(cfg, store, ids=ids, cb=cb)
    return result


def pipeline_text(cfg, store: Store, text: str, formats: list[str] | None = None,
                  engine: str | None = None, full: bool = False,
                  limit_per_author: int | None = None,
                  cb: Callable[[str], None] | None = None,
                  out_dir: str | None = None,
                  merge: bool = False,
                  merge_formats: list[str] | None = None,
                  merge_title: str | None = None,
                  options: dict | None = None) -> dict:
    """One-stop for pasted input: ingest -> download -> transcribe -> export."""
    res = add_targets(cfg, store, text, download=True, full=full,
                      limit_per_author=limit_per_author, cb=cb)

    ids: list[str] = list(res.get("videoIds") or [])
    for entry in res.get("authors") or []:
        sec = entry.get("sec_uid")
        if not sec:
            continue
        ids += [v["aweme_id"] for v in store.query_videos(sec_uid=sec, limit=100000)]

    seen = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]
    if ordered:
        res["transcribe"] = transcribe_many(cfg, store, ids=ordered,
                                            engine_name=engine, cb=cb, out_dir=out_dir)
        res["export"] = export_many(cfg, store, ids=ordered, formats=formats, cb=cb,
                                    out_dir=out_dir, merge=merge,
                                    merge_formats=merge_formats,
                                    merge_title=merge_title, options=options)
    return res


# ------------------------------------------------------------------ download

def _image_urls(row: dict) -> list[str]:
    """图集作品的图片地址（存在 raw_json 的 _images 里，也兼容已扁平化的字段）。"""
    urls = row.get("_images")
    if isinstance(urls, list) and urls:
        return [u for u in urls if isinstance(u, str) and u.startswith("http")]
    raw = row.get("raw_json") or ""
    if raw:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            urls = data.get("_images") or []
            if isinstance(urls, list):
                return [u for u in urls if isinstance(u, str) and u.startswith("http")]
        except Exception:  # noqa: BLE001
            return []
    return []

def _clean_error(err: Exception | str, limit: int = 200) -> str:
    """错误信息去掉重复前缀（"下载失败: 下载失败: xxx" → "下载失败: xxx"）。"""
    msg = str(err).strip()
    while re.match(r"^下载失败[:：]\s*下载失败[:：]", msg):
        msg = re.sub(r"^下载失败[:：]\s*", "", msg, count=1)
    return msg[:limit]


def download_many(cfg, store: Store, ids: list[str] | None = None,
                  author: str | None = None, limit: int | None = None,
                  cb: Callable[[str], None] | None = None,
                  out_dir: str | None = None) -> dict:
    base = out_dir or None
    if ids:
        rows = [store.get_video(i) for i in ids]
        rows = [r for r in rows if r]
    else:
        sec = _author_key(author)
        rows = store.pending("download", sec_uid=sec, limit=limit or 200)
    ok, failed, skipped, images_skipped = [], [], [], []
    breaker = int(cfg.get("asr.breakerConsecutive") or 0)
    blocked_streak = 0
    cooldowns = 0
    pause: dict = {"reason": None, "remaining": 0}
    prog = Progress("download", len(rows), "个")
    prog.set_phase("下载中", f"共 {len(rows)} 个待处理")
    xhs_session = None
    xhs_count = 0
    _xhs_delay = float(cfg.get("fetch.xhsDelaySec") or 9)
    _raw_burst = cfg.get("fetch.xhsBurst")
    _xhs_burst = 20 if _raw_burst is None else int(_raw_burst)
    _xhs_pause = float(cfg.get("fetch.xhsBurstPauseSec") or 22)
    _xhs_jitter = float(cfg.get("fetch.xhsJitterSec") or 0)
    for row in rows:
        if row.get("status") == "downloaded" and row.get("file_path") and \
                os.path.exists(row["file_path"]):
            skipped.append(row["aweme_id"])
            prog.bump(skipped=True)
            continue
        try:
            _progress(cb, f"下载 {row['aweme_id']} …")
            if row.get("is_image_post"):
                reason = "图集作品：只有图片，没有视频和音频，按设置跳过"
                store.mark_skipped(row["aweme_id"], reason)
                skipped.append(row["aweme_id"])
                images_skipped.append({"aweme_id": row["aweme_id"],
                                       "images": row.get("image_count") or 0,
                                       "reason": reason})
                prog.bump(skipped=True, note="图集", log="图集跳过")
                continue
            if (row.get("platform") or "douyin") == "xiaohongshu":
                if xhs_session is None:
                    xhs_session = XhsSession(cfg).__enter__()
                xhs_count += 1
                if xhs_count > 1:
                    time.sleep(_xhs_delay + (random.uniform(0, _xhs_jitter) if _xhs_jitter else 0))
                if _xhs_burst and xhs_count % _xhs_burst == 0:
                    _progress(cb, f"  已下 {xhs_count} 条，歇 {int(_xhs_pause)} 秒避风控")
                    time.sleep(_xhs_pause)
                try:
                    info = download_video_xhs(cfg, row, xhs_session, base_dir=base)
                except Exception as _e:  # noqa: BLE001
                    _m = str(_e)
                    if ("has been closed" in _m or "Target page" in _m
                            or "browser" in _m.lower()):
                        log("  浏览器会话已失效，重开一个继续")
                        try:
                            xhs_session.__exit__()
                        except Exception:
                            pass
                        xhs_session = XhsSession(cfg).__enter__()
                        info = download_video_xhs(cfg, row, xhs_session, base_dir=base)
                    else:
                        raise
                if info.get("title") and not (row.get("desc") or "").strip():
                    row["desc"] = info["title"]
            else:
                info = download_video(cfg, row, base_dir=base)
            store.mark_downloaded(row["aweme_id"], info["file"], info["size"],
                                  info["audio"], row.get("ratio"),
                                  info["width"], info["height"],
                                  info.get("subtitle"))
            ok.append({"aweme_id": row["aweme_id"], "size": info["size"],
                       "resolution": f"{info['width']}x{info['height']}",
                       "seconds": round(info["duration"], 1)})
            prog.bump(ok=True, note=f"{info['size']/1048576:.1f} MB",
                      log=f"{info['width']}x{info['height']}")
            _progress(cb, f"  ✓ {info['size']/1048576:.1f} MB "
                          f"{info['width']}x{info['height']}")
        except Exception as e:  # noqa: BLE001
            msg = _clean_error(e)
            store.mark_failed(row["aweme_id"], msg)
            failed.append({"aweme_id": row["aweme_id"], "error": msg})
            prog.bump(failed=True, note=msg[:80], log="失败")
            _progress(cb, f"  ✗ {msg[:160]}")
            blocked_streak = (blocked_streak + 1
                              if classify_download(msg)[0] in ("blocked", "xhs_captcha")
                              else 0)
            if breaker and blocked_streak >= breaker:
                if (row.get("platform") or "douyin") == "xiaohongshu" and cooldowns < 5:
                    cooldowns += 1
                    _cool = float(cfg.get("fetch.xhsCooldownSec") or 600)
                    _progress(cb, f"  风控触发，冷却 {int(_cool/60)} 分钟后自动续跑（第 {cooldowns} 次）")
                    prog.set_phase("冷却中", f"风控冷却 {int(_cool/60)} 分钟")
                    time.sleep(_cool)
                    blocked_streak = 0
                    try:
                        store.mark_listed(row["aweme_id"])
                    except Exception:
                        pass
                    failed = [f for f in failed if f["aweme_id"] != row["aweme_id"]]
                    prog.set_phase("下载中", "冷却结束，继续")
                    continue
                pause["reason"] = (f"连续 {blocked_streak} 条被风控拦截，已暂停整轮"
                                   f"（过一会儿或换网络再点重试，剩下的会接着下载）")
                pause["remaining"] = len(rows) - len(ok) - len(failed) - len(skipped)
                break
    if pause["reason"]:
        prog.set_phase("已暂停", pause["reason"])
        _progress(cb, "⏸ " + pause["reason"])
    if xhs_session is not None:
        try:
            xhs_session.__exit__(None, None, None)
        except Exception:
            pass
    prog.finish()
    out = {"ok": ok, "failed": failed, "skipped": skipped,
           "imagesSkipped": images_skipped,
           "count": len(ok), "failedCount": len(failed),
           "skippedCount": len(images_skipped)}
    if pause["reason"]:
        out["paused"] = True
        out["pauseReason"] = pause["reason"]
        out["remaining"] = pause["remaining"]
    return out


# ----------------------------------------------------------------- transcribe
def _asr_workers(cfg, engine, pending_count: int) -> int:
    """本地模型是 CPU 密集型，串行反而更快；云端引擎按 asr.concurrency 并发。"""
    if not getattr(engine, "remote", False):
        return 1
    try:
        n = int(cfg.get("asr.concurrency") or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, 16, max(1, pending_count)))


def transcribe_many(cfg, store: Store, ids: list[str] | None = None,
                    author: str | None = None, limit: int | None = None,
                    engine_name: str | None = None, force: bool = False,
                    cb: Callable[[str], None] | None = None,
                    out_dir: str | None = None) -> dict:
    if engine_name:
        cfg.data["asr"]["engine"] = engine_name
    if ids:
        rows = [store.get_video(i) for i in ids]
        rows = [r for r in rows if r]
    else:
        sec = _author_key(author)
        rows = store.pending("transcribe", sec_uid=sec, limit=limit or 2000)

    # 「平台官方字幕」只对小红书有效：平台不匹配就直接拒绝，不要拿抖音视频去试
    eng_name = (cfg.get("asr.engine") or "sensevoice")
    if str(eng_name).lower() in ("official", "subtitle", "platform-subtitle"):
        wrong = [r for r in rows if (r.get("platform") or "douyin") != "xiaohongshu"]
        if wrong:
            raise ShortVideoError(
                f"「平台官方字幕」只能用于小红书作品；"
                f"这批 {len(rows)} 条里有 {len(wrong)} 条不是小红书的。"
                f"请改用其他引擎（如 dashscope / sensevoice），"
                f"或只选小红书作品。",
                code="engine_platform_mismatch",
            )

    done, failed, skipped, no_speech = [], [], [], []
    prog = Progress("transcribe", len(rows), "条")
    if not rows:
        prog.finish()
        return {"ok": [], "failed": [], "skipped": [], "noSpeech": [],
                "count": 0, "concurrency": 0}

    engine = get_engine(cfg)
    _progress(cb, f"ASR 引擎：{engine.name} / {engine.model_name}")

    # 主线程先把「跳过已有 / 缺音轨补下载」处理完（sqlite 连接不能跨线程），
    # 攒出真正要送给 ASR 的清单。
    todo: list[tuple[dict, str]] = []
    for row in rows:
        aid = row["aweme_id"]
        if not force and store.get_transcript(aid):
            skipped.append(aid)
            prog.bump(skipped=True)
            continue
        if force:
            store.mark_no_speech(aid, False)
        wav = row.get("audio_path")
        if getattr(engine, "needs_audio", True) is False:
            todo.append((row, wav or ""))
            continue
        if not wav or not os.path.exists(wav):
            try:
                _progress(cb, f"{aid}: 需要先下载音轨")
                info = download_video(cfg, row, base_dir=(out_dir or None))
                store.mark_downloaded(aid, info["file"], info["size"],
                                      info["audio"], row.get("ratio"),
                                      info["width"], info["height"])
                wav = info["audio"]
                row = store.get_video(aid) or row
            except Exception as e:  # noqa: BLE001
                store.mark_failed(aid, f"下载失败: {e}")
                failed.append({"aweme_id": aid, "error": str(e)[:200]})
                prog.bump(failed=True, note="下载失败", log="失败")
                continue
        if not wav:
            failed.append({"aweme_id": aid, "error": "无音轨"})
            prog.bump(failed=True, note="无音轨")
            continue
        todo.append((row, wav))

    workers = _asr_workers(cfg, engine, len(todo))
    prog.set_phase("转写中",
                   f"共 {len(rows)} 条，送 ASR {len(todo)} 条，并行 {workers} 路")

    fallback_engine = _fallback_engine(cfg, engine)

    def _run(item):
        row, wav = item
        lang = cfg.get("asr.language") or "zh"
        t0 = time.time()
        try:
            if getattr(engine, "needs_row", False):
                res = engine.transcribe(wav, lang, row=row)
            else:
                res = engine.transcribe(wav, lang)
            return row, res, time.time() - t0, None
        except Exception as e:  # noqa: BLE001
            if str(getattr(e, "code", "") or "") == "no_official_subtitle" and fallback_engine is not None:
                try:
                    _progress(cb, f"  · {row['aweme_id']} 没有官方字幕，改用 {fallback_engine.name}")
                    if getattr(fallback_engine, "needs_row", False):
                        res = fallback_engine.transcribe(wav, lang, row=row)
                    else:
                        res = fallback_engine.transcribe(wav, lang)
                    return row, res, time.time() - t0, None
                except Exception as e2:  # noqa: BLE001
                    return row, None, time.time() - t0, e2
            return row, None, time.time() - t0, e

    def _finish_one(row, res, seconds, error):
        aid = row["aweme_id"]
        if error is not None:
            failed.append({"aweme_id": aid, "error": str(error)[:300]})
            prog.bump(failed=True, note=str(error)[:80], log="失败")
            _progress(cb, f"  ✗ {aid} {str(error)[:160]}")
            return
        if not res.get("segments"):
            # 纯音乐 / 无人声：不入库空转写，打标记后不再重复送 ASR
            store.mark_no_speech(aid, True)
            no_speech.append({"aweme_id": aid, "reason": "no-speech"})
            prog.bump(ok=True, note="未检测到人声", log="无人声")
            _progress(cb, f"  · {aid} 未检测到人声，跳过")
            return
        store.save_transcript(aid, res["engine"], res["model"], res["language"],
                              res["duration"], res["segments"], res["text"])
        if out_dir:
            _dump_transcript(out_dir, row, res)
        done.append({"aweme_id": aid, "segments": len(res["segments"]),
                     "seconds": round(seconds, 1), "preview": res["text"][:60]})
        prog.bump(ok=True, note=f"{len(res['segments'])} 段", log=f"{seconds:.1f}s")
        _progress(cb, f"  ✓ {aid} {len(res['segments'])} 段 / {seconds:.1f}s")

    breaker = int(cfg.get("asr.breakerConsecutive") or 0)
    pause: dict = {"reason": None, "remaining": 0}
    streak = {"n": 0}

    def _handle(result: tuple) -> bool:
        """处理一条结果；返回 True 表示该整轮暂停。"""
        row, res, seconds, error = result
        aid = row["aweme_id"]
        if error is None:
            streak["n"] = 0
            _finish_one(row, res, seconds, None)
            return False
        if str(getattr(error, "code", "") or "") == "no_official_subtitle":
            store.mark_skipped(aid, "没有官方字幕")
            skipped.append({"aweme_id": aid, "reason": "no-subtitle"})
            prog.bump(skipped=True, note="无官方字幕", log="跳过")
            _progress(cb, f"  · {aid} 没有官方字幕，跳过")
            return False
        # 先如实记下这一条
        failed.append({"aweme_id": aid, "error": str(error)[:300]})
        prog.bump(failed=True, note=str(error)[:80], log="失败")
        _progress(cb, f"  ✗ {aid} {str(error)[:160]}")
        if is_fatal_asr_error(error):
            pause["reason"] = (f"账号或余额异常（{str(error)[:120]}），已暂停整轮；"
                               f"处理好之后重新发起转写，剩下的会接着做")
            return True
        streak["n"] += 1
        if breaker and streak["n"] >= breaker:
            pause["reason"] = (f"连续 {streak['n']} 条转写失败，已暂停整轮"
                               f"（最后一条：{str(error)[:120]}）")
            return True
        return False

    if workers <= 1 or len(todo) <= 1:
        for idx, item in enumerate(todo):
            if _handle(_run(item)):
                pause["remaining"] = len(todo) - idx - 1
                break
    else:
        _progress(cb, f"并发 {workers} 路送 ASR")
        idx = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while idx < len(todo):
                batch = todo[idx:idx + workers]
                stop = False
                for fut in as_completed([pool.submit(_run, item) for item in batch]):
                    if _handle(fut.result()):
                        stop = True
                idx += len(batch)
                if stop:
                    pause["remaining"] = len(todo) - idx
                    break

    if pause["reason"]:
        _progress(cb, "⏸ " + pause["reason"])
        prog.set_phase("已暂停", pause["reason"])
    prog.finish()
    out = {"ok": done, "failed": failed, "skipped": skipped,
           "noSpeech": no_speech, "count": len(done),
           "concurrency": workers if todo else 0}
    if pause["reason"]:
        out["paused"] = True
        out["pauseReason"] = pause["reason"]
        out["remaining"] = pause["remaining"]
    return out


def _fallback_engine(cfg, engine):
    """官方字幕缺失时的回落引擎（asr.officialFallback）。"""
    from .asr import get_engine as _get_engine

    if getattr(engine, "name", "") != "official":
        return None
    want = (cfg.get("asr.officialFallback") or "").strip()
    if not want:
        return None
    prev = cfg.get("asr.engine")
    try:
        cfg.set("asr.engine", want)
        fb = _get_engine(cfg)
    except Exception as e:  # noqa: BLE001
        log(f"回落引擎 {want} 不可用：{e}")
        fb = None
    finally:
        cfg.set("asr.engine", prev)
    return fb


def _dump_transcript(out_dir: str, row: dict, res: dict) -> None:
    """把这一条的转写结果另存一份到任务目录（任务详情里能直接看）。"""
    try:
        folder = os.path.join(out_dir, "transcript")
        os.makedirs(folder, exist_ok=True)
        aid = row.get("aweme_id") or "unknown"
        payload = {
            "aweme_id": aid,
            "desc": row.get("desc"),
            "url": row.get("url"),
            "author": row.get("author_name"),
            "duration_ms": row.get("duration_ms"),
            "engine": res.get("engine"),
            "model": res.get("model"),
            "language": res.get("language"),
            "audio_seconds": res.get("duration"),
            "segments": res.get("segments") or [],
            "text": res.get("text") or "",
        }
        tmp = os.path.join(folder, aid + ".json.part")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(folder, aid + ".json"))
    except OSError:
        pass


def _ensure_media_size(cfg, store: Store, row: dict) -> dict:
    """Backfill the real pixel size of a downloaded file (aweme metadata can lie)."""
    if row.get("media_width") and row.get("media_height"):
        return row
    path = row.get("file_path")
    if not path or not os.path.exists(path):
        return row
    try:
        _, ffprobe = find_ffmpeg(cfg.get("media.ffmpegPath"))
        info = ffprobe_json(ffprobe, path)
        vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        if vs.get("width") and vs.get("height"):
            store.mark_downloaded(row["aweme_id"], path, os.path.getsize(path),
                                  row.get("audio_path"), row.get("ratio"),
                                  vs["width"], vs["height"])
            return store.get_video(row["aweme_id"]) or row
    except Exception as e:  # noqa: BLE001
        log(f"  探测分辨率失败: {str(e)[:100]}")
    return row


def _norm_formats(formats):
    """把 "md,srt" / ["md", "srt"] / ["md,srt"] 归一成 ["md", "srt"]。"""
    if not formats:
        return None
    out = []
    for item in formats:
        for part in str(item).replace(";", ",").split(","):
            part = part.strip().lower().lstrip(".")
            if part and part not in out:
                out.append(part)
    return out or None


def _export_many_impl(cfg, store: Store, ids: list[str] | None = None,
                author: str | None = None, limit: int | None = None,
                formats: list[str] | None = None,
                cb: Callable[[str], None] | None = None,
                out_dir: str | None = None,
                merge: bool = False,
                merge_formats: list[str] | None = None,
                merge_title: str | None = None) -> dict:
    fmts = _norm_formats(formats) or _norm_formats(cfg.get("export.formats")) or ["md"]
    if ids:
        rows = [store.get_video(i) for i in ids]
        rows = [r for r in rows if r]
    else:
        sec = _author_key(author)
        rows = store.query_videos(sec_uid=sec, limit=limit or 100000)
    written, missing = [], []
    _fixes_cache: dict = {}
    def _fixes_by_sec():
        for _sec in {(r or {}).get("sec_uid") for r in rows}:
            _fixes_for(_sec)
        return _fixes_cache
    def _fixes_for(sec):
        if sec not in _fixes_cache:
            _fixes_cache[sec] = store.manual_fixes(sec)
        return _fixes_cache[sec]
    prog = Progress("export", len(rows), "条")
    prog.set_phase("排版中", f"共 {len(rows)} 条")
    for row in rows:
        aid = row["aweme_id"]
        tr = store.get_transcript(aid)
        if not tr or not tr.get("segments"):
            missing.append(aid)
            prog.bump(skipped=True)
            continue
        row = _ensure_media_size(cfg, store, row)
        files = export_one(cfg, row, tr, fmts, out_dir=out_dir,
                           fixes=_fixes_for(row.get("sec_uid")))
        for f in files:
            store.add_export(aid, f["format"], f["path"])
        written.append({"aweme_id": aid, "formats": [f["format"] for f in files]})
        prog.bump(ok=True, note=", ".join(f["format"] for f in files), log=aid)
        _progress(cb, f"  ✓ {aid} → {', '.join(f['format'] for f in files)}")
    merged = []
    if merge:
        items = []
        for row in rows:
            tr = store.get_transcript(row["aweme_id"])
            if not tr or not tr.get("segments"):
                continue
            items.append({"video": _ensure_media_size(cfg, store, row), "transcript": tr})
        items.sort(key=lambda item: (item["video"].get("create_time") or 0))
        if items:
            prog.set_phase("合并中", f"共 {len(items)} 条")
            mf = _norm_formats(merge_formats) or ["md"]
            mf = [f for f in mf if f in ("md", "txt", "html", "docx")] or ["md"]
            merged = export_merged(cfg, items, mf, out_dir, merge_title,
                                   fixes_by_sec=_fixes_by_sec())
            for f in merged:
                store.add_export(items[0]["video"]["aweme_id"], f"merged-{f['format']}", f["path"])
            _progress(cb, f"  合集 → {merged[0]['path']}")
    prog.finish()
    return {"written": written, "missingTranscript": missing, "count": len(written),
            "merged": merged, "mergedCount": len(merged)}


# ------------------------------------------------------------------- pipeline
def pipeline(cfg, store: Store, target: str, formats: list[str] | None = None,
             limit: int | None = None, engine_name: str | None = None,
             cb: Callable[[str], None] | None = None) -> dict:
    """target may be a video id/URL or an author profile URL."""
    out: dict = {}
    if sec_uid_from(target):
        out["sync"] = sync_author(cfg, store, target, limit=limit, download=False, cb=cb)
        ids = [i["aweme_id"] for i in out["sync"]["items"]]
    else:
        out["sync"] = fetch_one(cfg, store, target)
        ids = [out["sync"]["aweme_id"]]
    out["download"] = download_many(cfg, store, ids, cb=cb)
    out["transcribe"] = transcribe_many(cfg, store, ids, engine_name=engine_name, cb=cb)
    if out["transcribe"].get("paused"):
        # 熔断后不导出半成品，等处理好账号/网络再接着跑
        out["export"] = {"paused": True, "items": []}
        return out
    out["export"] = export_many(cfg, store, ids, formats=formats, cb=cb)
    return out


# ------------------------------------------------------------- retry failed
def retry_recoverable(cfg, store: Store, ids: list[str] | None = None,
                      author: str | None = None, limit: int | None = None,
                      cb: Callable[[str], None] | None = None,
                      out_dir: str | None = None) -> dict:
    """只重试值得抢救的下载失败：风控/超时/磁盘/未知。

    404（视频已删）、403（内容受限）、图集一律不动——重试也不会成功。
    """
    sec = _author_key(author)
    rows = store.query_videos(sec_uid=sec, status="failed", limit=limit or 500)
    wanted: list[str] = []
    kept: list[dict] = []
    for row in rows:
        aid = row["aweme_id"]
        if ids and aid not in ids:
            continue
        key, label, _hint = classify_download(row.get("error"))
        if key in ("blocked", "timeout", "disk", "other"):
            wanted.append(aid)
        else:
            kept.append({"aweme_id": aid, "reason": label})
    for aid in wanted:
        store.mark_listed(aid)
    _progress(cb, f"可抢救 {len(wanted)} 条；{len(kept)} 条属于永久失败，不重试")
    if not wanted:
        return {"candidates": 0, "permanent": kept,
                "download": {"ok": [], "failed": []}}
    result = download_many(cfg, store, ids=wanted, cb=cb, out_dir=out_dir)
    return {"candidates": len(wanted), "permanent": kept, "download": result}


# ------------------------------------------------------- 导出高级设置开关
@contextlib.contextmanager
def _export_options(cfg, options: dict | None):
    """临时套用导出选项（stats / timestamps），退出时还原。

    options: {"stats": bool, "statsFields": [...], "timestamps": bool,
              "published": bool, "order": "asc"|"desc"}，None 表示不动。
    """
    if not options:
        yield
        return
    prev_stats = cfg.get("export.includeStats")
    prev_fields = cfg.get("export.statsFields")
    prev_style = cfg.get("export.timestampStyle")
    prev_link = cfg.get("export.includeLink")
    prev_pub = cfg.get("export.includePublished")
    if options.get("link") is not None:
        cfg.set("export.includeLink", bool(options["link"]))
    prev_order = cfg.get("export.mergeOrder")
    if options.get("published") is not None:
        cfg.set("export.includePublished", bool(options["published"]))
    if options.get("order"):
        cfg.set("export.mergeOrder", str(options["order"]))
    if options.get("statsFields") is not None:
        cfg.set("export.statsFields", list(options["statsFields"]))
    elif options.get("stats") is not None:
        cfg.set("export.includeStats", bool(options["stats"]))
    if options.get("timestamps") is not None:
        cfg.set("export.timestampStyle", "clock" if options["timestamps"] else "none")
    try:
        yield
    finally:
        cfg.set("export.includeStats", prev_stats)
        cfg.set("export.statsFields", prev_fields)
        cfg.set("export.timestampStyle", prev_style)
        cfg.set("export.includeLink", prev_link)
        cfg.set("export.includePublished", prev_pub)
        cfg.set("export.mergeOrder", prev_order)


def export_many(cfg, store: Store, ids: list[str] | None = None,
                author: str | None = None, limit: int | None = None,
                formats: list[str] | None = None,
                cb: Callable[[str], None] | None = None,
                out_dir: str | None = None,
                merge: bool = False,
                merge_formats: list[str] | None = None,
                merge_title: str | None = None,
                options: dict | None = None) -> dict:
    with _export_options(cfg, options):
        return _export_many_impl(cfg, store, ids=ids, author=author, limit=limit,
                                 formats=formats, cb=cb, out_dir=out_dir,
                                 merge=merge, merge_formats=merge_formats,
                                 merge_title=merge_title)
