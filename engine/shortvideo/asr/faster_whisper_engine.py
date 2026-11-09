"""faster-whisper on CPU (int8) — multilingual fallback, no GPU required."""
from __future__ import annotations

import os

from ..util import ShortVideoError, log
from . import read_wav_f32


class FasterWhisperEngine:
    remote = False
    name = "faster-whisper"

    def __init__(self, cfg):
        self.cfg = cfg
        self.model_name = cfg.get("asr.fasterWhisper.model") or "small"
        self.compute = cfg.get("asr.fasterWhisper.computeType") or "int8"
        self.threads = int(cfg.get("asr.fasterWhisper.cpuThreads") or 0) or os.cpu_count() or 4
        self.beam = int(cfg.get("asr.fasterWhisper.beamSize") or 5)
        self.vad = bool(cfg.get("asr.fasterWhisper.vad", True))
        self._model = None

    def _m(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise ShortVideoError("未安装 faster-whisper",
                           code="dep_missing",
                           hint="pip install faster-whisper 'av>=14'") from e
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        log(f"加载 faster-whisper {self.model_name} ({self.compute}, {self.threads} threads)")
        self._model = WhisperModel(self.model_name, device="cpu",
                                   compute_type=self.compute, cpu_threads=self.threads)
        return self._model

    def transcribe(self, wav_path: str, language: str = "zh") -> dict:
        audio, sr = read_wav_f32(wav_path)
        assert sr == 16000
        model = self._m()
        segs_iter, info = model.transcribe(
            audio,
            language=None if language in ("", "auto") else language,
            beam_size=self.beam,
            vad_filter=self.vad,
            vad_parameters={"min_silence_duration_ms": 500} if self.vad else None,
            initial_prompt="以下是普通话视频内容，请使用简体中文并加标点。"
            if (language in ("zh", "", "auto")) else None,
        )
        segments = [{"start": round(s.start, 2), "end": round(s.end, 2),
                     "text": (s.text or "").strip()}
                    for s in segs_iter if (s.text or "").strip()]
        return {
            "engine": self.name,
            "model": self.model_name,
            "language": info.language or language or "auto",
            "duration": round(info.duration or (len(audio) / sr), 2),
            "segments": segments,
            "text": "".join(s["text"] + ("\n" if s["text"][-1:] in "。！？!?" else "")
                            for s in segments),
        }
