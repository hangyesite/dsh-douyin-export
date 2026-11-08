"""Speech-to-text engines and the shared contract."""
from __future__ import annotations

import wave
from typing import Protocol

import numpy as np

from ..util import DyxError


class ASREngine(Protocol):
    name: str
    model_name: str

    def transcribe(self, wav_path: str, language: str) -> dict:
        ...


def read_wav_f32(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise DyxError("音频必须是 16-bit PCM", code="bad_wav")
        sr = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    return a, sr


TERMINAL = "。！？；…!?;"

def merge_short(segments: list[dict], min_len: float = 0.9,
                max_len: float = 28.0, max_gap: float = 0.7) -> list[dict]:
    """Rejoin fragments that the silence detector split inside one sentence."""
    out: list[dict] = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        s = {"start": s["start"], "end": s["end"], "text": text}
        if out:
            prev = out[-1]
            gap = s["start"] - prev["end"]
            span = s["end"] - prev["start"]
            short_prev = len(prev["text"]) < 8
            dangling = not prev["text"].endswith(tuple(TERMINAL))
            if span <= max_len and gap <= max_gap and (short_prev or dangling):
                prev["end"] = s["end"]
                prev["text"] = prev["text"] + s["text"]
                continue
        out.append(dict(s))
    final: list[dict] = []
    for s in out:
        span = s["end"] - s["start"]
        if span <= max_len:
            final.append(s)
            continue
        n = int(span // max_len) + 1
        step = span / n
        text = s["text"]
        size = max(1, len(text) // n)
        for i in range(n):
            a = s["start"] + step * i
            chunk = text[i * size:] if i == n - 1 else text[i * size:(i + 1) * size]
            final.append({"start": round(a, 2), "end": round(a + step, 2),
                          "text": chunk or text})
    return final


def get_engine(cfg):
    kind = (cfg.get("asr.engine") or "sensevoice").lower()
    if kind in ("sensevoice", "sv", "sense-voice"):
        from .sensevoice import SenseVoiceEngine
        return SenseVoiceEngine(cfg)
    if kind in ("faster-whisper", "faster_whisper", "whisper", "fw"):
        from .faster_whisper_engine import FasterWhisperEngine
        return FasterWhisperEngine(cfg)
    if kind in ("openai", "openai-compat", "api", "cloud"):
        from .openai_compat import OpenAICompatEngine
        return OpenAICompatEngine(cfg)
    if kind in ("dashscope", "aliyun", "bailian", "qwen"):
        from .dashscope import DashScopeEngine
        return DashScopeEngine(cfg)
    if kind in ("volc", "doubao", "volcengine"):
        from .volc import VolcEngine
        return VolcEngine(cfg)
    from .. import catalog
    raise DyxError(f"未知 ASR 引擎: {kind}", code="bad_engine",
                   hint="可选：" + " / ".join(catalog.BY_ID))


# ------------------------------------------------------- 账号/余额类错误识别
FATAL_MARKERS = (
    "arrearage", "access denied", "good standing", "invalidapikey",
    "invalid api key", "unauthorized", "authentication", "insufficient",
    "quota", "欠费", "余额不足", "未授权", "无权限",
)


def is_fatal_asr_error(err: object) -> bool:
    """账号或余额问题：再试下去每条都会一样失败，应当整轮暂停。"""
    code = str(getattr(err, "code", "") or "")
    if code in ("asr_account", "asr_auth"):
        return True
    text = str(err or "").lower()
    return any(m in text for m in FATAL_MARKERS)
