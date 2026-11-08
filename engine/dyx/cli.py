"""Command line entry point. Every command prints one JSON object to stdout."""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from . import ops
from .config import Config
from .store import Store
from .util import DyxError, log

LOG_FILE: str | None = None


def _tee(msg: str) -> None:
    log(msg)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    sys.stdout.flush()


def _read_task(cfg, task_id: str) -> dict:
    """读 tasks/<id>/task.json，返回 {title, ids, sec_uids}。"""
    import json as _json
    import os as _os
    base = _os.path.join(_os.path.dirname(cfg.db_path), "tasks", task_id)
    path = _os.path.join(base, "task.json")
    out = {"title": task_id, "ids": [], "sec_uids": []}
    try:
        data = _json.loads(open(path, encoding="utf-8").read())
    except OSError:
        return out
    out["title"] = data.get("title") or task_id
    inp = data.get("input") or {}
    out["ids"] = [str(i) for i in (inp.get("videos") or [])]
    out["sec_uids"] = [str(s) for s in (inp.get("authors") or [])]
    return out


def _split(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    out: list[str] = []
    for v in values:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dyx", description="Douyin export engine")
    p.add_argument("--home", default=None, help="library home directory")
    p.add_argument("--log-file", default=None, help="append progress lines here")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detail", help="抓取单个作品的元数据")
    d.add_argument("target")

    s = sub.add_parser("sync", help="同步作者主页作品清单（增量）")
    s.add_argument("author")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--full", action="store_true", help="全量，不在遇到旧作品时停止")
    s.add_argument("--stop-after-known", type=int, default=25)
    s.add_argument("--download", action="store_true")

    dl = sub.add_parser("download", help="下载无水印视频")
    dl.add_argument("--ids", nargs="*")
    dl.add_argument("--author", default=None)
    dl.add_argument("--limit", type=int, default=None)
    dl.add_argument("--out", default=None, help="产出目录（任务目录）")

    tr = sub.add_parser("transcribe", help="转写为文字")
    tr.add_argument("--ids", nargs="*")
    tr.add_argument("--author", default=None)
    tr.add_argument("--limit", type=int, default=None)
    tr.add_argument("--engine", default=None)
    tr.add_argument("--force", action="store_true")
    tr.add_argument("--formats", nargs="*", default=None,
                    help="转写完成后顺带导出这些格式")
    tr.add_argument("--out", default=None,
                    help="任务目录：转写 JSON 与导出文件都落到这里")
    tr.add_argument("--with-stats", action="store_true",
                   help="导出文本里补一行互动数据（点赞/评论/转发/收藏）")
    tr.add_argument("--timestamps", action="store_true",
                   help="导出文本带 [mm:ss] 时间戳（默认不带；JSON 永远有）")
    tr.add_argument("--stats-fields", nargs="*", default=None,
                   help="只导这些互动数据：digg_count comment_count share_count collect_count")
    tr.add_argument("--published", action="store_true",
                   help="正文里写发布日期")
    tr.add_argument("--with-link", action="store_true",
                   help="正文里写抖音原视频链接（默认不写）")
    tr.add_argument("--merge-order", choices=["asc", "desc"], default=None,
                   help="合集排序：desc 最新在前（默认），asc 最老在前")

    ex = sub.add_parser("export", help="排版导出")
    ex.add_argument("--ids", nargs="*")
    ex.add_argument("--author", default=None)
    ex.add_argument("--limit", type=int, default=None)
    ex.add_argument("--formats", nargs="*", default=None)
    ex.add_argument("--out", default=None, help="任务目录：导出文件落到这里")
    ex.add_argument("--merge", action="store_true", help="另外把所有条目合成一个大文件")
    ex.add_argument("--merge-formats", nargs="*", default=None,
                    help="合集格式：md txt html docx（默认 md）")
    ex.add_argument("--merge-title", default=None, help="合集标题")
    ex.add_argument("--with-stats", action="store_true",
                   help="导出文本里补一行互动数据（点赞/评论/转发/收藏）")
    ex.add_argument("--timestamps", action="store_true",
                   help="导出文本带 [mm:ss] 时间戳（默认不带；JSON 永远有）")
    ex.add_argument("--stats-fields", nargs="*", default=None,
                   help="只导这些互动数据：digg_count comment_count share_count collect_count")
    ex.add_argument("--published", action="store_true",
                   help="正文里写发布日期")
    ex.add_argument("--with-link", action="store_true",
                   help="正文里写抖音原视频链接（默认不写）")
    ex.add_argument("--merge-order", choices=["asc", "desc"], default=None,
                   help="合集排序：desc 最新在前（默认），asc 最老在前")

    ad = sub.add_parser("add", help="粘贴作者链接/作品链接/分享口令，自动识别并入库")
    ad.add_argument("text", nargs="?", default=None, help="多行文本；省略或 '-' 时读 stdin")
    ad.add_argument("--file", default=None, help="从 UTF-8 文本文件读取多行输入")
    ad.add_argument("--download", action="store_true")
    ad.add_argument("--full", action="store_true", help="作者做全量而不是差量")
    ad.add_argument("--limit-per-author", type=int, default=None)
    ad.add_argument("--out", default=None,
                    help="任务目录（视频仍落到输出目录，这里只记录）")

    r = sub.add_parser("run", help="一站式：抓取→下载→转写→导出")
    r.add_argument("target", nargs="?", default=None,
                   help="作品/作者链接，也可以是多行文本")
    r.add_argument("--file", default=None, help="从 UTF-8 文本文件读取多行输入")
    r.add_argument("--full", action="store_true")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--formats", nargs="*", default=None)
    r.add_argument("--engine", default=None)
    r.add_argument("--out", default=None, help="任务目录：转写 JSON 与导出文件落到这里")
    r.add_argument("--merge", action="store_true", help="转写后额外合并成一个合集文件")
    r.add_argument("--merge-formats", nargs="*", default=None,
                   help="合集格式：md/txt/html/docx")
    r.add_argument("--merge-title", default=None, help="合集标题")
    r.add_argument("--with-stats", action="store_true",
                   help="导出文本里补一行互动数据（点赞/评论/转发/收藏）")
    r.add_argument("--timestamps", action="store_true",
                   help="导出文本带 [mm:ss] 时间戳（默认不带；JSON 永远有）")
    r.add_argument("--stats-fields", nargs="*", default=None,
                   help="只导这些互动数据：digg_count comment_count share_count collect_count")
    r.add_argument("--published", action="store_true",
                   help="正文里写发布日期")
    r.add_argument("--with-link", action="store_true",
                   help="正文里写抖音原视频链接（默认不写）")
    r.add_argument("--merge-order", choices=["asc", "desc"], default=None,
                   help="合集排序：desc 最新在前（默认），asc 最老在前")

    lg = sub.add_parser("login", help="打开浏览器扫码登录（可选）")
    lg.add_argument("--wait", type=int, default=240)
    lg.add_argument("--qr-out", default=None, help="二维码截图落到这个路径（供 Web 页面显示）")
    lg.add_argument("--status-out", default=None, help="登录状态 JSON 落到这个路径（供轮询）")

    sub.add_parser("warm", help="预热浏览器，取得风控 cookie")

    sub.add_parser("library", help="库统计")
    sub.add_parser("authors", help="作者列表")

    v = sub.add_parser("videos", help="作品列表")
    v.add_argument("--author", default=None, help="只看这个作者（主页链接或 sec_uid）")
    v.add_argument("--status", default=None, help="listed / downloaded / failed / skipped")
    v.add_argument("--q", default=None, help="按标题或作品 ID 搜")
    v.add_argument("--task", default=None, help="只看这个任务里的作品（任务 ID）")
    v.add_argument("--limit", type=int, default=50)
    v.add_argument("--offset", type=int, default=0)

    fl = sub.add_parser("failures", help="诊断失败原因：下载失败分类、图集/无人声跳过、历史欠费")
    fl.add_argument("--author", default=None, help="作者主页链接或 sec_uid")
    fl.add_argument("--ids", nargs="+", default=None, help="只看这些作品")
    fl.add_argument("--limit", type=int, default=300)
    fl.add_argument("--no-logs", action="store_true", help="不扫描任务日志（更快）")

    rt = sub.add_parser("retry", help="只重试「可抢救」的下载失败（风控/超时/磁盘），404/403 不动")
    rt.add_argument("--author", default=None, help="作者主页链接或 sec_uid")
    rt.add_argument("--ids", nargs="+", default=None, help="只重试这些作品")
    rt.add_argument("--limit", type=int, default=500)
    rt.add_argument("--out", default=None, help="任务目录（产物落这里）")

    st = sub.add_parser("settings", help="读取/写入配置")
    st.add_argument("action", choices=["get", "set", "install-model", "reset"])
    st.add_argument("--key", default=None)
    st.add_argument("--value", default=None)
    st.add_argument("--engine", default=None)

    pl = sub.add_parser("polish", help="AI 校对：抽样挖术语表 + 全库确定性替换（不重写正文）")
    pl.add_argument("--author", default=None, help="只处理这个作者（主页链接或 sec_uid）")
    pl.add_argument("--ids", nargs="+", default=None, help="只处理这些作品")
    pl.add_argument("--sample", type=int, default=None,
                    help="抽样多少条代表作去问模型（默认取配置 polish.sampleSize）")
    pl.add_argument("--no-apply", action="store_true", help="只挖术语表，先不刷全库")
    pl.add_argument("--dump-samples", action="store_true", dest="dump_samples",
                    help="只输出抽样文本（不调模型），给 Host 侧用 DSH 自己的模型")
    pl.add_argument("--ingest", default=None,
                    help="读取外部模型回复的 JSON（[{aweme_id, reply}]）→ 解析并落候选")
    pl.add_argument("--show", action="store_true", help="只列出已有术语表")
    pl.add_argument("--apply-only", action="store_true", help="只按现有术语表刷库，不再问模型")
    pl.add_argument("--auto", action="store_true",
                    help="允许按门槛自动应用 AI 候选术语（默认只应用人工采纳的）")
    pl.add_argument("--accept", nargs="+", default=None,
                    help='手工收录术语，例如 --accept "达叔=>达说"（manual，一定参与替换）')
    pl.add_argument("--clear", action="store_true", help="清空术语表")
    pl.add_argument("--task", default=None,
                    help="只处理某个任务目录里的作品（tasks/<id>）")
    pl.add_argument("--drop", nargs="+", default=None,
                    help='否掉坏术语，例如 --drop "研学=>眼学"')
    pl.add_argument("--reset", action="store_true", help="撤销所有校对（清空 polished，导出回到原始转写）")
    pl.add_argument("--accept-local", nargs="+", default=None, dest="accept_local",
                    help='只改这一处（按上下文定位），例如 --accept-local "城市=>处室 || 通常是和博物馆统筹在一个城市"')
    pl.add_argument("--patches", action="store_true",
                    help="列出所有「只改这处」的修正")
    pl.add_argument("--limit", type=int, default=300, help="--show 时最多列多少条")

    en = sub.add_parser("engines", help="列出所有可用的转写引擎（位置/价格/时间戳/前置条件）")
    en.add_argument("--seconds", type=float, default=None,
                    help="给定时长（秒）时附带「这一批大概多少钱」的估算")

    return p


def _export_opts(args) -> dict:
    """导出高级设置：互动数据（可逐项）/ 时间戳，默认都不开。"""
    fields = getattr(args, "stats_fields", None) or []
    if isinstance(fields, str):
        fields = [p for p in fields.replace(",", " ").split() if p]
    return {"stats": bool(getattr(args, "with_stats", False)),
            "statsFields": list(fields),
            "timestamps": bool(getattr(args, "timestamps", False)),
            "link": bool(getattr(args, "with_link", False)),
            "published": bool(getattr(args, "published", False)),
            "order": getattr(args, "merge_order", None)}


def main(argv: list[str] | None = None) -> int:
    global LOG_FILE
    args = build_parser().parse_args(argv)
    LOG_FILE = args.log_file
    cfg = Config(args.home)
    store: Store | None = None
    try:
        if args.cmd == "settings" and args.action in ("get", "set", "reset", "install-model"):
            if args.cmd == "settings" and args.action == "get":
                from . import catalog
                emit({"ok": True, "config": cfg.public(), "configPath": cfg.path,
                      "engines": catalog.summary_rows()})
                return 0
            if args.action == "set":
                if not args.key:
                    raise DyxError("需要 --key", code="bad_args")
                value = args.value
                try:
                    value = json.loads(value) if value is not None else None
                except json.JSONDecodeError:
                    pass
                cfg.set(args.key, value)
                cfg.save()
                emit({"ok": True, "config": cfg.public()})
                return 0
            if args.action == "reset":
                if os.path.exists(cfg.path):
                    os.remove(cfg.path)
                emit({"ok": True, "reset": True, "configPath": cfg.path})
                return 0
            if args.action == "install-model":
                from .models import install_model
                emit({"ok": True, "result": install_model(cfg, args.engine or "sensevoice",
                                                          log=_tee)})
                return 0

        if args.cmd == "engines":
            from . import catalog
            rows = catalog.summary_rows(args.seconds)
            emit({"ok": True, "count": len(rows), "engines": rows,
                  "priceSources": catalog.PRICE_SOURCES})
            return 0

        store = Store(cfg.db_path)

        if args.cmd == "detail":
            emit({"ok": True, "result": ops.fetch_one(cfg, store, args.target)})

        elif args.cmd == "sync":
            emit({"ok": True, "result": ops.sync_author(
                cfg, store, args.author, limit=args.limit,
                incremental=not args.full, stop_after_known=args.stop_after_known,
                download=args.download, cb=None)})

        elif args.cmd == "download":
            emit({"ok": True, "result": ops.download_many(
                cfg, store, _split(args.ids), args.author, args.limit, cb=None)})

        elif args.cmd == "retry":
            emit({"ok": True, "result": ops.retry_recoverable(
                cfg, store, _split(args.ids), args.author, args.limit,
                cb=None, out_dir=args.out)})

        elif args.cmd == "transcribe":
            opts = _export_opts(args)
            res = ops.transcribe_many(
                cfg, store, _split(args.ids), args.author, args.limit,
                args.engine, args.force, cb=None, out_dir=args.out)
            fmts = _split(args.formats)
            if res.get('paused'):
                _tee('⏸ ' + str(res.get('pauseReason') or ''))
                emit({'ok': True, 'result': res})
                return 0
            if fmts:
                ok_ids = [str(item["aweme_id"]) for item in res.get("ok", [])]
                if ok_ids:
                    res["exports"] = ops.export_many(
                        cfg, store, ok_ids, None, None, fmts, cb=None,
                        out_dir=args.out, options=opts)
            emit({"ok": True, "result": res})

        elif args.cmd == "export":
            emit({"ok": True, "result": ops.export_many(
                cfg, store, _split(args.ids), args.author, args.limit,
                _split(args.formats), cb=None, out_dir=args.out,
                merge=getattr(args, 'merge', False),
                merge_formats=getattr(args, 'merge_formats', None),
                merge_title=getattr(args, 'merge_title', None),
                options=_export_opts(args))})

        elif args.cmd == "add":
            text = args.text
            if args.file:
                with open(args.file, encoding="utf-8") as fh:
                    text = fh.read()
            elif text is None or text == "-":
                text = sys.stdin.read()
            emit({"ok": True, "result": ops.add_targets(
                cfg, store, text, download=args.download, full=args.full,
                limit_per_author=args.limit_per_author, cb=None,
                      out_dir=getattr(args, 'out', None,))})

        elif args.cmd == "run":
            text = args.target
            if args.file:
                with open(args.file, encoding="utf-8") as fh:
                    text = fh.read()
            elif text == "-":
                text = sys.stdin.read()
            emit({"ok": True, "result": ops.pipeline_text(
                cfg, store, text, formats=_split(args.formats),
                engine=args.engine, full=args.full,
                limit_per_author=args.limit, cb=None, out_dir=args.out,
                merge=getattr(args, "merge", False),
                merge_formats=_split(getattr(args, "merge_formats", None)),
                merge_title=getattr(args, "merge_title", None),
                options=_export_opts(args))})

        elif args.cmd == "login":
            from .browser import BrowserSession
            with BrowserSession(cfg) as s:
                s.warm()
                emit({"ok": True, "result": s.login(args.wait, qr_out=args.qr_out,
                                                 status_out=args.status_out)})

        elif args.cmd == "warm":
            from .browser import BrowserSession
            with BrowserSession(cfg) as s:
                s.warm(force=True)
                emit({"ok": True, "result": {"warm": True, "loggedIn": s.is_logged_in()}})

        elif args.cmd == "failures":
            from .diagnose import report
            from .browser import sec_uid_from
            sec = sec_uid_from(args.author) if args.author else None
            emit({"ok": True, "report": report(
                cfg, store, sec_uid=sec, ids=args.ids, limit=args.limit,
                scan_logs=not args.no_logs)})

        elif args.cmd == "library":
            emit({"ok": True, "stats": store.stats(), "authors": store.list_authors(),
                  "config": cfg.public()})

        elif args.cmd == "authors":
            emit({"ok": True, "authors": store.list_authors()})

        elif args.cmd == "polish":
            from . import polish as pol
            from .browser import sec_uid_from
            sec = sec_uid_from(args.author) if args.author else None
            scope_ids = _split(args.ids)
            task_meta = None
            if getattr(args, "task", None):
                task_meta = _read_task(cfg, args.task)
                merged = list(dict.fromkeys((scope_ids or []) + (task_meta.get("ids") or [])))
                scope_ids = merged or None
                if not scope_ids and not sec and task_meta.get("sec_uids"):
                    sec = task_meta["sec_uids"][0]
            if args.reset:
                n = store.reset_polished()
                emit({"ok": True, "reset": n})
            elif args.clear:
                store.clear_terms(sec)
                emit({"ok": True, "cleared": True, "terms": store.list_terms(sec, args.limit)})
            elif args.drop:
                pairs = pol.parse_pairs_arg(args.drop)
                n = pol.drop_pairs(store, args.drop, sec)
                emit({"ok": True, "dropped": n, "pairs": pairs,
                      "terms": store.list_terms(sec, args.limit)})
            elif getattr(args, "dump_samples", False):
                out = pol.dump_samples(cfg, store, sec, args.sample, scope_ids)
                emit({"ok": True, **out})
            elif getattr(args, "ingest", None):
                data = json.loads(open(args.ingest, "r", encoding="utf-8").read())
                replies = data.get("replies") if isinstance(data, dict) else data
                res = pol.ingest_replies(cfg, store, replies or [], sec)
                emit({"ok": True, "mine": res,
                      "terms": store.list_terms(sec, args.limit),
                      "patches": store.list_patches(sec, 200)})
            elif args.show:
                emit({"ok": True, "terms": store.list_terms(sec, args.limit)})
            elif args.accept:
                pairs = pol.parse_pairs_arg(args.accept)
                n = pol.accept_terms(store, pairs, sec)
                emit({"ok": True, "accepted": n, "pairs": pairs,
                      "terms": store.list_terms(sec, args.limit)})
            elif getattr(args, "accept_local", None):
                items = pol.parse_local_arg(args.accept_local)
                done = pol.accept_local(cfg, store, items, cb=_tee)
                emit({"ok": True, **done, "items": items,
                      "patches": store.list_patches(200),
                      "terms": store.list_terms(sec, args.limit)})
            elif getattr(args, "patches", False):
                emit({"ok": True, "patches": store.list_patches(args.limit)})
            elif args.apply_only:
                applied = pol.apply_terms(cfg, store, sec_uid=sec, cb=_tee,
                                          auto=bool(args.auto), ids=scope_ids)
                emit({"ok": True, "apply": applied, "terms": store.list_terms(sec, 200)})
            else:
                mine = pol.mine(cfg, store, sec_uid=sec, sample=args.sample,
                                ids=scope_ids, cb=_tee)
                applied = ({"skipped": True} if args.no_apply
                           else pol.apply_terms(cfg, store, sec_uid=sec, cb=_tee,
                                                auto=bool(args.auto), ids=scope_ids))
                emit({"ok": True, "mine": mine, "apply": applied,
                      "terms": store.list_terms(sec, 200)})

        elif args.cmd == "videos":
            from .browser import sec_uid_from
            sec = sec_uid_from(args.author) if args.author else None
            ids = None
            task_title = None
            if args.task:
                path = os.path.join(os.path.dirname(cfg.db_path), "tasks",
                                    str(args.task), "task.json")
                if os.path.exists(path):
                    with io.open(path, encoding="utf-8") as fh:
                        task = json.load(fh)
                    task_title = task.get("title")
                    inp = task.get("input") or {}
                    authors = [a for a in (inp.get("authors") or []) if isinstance(a, str)]
                    vids = [v for v in (inp.get("videos") or []) if isinstance(v, str)]
                    if authors and not sec:
                        sec = sec_uid_from(authors[0]) or authors[0]
                    ids = vids or None
            rows = store.query_videos(sec_uid=sec, status=args.status, q=args.q,
                                      aweme_ids=ids, limit=args.limit,
                                      offset=args.offset)
            total = store.count_videos(sec_uid=sec, status=args.status, q=args.q,
                                       aweme_ids=ids)
            emit({"ok": True, "videos": rows, "count": len(rows), "total": total,
                  "task": args.task, "taskTitle": task_title,
                  "authors": store.list_authors()})
        else:
            raise DyxError(f"未知命令 {args.cmd}", code="bad_command")
        return 0

    except DyxError as e:
        emit({"ok": False, "error": e.to_dict()})
        return 2
    except Exception as e:  # noqa: BLE001
        emit({"ok": False, "error": {"code": "unexpected", "message": str(e),
                                     "traceback": traceback.format_exc()[-1500:]}})
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
