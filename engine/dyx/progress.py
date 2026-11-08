# -*- coding: utf-8 -*-
"""进度快照。

长任务（下载/转写/导出/同步）在跑的时候，把「做到第几个、总共几个、失败几个、
每秒几个、还要多久」写成一个 JSON 文件；Web 面板读它画进度条，不用解析日志。

路径来自环境变量 ``DY_PROGRESS_FILE``（由 Host 侧的 startJob 注入）。
没设就没开销：写完即返回。
"""
from __future__ import annotations

import json
import os
import time

KEY = "DY_PROGRESS_FILE"


def _path() -> str:
    return os.environ.get(KEY) or ""


def enabled() -> bool:
    return bool(_path())


class Progress:
    """一个长任务的进度尺。线程不安全，也不需要——引擎是单线程跑的。"""

    def __init__(self, kind: str, total: int | None = None, unit: str = "项",
                 log_every: int = 10) -> None:
        self.kind = kind
        self.unit = unit
        self.total = int(total or 0)
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.phase = "准备中"
        self.note = ""
        self.started = time.time()
        self._last_write = 0.0
        self._last_log = 0.0
        self.log_every = max(1, int(log_every))
        self.write(force=True)

    # ---------------------------------------------------------------- 内部
    @property
    def processed(self) -> int:
        return self.done + self.failed + self.skipped

    def _payload(self) -> dict:
        elapsed = max(0.001, time.time() - self.started)
        payload = {
            "kind": self.kind,
            "unit": self.unit,
            "phase": self.phase,
            "note": self.note,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "processed": self.processed,
            "elapsedSec": round(elapsed, 1),
            "updatedAt": int(time.time()),
        }
        if self.total:
            payload["percent"] = round(min(100.0, self.processed * 100.0 / self.total), 1)
        rate = self.processed / elapsed if elapsed > 1.0 else 0.0
        payload["perSec"] = round(rate, 3)
        if rate > 0 and self.total and self.processed < self.total:
            payload["etaSec"] = int(round((self.total - self.processed) / rate))
        return payload

    # ---------------------------------------------------------------- 对外
    def write(self, force: bool = False) -> None:
        path = _path()
        if not path:
            return
        now = time.time()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        tmp = path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._payload(), fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    def set_phase(self, phase: str, note: str | None = None) -> None:
        self.phase = phase
        if note is not None:
            self.note = note
        self.write(force=True)

    def bump(self, ok: bool = False, failed: bool = False, skipped: bool = False,
             note: str | None = None, log: str | None = None) -> None:
        if ok:
            self.done += 1
        if failed:
            self.failed += 1
        if skipped:
            self.skipped += 1
        if note is not None:
            self.note = note
        self.write()
        if log:
            now = time.time()
            if now - self._last_log >= 2.0 or self.processed % self.log_every == 0:
                self._last_log = now
                from .util import log as _log
                total = f"/{self.total}" if self.total else ""
                _log(f"进度 {self.processed}{total} {self.unit}"
                     f"（成功 {self.done}，失败 {self.failed}"
                     + (f"，跳过 {self.skipped}" if self.skipped else "")
                     + f"）{log}")

    def finish(self, phase: str = "已完成") -> None:
        self.phase = phase
        self.write(force=True)
