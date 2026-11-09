# -*- coding: utf-8 -*-
"""阿里云百炼（DashScope）语音识别：原生 multimodal-generation，带词级时间戳。

实测结论（2026-11）：
- OpenAI 兼容端点 `/compatible-mode/v1/audio/transcriptions` 在本账号上会被服务端直接
  掐断连接（ConnectionReset），不可用；只能走原生 multimodal-generation。
- 原生端点返回 `output.sentence.words[]`，每词自带 begin_time/end_time（毫秒）与
  punctuation 字段，可据此重建句级时间戳。
- `qwen3-asr-flash` 只回文本（无时间戳）；带时间戳的是 `qwen-audio-3.0-asr-flash`
  与 `fun-asr-flash-*`。
- 音频按 base64 data URL 内联，长音频要本地切块后分段请求再补时间偏移。
"""
from __future__ import annotations

import base64
import time
import uuid
import json
import os
import urllib.error
import urllib.request

from ..util import ShortVideoError, find_ffmpeg, log, run

ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
CHUNK_SECONDS = 180          # 16k 单声道 s16 ≈ 5.8MB/180s，base64 后 ≈ 7.7MB，留足余量
TERMINATORS = "。！？；!?;…"


def _words_to_segments(words: list[dict], offset: float) -> list[dict]:
    """把词级结果按标点重建成句级片段。"""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if not (w.get("text") or "").strip():
            continue
        cur.append(w)
        punct = w.get("punctuation") or ""
        if any(ch in punct for ch in TERMINATORS):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    out: list[dict] = []
    for g in groups:
        text = "".join((w.get("text") or "") + (w.get("punctuation") or "") for w in g).strip()
        if not text:
            continue
        start = float(g[0].get("begin_time") or 0) / 1000.0
        end = float(g[-1].get("end_time") or 0) / 1000.0
        out.append({
            "start": round(start + offset, 2),
            "end": round(max(end, start) + offset, 2),
            "text": text,
        })
    return out


class DashScopeEngine:
    remote = True
    name = "dashscope"

    def __init__(self, cfg):
        o = cfg.data["asr"].get("dashscope") or {}
        self.cfg = cfg
        self.model_name = o.get("model") or "qwen-audio-3.1-asr-flash"
        self.key = (o.get("apiKey") or os.environ.get("SV_DASHSCOPE_KEY")
                    or os.environ.get("DASHSCOPE_API_KEY") or "")
        self.timeout = int(o.get("timeoutSec") or 600)
        self.chunk_sec = int(o.get("chunkSec") or CHUNK_SECONDS)
        if not self.key:
            raise ShortVideoError("未配置百炼 API Key", code="asr_unconfigured",
                           hint="运行 sv_settings set asr.dashscope.apiKey <你的Key>，"
                                "或设置环境变量 SV_DASHSCOPE_KEY")

    # ------------------------------------------------------------------ chunks
    def _chunks(self, wav_path: str) -> list[tuple[str, float]]:
        ffmpeg, _ = find_ffmpeg(self.cfg.get("media.ffmpegPath"))
        tmpdir = os.path.dirname(wav_path)
        tag = uuid.uuid4().hex[:8]
        out: list[tuple[str, float]] = []
        offset = 0.0
        for i in range(400):
            dest = os.path.join(tmpdir, f".ds_{tag}_{i:03d}.wav")
            p = run([ffmpeg, "-y", "-v", "error", "-ss", str(offset), "-t", str(self.chunk_sec),
                     "-i", wav_path, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dest],
                    timeout=600)
            if p.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 2048:
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                break
            out.append((dest, offset))
            offset += self.chunk_sec
        return out or [(wav_path, 0.0)]

    # ----------------------------------------------------------------- request
    def _call(self, path: str) -> dict:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        payload = {
            "model": self.model_name,
            "input": {"messages": [{"role": "user", "content": [
                {"audio": "data:audio/wav;base64," + b64}]}]},
            "parameters": {"format": "wav", "sample_rate": 16000},
        }
        data = json.dumps(payload).encode()
        headers = {"Authorization": "Bearer " + self.key,
                   "Content-Type": "application/json"}
        last_error = ""
        for attempt in range(4):
            req = urllib.request.Request(ENDPOINT, data=data, headers=headers,
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                # 该段没有可识别语音时百炼回 400 ASR_RESPONSE_HAVE_NO_WORDS——
                # 这不是故障，纯音乐/无人声就是空结果，交给上层记为「无语音」。
                if "HAVE_NO_WORDS" in detail:
                    return {"output": {"sentences": []}, "_noSpeech": True}
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    last_error = f"{e.code}: {detail[:120]}"
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                code = "asr_auth" if e.code in (401, 403) else (
                    "asr_rate_limited" if e.code == 429 else "asr_http_error")
                raise ShortVideoError(f"百炼 ASR 返回 {e.code}: {detail}", code=code) from e
            except Exception as e:  # noqa: BLE001
                if attempt < 3:
                    last_error = str(e)[:160]
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise ShortVideoError(f"百炼 ASR 调用失败: {e}", code="asr_call_failed") from e
        raise ShortVideoError(f"百炼 ASR 反复失败: {last_error}", code="asr_call_failed")

    # -------------------------------------------------------------- transcribe
    def transcribe(self, wav_path: str, language: str = "zh") -> dict:
        segments: list[dict] = []
        total = 0.0
        chunks = self._chunks(wav_path)
        for path, offset in chunks:
            log(f"  百炼转写 {os.path.basename(path)}（偏移 {offset:.0f}s）")
            obj = self._call(path)
            out = obj.get("output") or {}
            inner = out.get("output") if isinstance(out.get("output"), dict) else out
            sents = inner.get("sentences") or inner.get("sentence_list")
            if isinstance(sents, list) and sents:
                for s in sents:
                    text = (s.get("text") or "").strip()
                    if not text:
                        continue
                    start = float(s.get("begin_time") or 0) / 1000.0
                    end = float(s.get("end_time") or start * 1000) / 1000.0
                    segments.append({"start": round(start + offset, 2),
                                     "end": round(max(end, start) + offset, 2), "text": text})
            else:
                sent = inner.get("sentence") or {}
                words = sent.get("words") or []
                if words:
                    segments.extend(_words_to_segments(words, offset))
                elif (inner.get("text") or sent.get("text") or "").strip():
                    segments.append({
                        "start": round(offset, 2),
                        "end": round(offset + float((obj.get("usage") or {}).get("duration") or 0), 2),
                        "text": (inner.get("text") or sent.get("text")).strip(),
                    })
            usage_sec = float((obj.get("usage") or {}).get("duration") or 0)
            total = max(total, offset + usage_sec)
            if path != wav_path:
                try:
                    os.remove(path)
                except OSError:
                    pass

        segments = [s for s in segments if s["text"]]
        if segments:
            total = max(total, segments[-1]["end"])
        return {
            "engine": self.name,
            "model": self.model_name,
            "language": language or "auto",
            "duration": round(total, 2),
            "segments": segments,
            "text": "".join(s["text"] for s in segments),
        }
