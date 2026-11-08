"""High-level operations composed from browser, download, asr and export."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .asr import get_engine, is_fatal_asr_error
from .aweme import author_record, normalise
from .browser import BrowserSession, aweme_id_from, sec_uid_from
from .diagnose import classify_download
from .download import download_images, download_video
from .export import export_one, export_merged
from .store import Store
from .progress import Progress
from .util import DyxError, ffprobe_json, find_ffmpeg, log


def _progress(cb: Callable[[str], None] | None, msg: str) -> None:
    log(msg)
    if cb:
        cb(msg)


# --------------------------------------------------------------------- fetch
def fetch_one(cfg, store: Store, target: str, session: BrowserSession | None = None) -> dict:
    aid = aweme_id_from(target)
    if not aid:
        raise DyxError(f"无法识别作品 ID: {target}", code="bad_target")
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
        raise DyxError(f"无法识别作者主页: {target}", code="bad_target")

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
                    "运行 dyx_login 扫码登录后再同步即可拿到全量作品。")
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
        sec = sec_uid_from(author) if author else None
        rows = store.pending("download", sec_uid=sec, limit=limit or 200)
    ok, failed, skipped, images_skipped = [], [], [], []
    breaker = int(cfg.get("asr.breakerConsecutive") or 0)
    blocked_streak = 0
    pause: dict = {"reason": None, "remaining": 0}
    prog = Progress("download", len(rows), "个")
    prog.set_phase("下载中", f"共 {len(rows)} 个待处理")
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
            info = download_video(cfg, row, base_dir=base)
            store.mark_downloaded(row["aweme_id"], info["file"], info["size"],
                                  info["audio"], row.get("ratio"),
                                  info["width"], info["height"])
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
            blocked_streak = blocked_streak + 1 if classify_download(msg)[0] == "blocked" else 0
            if breaker and blocked_streak >= breaker:
                pause["reason"] = (f"连续 {blocked_streak} 条被风控拦截，已暂停整轮"
                                   f"（过一会儿或换网络再点重试，剩下的会接着下载）")
                pause["remaining"] = len(rows) - len(ok) - len(failed) - len(skipped)
                break
    if pause["reason"]:
        prog.set_phase("已暂停", pause["reason"])
        _progress(cb, "⏸ " + pause["reason"])
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
        sec = sec_uid_from(author) if author else None
        rows = store.pending("transcribe", sec_uid=sec, limit=limit or 2000)

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

    def _run(item):
        row, wav = item
        t0 = time.time()
        try:
            res = engine.transcribe(wav, cfg.get("asr.language") or "zh")
            return row, res, time.time() - t0, None
        except Exception as e:  # noqa: BLE001
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
        sec = sec_uid_from(author) if author else None
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
    sec = sec_uid_from(author) if author else None
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
