"""SQLite library: authors, videos, transcripts, exports, incremental cursors."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
  sec_uid        TEXT PRIMARY KEY,
  uid            TEXT,
  nickname       TEXT,
  unique_id      TEXT,
  signature      TEXT,
  avatar         TEXT,
  aweme_count    INTEGER,
  follower_count INTEGER,
  total_favorited INTEGER,
  cursor         TEXT,
  synced_at      TEXT,
  complete       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS videos (
  aweme_id     TEXT PRIMARY KEY,
  sec_uid      TEXT,
  author_name  TEXT,
  desc         TEXT,
  create_time  INTEGER,
  duration_ms  INTEGER,
  width        INTEGER,
  height       INTEGER,
  play_uri     TEXT,
  ratio        TEXT,
  cover        TEXT,
  image_count  INTEGER DEFAULT 0,
  is_image_post INTEGER DEFAULT 0,
  stats_json   TEXT,
  tags         TEXT,
  raw_json     TEXT,
  status       TEXT DEFAULT 'listed',
  file_path    TEXT,
  file_size    INTEGER,
  audio_path   TEXT,
  downloaded_at TEXT,
  error        TEXT,
  media_width  INTEGER,
  media_height INTEGER,
  platform     TEXT DEFAULT 'douyin',
  share_url    TEXT,
  subtitle_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_author ON videos(sec_uid);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);

CREATE TABLE IF NOT EXISTS transcripts (
  aweme_id   TEXT PRIMARY KEY,
  engine     TEXT,
  model      TEXT,
  language   TEXT,
  duration   REAL,
  segment_count INTEGER,
  segments_json TEXT,
  text       TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS terms (
  sec_uid    TEXT,
  wrong      TEXT,
  right      TEXT,
  hits       INTEGER DEFAULT 1,
  videos     INTEGER DEFAULT 0,
  source     TEXT,
  created_at TEXT,
  PRIMARY KEY (sec_uid, wrong)
);

CREATE TABLE IF NOT EXISTS patches (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  aweme_id   TEXT,
  wrong      TEXT,
  right      TEXT,
  context    TEXT,
  source     TEXT DEFAULT 'manual',
  created_at TEXT,
  UNIQUE(aweme_id, wrong, context)
);
CREATE INDEX IF NOT EXISTS idx_patches_video ON patches(aweme_id);

CREATE TABLE IF NOT EXISTS exports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  aweme_id   TEXT,
  fmt        TEXT,
  path       TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_exports_video ON exports(aweme_id);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:

        # 校对后的文本（AI 术语表确定性替换的结果，原文保留在 text 列）
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(transcripts)")}
        if "polished" not in cols:
            self.conn.execute("ALTER TABLE transcripts ADD COLUMN polished TEXT")
        tcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(terms)")}
        if "videos" not in tcols:
            self.conn.execute("ALTER TABLE terms ADD COLUMN videos INTEGER DEFAULT 0")
        if "context" not in tcols:
            self.conn.execute("ALTER TABLE terms ADD COLUMN context TEXT")

        """Add columns introduced after a library file was first created."""
        wanted = {
            "videos": [("media_width", "INTEGER"), ("media_height", "INTEGER"),
                       ("no_speech", "INTEGER DEFAULT 0"),
            ("platform", "TEXT DEFAULT 'douyin'"),
            ("share_url", "TEXT"), ("subtitle_url", "TEXT")],
        }
        for table, cols in wanted.items():
            have = {r["name"] for r in
                    self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- authors
    def upsert_author(self, a: dict) -> None:
        self.conn.execute(
            """INSERT INTO authors(sec_uid,uid,nickname,unique_id,signature,avatar,
                 aweme_count,follower_count,total_favorited,synced_at)
               VALUES(:sec_uid,:uid,:nickname,:unique_id,:signature,:avatar,
                 :aweme_count,:follower_count,:total_favorited,:synced_at)
               ON CONFLICT(sec_uid) DO UPDATE SET
                 uid=COALESCE(excluded.uid,uid),
                 nickname=COALESCE(excluded.nickname,nickname),
                 unique_id=COALESCE(excluded.unique_id,unique_id),
                 signature=COALESCE(excluded.signature,signature),
                 avatar=COALESCE(excluded.avatar,avatar),
                 aweme_count=COALESCE(excluded.aweme_count,aweme_count),
                 follower_count=COALESCE(excluded.follower_count,follower_count),
                 total_favorited=COALESCE(excluded.total_favorited,total_favorited),
                 synced_at=excluded.synced_at""",
            {
                "sec_uid": a.get("sec_uid"),
                "uid": a.get("uid"),
                "nickname": a.get("nickname"),
                "unique_id": a.get("unique_id"),
                "signature": a.get("signature"),
                "avatar": a.get("avatar"),
                "aweme_count": a.get("aweme_count"),
                "follower_count": a.get("follower_count"),
                "total_favorited": a.get("total_favorited"),
                "synced_at": now_iso(),
            },
        )
        self.conn.commit()

    def get_author(self, sec_uid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM authors WHERE sec_uid=?", (sec_uid,)).fetchone()
        return dict(r) if r else None

    def set_author_cursor(self, sec_uid: str, cursor: str | int | None,
                          complete: bool = False) -> None:
        self.conn.execute(
            "UPDATE authors SET cursor=?, complete=?, synced_at=? WHERE sec_uid=?",
            (str(cursor) if cursor is not None else None, 1 if complete else 0,
             now_iso(), sec_uid))
        self.conn.commit()

    def list_authors(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT a.*, (SELECT COUNT(*) FROM videos v WHERE v.sec_uid=a.sec_uid) AS stored
               FROM authors a ORDER BY a.synced_at DESC""").fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- videos
    def upsert_video(self, v: dict) -> str:
        """Return 'new' | 'updated'."""
        existing = self.conn.execute(
            "SELECT aweme_id, status FROM videos WHERE aweme_id=?", (v["aweme_id"],)).fetchone()
        cols = ["aweme_id", "sec_uid", "author_name", "desc", "create_time", "duration_ms",
                "width", "height", "play_uri", "ratio", "cover", "image_count",
                "is_image_post", "stats_json", "tags", "raw_json", "platform", "share_url"]
        payload = {c: v.get(c) for c in cols}
        if existing:
            sets = ",".join(f"{c}=COALESCE(:{c},{c})" for c in cols[1:])
            self.conn.execute(f"UPDATE videos SET {sets} WHERE aweme_id=:aweme_id", payload)
            self.conn.commit()
            return "updated"
        payload["status"] = "listed"
        self.conn.execute(
            f"INSERT INTO videos({','.join(cols)}) VALUES({','.join(':'+c for c in cols)})",
            payload)
        self.conn.commit()
        return "new"

    def mark_downloaded(self, aweme_id: str, file_path: str, size: int,
                        audio_path: str | None, ratio: str | None,
                        width: int | None = None, height: int | None = None,
                        subtitle_url: str | None = None) -> None:
        self.conn.execute(
            """UPDATE videos SET status='downloaded', file_path=?, file_size=?,
                 audio_path=COALESCE(?,audio_path), ratio=COALESCE(?,ratio),
                 media_width=COALESCE(?,media_width), media_height=COALESCE(?,media_height),
                 subtitle_url=COALESCE(?,subtitle_url),
                 downloaded_at=?, error=NULL WHERE aweme_id=?""",
            (file_path, size, audio_path, ratio, width, height, subtitle_url,
             now_iso(), aweme_id))
        self.conn.commit()

    def mark_failed(self, aweme_id: str, err: str) -> None:
        self.conn.execute(
            "UPDATE videos SET status='failed', error=? WHERE aweme_id=?",
            (err[:500], aweme_id))
        self.conn.commit()

    def get_video(self, aweme_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM videos WHERE aweme_id=?", (aweme_id,)).fetchone()
        return dict(r) if r else None

    def find_video_fuzzy(self, needle: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM videos WHERE aweme_id=? OR aweme_id LIKE ? LIMIT 1",
            (needle, f"%{needle}%")).fetchone()
        return dict(r) if r else None

    def _video_where(self, sec_uid=None, status=None, has_transcript=None,
                     q=None, aweme_ids=None):
        """资料库筛选条件（query 与 count 共用）。"""
        sql = " WHERE 1=1"
        args: list[Any] = []
        if sec_uid:
            sql += " AND v.sec_uid=?"
            args.append(sec_uid)
        if status:
            sql += " AND v.status=?"
            args.append(status)
        if q:
            sql += " AND (v.desc LIKE ? OR v.author_name LIKE ? OR v.aweme_id LIKE ?)"
            like = "%" + q + "%"
            args += [like, like, like]
        if aweme_ids:
            marks = ",".join("?" * len(aweme_ids))
            sql += f" AND v.aweme_id IN ({marks})"
            args += [str(x) for x in aweme_ids]
        if has_transcript is True:
            sql += " AND t.aweme_id IS NOT NULL"
        elif has_transcript is False:
            sql += " AND t.aweme_id IS NULL"
        return sql, args

    def count_videos(self, sec_uid=None, status=None, has_transcript=None,
                     q=None, aweme_ids=None) -> int:
        where, args = self._video_where(sec_uid, status, has_transcript, q, aweme_ids)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM videos v LEFT JOIN transcripts t"
            " ON t.aweme_id=v.aweme_id" + where, args).fetchone()
        return int((row or {"n": 0})["n"] or 0)

    def query_videos(self, sec_uid: str | None = None, status: str | None = None,
                     has_transcript: bool | None = None, limit: int = 100,
                     offset: int = 0, order: str = "create_time DESC",
                     q: str | None = None, aweme_ids: list | None = None) -> list[dict]:
        where, args = self._video_where(sec_uid, status, has_transcript, q, aweme_ids)
        sql = ("SELECT v.*, (t.aweme_id IS NOT NULL) AS has_transcript"
               " FROM videos v LEFT JOIN transcripts t ON t.aweme_id=v.aweme_id" + where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def pending(self, kind: str, sec_uid: str | None = None, limit: int = 100,
                since: int | None = None) -> list[dict]:
        if kind == "download":
            rows = self.conn.execute(
                """SELECT v.* FROM videos v
                   WHERE v.status='listed' AND COALESCE(v.is_image_post, 0)=0
                     AND (:sec IS NULL OR v.sec_uid=:sec)
                   ORDER BY v.create_time DESC LIMIT :lim""",
                {"sec": sec_uid, "lim": limit}).fetchall()
            return [dict(r) for r in rows]
        rows = self.conn.execute(
            """SELECT v.* FROM videos v LEFT JOIN transcripts t ON t.aweme_id=v.aweme_id
               WHERE v.status='downloaded' AND t.aweme_id IS NULL
                 AND COALESCE(v.no_speech, 0)=0
                 AND (:sec IS NULL OR v.sec_uid=:sec)
               ORDER BY v.create_time DESC LIMIT :lim""",
            {"sec": sec_uid, "lim": limit}).fetchall()
        return [dict(r) for r in rows]

    def mark_skipped(self, aweme_id: str, reason: str) -> None:
        """标记「有意跳过」（图集作品等），不算失败、也不再重试。"""
        self.conn.execute("UPDATE videos SET status='skipped', error=? WHERE aweme_id=?",
                          (str(reason)[:200], str(aweme_id)))
        self.conn.commit()

    def mark_listed(self, aweme_id: str) -> None:
        """把 skipped/failed 复位成 listed（需要重新尝试时用）。"""
        self.conn.execute("UPDATE videos SET status='listed', error=NULL WHERE aweme_id=?",
                          (str(aweme_id),))
        self.conn.commit()

    def mark_no_speech(self, aweme_id: str, flag: bool = True) -> None:
        """标记「已确认无人声」，避免每轮重复送 ASR 白花钱。"""
        self.conn.execute("UPDATE videos SET no_speech=? WHERE aweme_id=?",
                          (1 if flag else 0, str(aweme_id)))
        self.conn.commit()

    def no_speech_ids(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT aweme_id, desc, duration_ms FROM videos
               WHERE COALESCE(no_speech,0)=1 ORDER BY create_time DESC LIMIT ?""",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- transcripts
    def save_transcript(self, aweme_id: str, engine: str, model: str, language: str,
                        duration: float, segments: list[dict], text: str) -> None:
        self.conn.execute(
            """INSERT INTO transcripts(aweme_id,engine,model,language,duration,
                 segment_count,segments_json,text,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(aweme_id) DO UPDATE SET
                 engine=excluded.engine, model=excluded.model, language=excluded.language,
                 duration=excluded.duration, segment_count=excluded.segment_count,
                 segments_json=excluded.segments_json, text=excluded.text,
                 created_at=excluded.created_at""",
            (aweme_id, engine, model, language, duration, len(segments),
             json.dumps(segments, ensure_ascii=False), text, now_iso()))
        self.conn.commit()

    def get_transcript(self, aweme_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM transcripts WHERE aweme_id=?", (aweme_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["segments"] = json.loads(d.pop("segments_json") or "[]")
        d["raw_text"] = d.get("text")
        if d.get("polished"):
            d["text"] = d["polished"]
        return d


    # ----------------------------------------------------------- 校对（A2）
    def save_terms(self, sec_uid: str, pairs: dict, source: str = "ai",
                   videos: dict | None = None, contexts: dict | None = None) -> int:
        """pairs = {错: 正}；videos = {错: 命中了几条不同视频}；contexts = {错: 原句}。"""
        now = now_iso()
        n = 0
        if source == "manual":
            heads = [k for k in (pairs or {}) if (k or "").strip()]
            if heads:
                self.conn.execute(
                    f"DELETE FROM terms WHERE wrong IN ({','.join('?' * len(heads))})",
                    list(heads),
                )
        for wrong, right in (pairs or {}).items():
            wrong = (wrong or "").strip()
            right = (right or "").strip()
            if not wrong or not right or wrong == right:
                continue
            vids = int((videos or {}).get(wrong, 1) or 1)
            ctx = ((contexts or {}).get(wrong) or "").strip()[:160]
            self.conn.execute(
                """INSERT INTO terms(sec_uid,wrong,right,hits,videos,source,created_at,context)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(sec_uid,wrong) DO UPDATE SET
                     right=excluded.right,
                     hits=terms.hits+excluded.hits,
                     videos=MAX(terms.videos, excluded.videos),
                     context=COALESCE(NULLIF(excluded.context,''), terms.context),
                     source=CASE WHEN terms.source='manual' THEN 'manual' ELSE excluded.source END""",
                ("" if source == "manual" else (sec_uid or ""),
                 wrong, right, vids, vids, source, now, ctx))
            n += 1
        self.conn.commit()
        return n

    # --------------------------------------------- 逐处修正（按上下文定位）
    def save_patch(self, aweme_id: str, wrong: str, right: str, context: str) -> int:
        """只对某一条作品、某一处上下文做替换；拼错位置也不会污染全库。"""
        wrong = (wrong or "").strip()
        right = (right or "").strip()
        context = (context or "").strip()[:160]
        if not (aweme_id and wrong and right and wrong != right):
            return 0
        cur = self.conn.execute(
            """INSERT INTO patches(aweme_id,wrong,right,context,source,created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(aweme_id,wrong,context) DO UPDATE SET right=excluded.right""",
            (str(aweme_id), wrong, right, context, "manual", now_iso()))
        self.conn.commit()
        return cur.rowcount or 1

    def patches_for(self, aweme_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT wrong,right,context FROM patches WHERE aweme_id=?", (str(aweme_id),))
        return [dict(r) for r in rows.fetchall()]

    def list_patches(self, sec_uid: str | None = None, limit: int = 500) -> list[dict]:
        if sec_uid:
            rows = self.conn.execute(
                """SELECT p.aweme_id,p.wrong,p.right,p.context,p.source,p.created_at
                     FROM patches p JOIN videos v ON v.aweme_id = p.aweme_id
                    WHERE v.sec_uid = ?
                    ORDER BY p.id DESC LIMIT ?""", (sec_uid, limit))
        else:
            rows = self.conn.execute(
                """SELECT aweme_id,wrong,right,context,source,created_at FROM patches
                   ORDER BY id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows.fetchall()]

    def drop_patch(self, aweme_id: str, wrong: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM patches WHERE aweme_id=? AND wrong=?", (str(aweme_id), wrong))
        self.conn.commit()
        return cur.rowcount or 0

    def ids_by_context(self, context: str, limit: int = 5) -> list[str]:
        """按上下文原句反查是哪一条作品（用于「只改这处」）。"""
        needle = (context or "").strip()[:60]
        if not needle:
            return []
        rows = self.conn.execute(
            """SELECT aweme_id FROM transcripts
               WHERE text LIKE ? OR polished LIKE ? LIMIT ?""",
            (f"%{needle}%", f"%{needle}%", limit))
        return [r[0] for r in rows.fetchall()]

    def manual_fixes(self, sec_uid: str | None = None) -> dict:
        """已采纳（manual）的术语：{错: 正}，供导出时对分段文本做确定性替换。"""
        sql = ("SELECT wrong, right FROM terms WHERE source='manual' "
               "AND (sec_uid=? OR sec_uid='' OR sec_uid IS NULL)")
        return {r[0]: r[1] for r in self.conn.execute(sql, (sec_uid or "",)).fetchall()}

    def drop_terms(self, sec_uid: str | None = None, wrongs: list[str] | None = None) -> int:
        """删掉指定错词（或该账号全部）的术语。"""
        sql = "DELETE FROM terms WHERE 1=1"
        args: list = []
        if sec_uid is not None:
            sql += " AND sec_uid=?"
            args.append(sec_uid)
        if wrongs:
            sql += f" AND wrong IN ({','.join('?' * len(wrongs))})"
            args.extend(list(wrongs))
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return int(cur.rowcount or 0)

    def list_terms(self, sec_uid: str | None = None, limit: int = 500) -> list[dict]:
        """术语表；同错词既有 AI 候选又有已采纳时，候选会带 covered=True（面板灰显）。"""
        sql = "SELECT * FROM terms"
        args: list[Any] = []
        if sec_uid:
            sql += " WHERE sec_uid=? OR sec_uid=''"
            args.append(sec_uid)
        sql += " ORDER BY videos DESC, hits DESC, wrong LIMIT ?"
        args.append(limit)
        items = [dict(r) for r in self.conn.execute(sql, args).fetchall()]
        manual = {it["wrong"] for it in items if it.get("source") == "manual"}
        for it in items:
            it["covered"] = bool(it.get("source") != "manual" and it["wrong"] in manual)
        items.sort(key=lambda it: (it.get("source") != "manual",
                                   bool(it.get("covered")),
                                   -(it.get("videos") or 0),
                                   -(it.get("hits") or 0),
                                   it.get("wrong") or ""))
        return items


    def clear_terms(self, sec_uid: str | None = None) -> None:
        if sec_uid:
            self.conn.execute("DELETE FROM terms WHERE sec_uid=?", (sec_uid,))
        else:
            self.conn.execute("DELETE FROM terms")
        self.conn.commit()

    def reset_polished(self) -> int:
        cur = self.conn.execute("UPDATE transcripts SET polished=NULL WHERE polished IS NOT NULL")
        self.conn.commit()
        return int(cur.rowcount or 0)

    def save_polished(self, aweme_id: str, text: str) -> None:
        self.conn.execute("UPDATE transcripts SET polished=? WHERE aweme_id=?",
                          (text, aweme_id))
        self.conn.commit()

    def list_transcripts(self, sec_uid: str | None = None, only_unpolished: bool = False,
                         limit: int = 100000, ids: list[str] | None = None) -> list[dict]:
        """按作者取转写记录（带作品元信息，供抽样校对用）。"""
        sql = """SELECT t.aweme_id, t.engine, t.model, t.text, t.polished, t.duration,
                        v.sec_uid, v.author_name, v.desc, v.create_time
                 FROM transcripts t JOIN videos v ON v.aweme_id=t.aweme_id WHERE 1=1"""
        args: list[Any] = []
        if sec_uid:
            sql += " AND v.sec_uid=?"
            args.append(sec_uid)
        if ids:
            sql += f" AND t.aweme_id IN ({','.join('?' * len(ids))})"
            args.extend([str(i) for i in ids])
        if only_unpolished:
            sql += " AND COALESCE(t.polished, '')=''"
        sql += " ORDER BY v.create_time DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # -------------------------------------------------------------- exports
    def add_export(self, aweme_id: str, fmt: str, path: str) -> None:
        self.conn.execute(
            "INSERT INTO exports(aweme_id,fmt,path,created_at) VALUES(?,?,?,?)",
            (aweme_id, fmt, path, now_iso()))
        self.conn.commit()

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        one = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]  # noqa: E731
        return {
            "authors": one("SELECT COUNT(*) FROM authors"),
            "videos": one("SELECT COUNT(*) FROM videos"),
            "downloaded": one("SELECT COUNT(*) FROM videos WHERE status='downloaded'"),
            "failed": one("SELECT COUNT(*) FROM videos WHERE status='failed'"),
            "skipped": one("SELECT COUNT(*) FROM videos WHERE status='skipped'"),
            "noSpeech": one("SELECT COUNT(*) FROM videos WHERE COALESCE(no_speech,0)=1"),
            "transcribed": one("SELECT COUNT(*) FROM transcripts"),
            "exports": one("SELECT COUNT(*) FROM exports"),
            "imagePosts": one("SELECT COUNT(*) FROM videos WHERE is_image_post=1"),
            "bytes": one("SELECT COALESCE(SUM(file_size),0) FROM videos"),
        }

    def meta_get(self, key: str, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
            (key, value, value))
        self.conn.commit()
