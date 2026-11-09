"""SenseVoice (sherpa-onnx, int8, CPU) — best accuracy/speed for Chinese on CPU.

SenseVoice itself emits no timestamps, so sentences are delimited with ffmpeg's
silence detector and each chunk is decoded independently.  That yields
sentence-level timing without a second model.
"""
from __future__ import annotations

import os
import re
import wave

import numpy as np

from ..util import ShortVideoError, find_ffmpeg, log, run
from . import merge_short, read_wav_f32

TAG_RE = re.compile(r"<\|[^|]*\|>")
PUNCT_END = tuple("。！？!?…；;：:")


def detect_silences(ffmpeg: str, wav: str, noise_db: int = -32,
                    min_silence: float = 0.35) -> list[tuple[float, float]]:
    p = run([ffmpeg, "-v", "info", "-i", wav, "-af",
             f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
            timeout=900)
    text = (p.stderr or "") + (p.stdout or "")
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[\d.]+)", text)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is not None and e > max(s, 0):
            out.append((max(s, 0.0), e))
    return out


def cut_points(silences: list[tuple[float, float]], duration: float,
               max_len: float = 28.0) -> list[tuple[float, float]]:
    bounds = [0.0]
    for s, e in silences:
        mid = (s + e) / 2.0
        if mid - bounds[-1] > max_len:
            # force intermediate cuts
            while mid - bounds[-1] > max_len:
                bounds.append(round(bounds[-1] + max_len, 3))
        bounds.append(round(mid, 3))
    bounds.append(round(duration, 3))
    segs = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a >= 0.25:
            segs.append({"start": float(round(a, 2)), "end": float(round(b, 2)), "text": ""})
    return segs


class SenseVoiceEngine:
    remote = False
    name = "sensevoice"

    def __init__(self, cfg):
        self.cfg = cfg
        self.model_name = "sensevoice-int8"
        self.model_dir = cfg.get("asr.sensevoice.modelDir")
        self.threads = int(cfg.get("asr.sensevoice.threads") or 4)
        self.use_itn = bool(cfg.get("asr.sensevoice.useItn", True))
        self._rec = None
        self.ffmpeg, _ = find_ffmpeg(cfg.get("media.ffmpegPath"))

    # ------------------------------------------------------------------ model
    def _recognizer(self):
        if self._rec is not None:
            return self._rec
        d = self.model_dir
        model = os.path.join(d, "model.int8.onnx")
        if not os.path.exists(model):
            model = os.path.join(d, "model.onnx")
        tokens = os.path.join(d, "tokens.txt")
        if not (os.path.exists(model) and os.path.exists(tokens)):
            raise ShortVideoError(
                f"SenseVoice 模型缺失: {d}",
                code="model_missing",
                hint="运行 sv_settings action=install_model engine=sensevoice 自动下载，"
                     "或改用 asr.engine=faster-whisper / openai。",
            )
        import sherpa_onnx
        log(f"加载 SenseVoice: {os.path.basename(model)} threads={self.threads}")
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model, tokens=tokens, num_threads=self.threads,
            use_itn=self.use_itn, language="auto", debug=False)
        return self._rec

    # -------------------------------------------------------------- transcribe
    def transcribe(self, wav_path: str, language: str = "zh") -> dict:
        rec = self._recognizer()
        audio, sr = read_wav_f32(wav_path)
        duration = len(audio) / float(sr)
        silences = detect_silences(self.ffmpeg, wav_path)
        segs = cut_points(silences, duration)

        with wave.open(wav_path, "rb") as w:
            nch = w.getnchannels()
        if nch > 1:
            raise ShortVideoError("需要单声道 wav", code="bad_wav")

        for s in segs:
            i0 = int(s["start"] * sr)
            i1 = int(s["end"] * sr)
            chunk = audio[i0:i1]
            if chunk.size < sr * 0.15:
                continue
            stream = rec.create_stream()
            stream.accept_waveform(sr, chunk)
            rec.decode_stream(stream)
            text = TAG_RE.sub("", stream.result.text or "").strip()
            s["text"] = text

        segs = [s for s in segs if s["text"]]
        segs = merge_short(segs, min_len=0.9, max_len=30.0)
        full = "".join(s["text"] if s["text"][-1:] in PUNCT_END else s["text"] + "。"
                       for s in segs)
        return {
            "engine": self.name,
            "model": self.model_name,
            "language": language or "auto",
            "duration": round(duration, 2),
            "segments": segs,
            "text": full,
        }
