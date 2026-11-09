"""火山引擎（豆包）录音文件识别 —— 中文最强的云端方案。

三种档位（`asr.volc.tier`）：
  express  极速版 volc.bigasr.auc_turbo  单次 ≤2h，一次请求直接返回结果（默认）
  standard 标准版 volc.seedasr.auc       submit + query 轮询
  offpeak  闲时版 volc.bigasr.auc_idle   排队 24h 内出结果，最便宜

鉴权：只需要控制台的一个 API Key（`asr.volc.apiKey`），放在 `x-api-key` 头里。

⚠ 这个接口只接受**公网可访问的音频 URL**。本地文件有两种走法：
  1) 配好火山 TOS（`asr.volc.tosBucket` + AK/SK），本引擎自动签名直传再转录（推荐）
  2) 自己在 `asr.volc.audioUrl` 里填一个已上传好的公网地址
两条都没有时会给出明确报错，不会静默失败。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from ..util import ShortVideoError, log

API_BASE = "https://openspeech.bytedance.com/api/v3/auc/bigmodel"

TIERS: dict[str, dict] = {
    "express": {"resourceId": "volc.bigasr.auc_turbo",
                "flash": f"{API_BASE}/recognize/flash"},
    "standard": {"resourceId": "volc.seedasr.auc",
                 "submit": f"{API_BASE}/submit", "query": f"{API_BASE}/query"},
    "offpeak": {"resourceId": "volc.bigasr.auc_idle",
                "submit": f"{API_BASE}/idle/submit", "query": f"{API_BASE}/idle/query"},
}

OK = "20000000"
PENDING = ("20000001", "20000002")
SILENT = "20000003"

ERR_HINTS = {
    "45000006": "ASR 服务下载不到你给的音频（Invalid audio URI）。若是 TOS 签名直传，"
                "请确认这个 AK/SK 对 bucket 有**读**权限（桶策略选『文件夹读写』，别加 IP 限制）。",
    "45000001": "请求参数不合法：检查 audio.format 与文件真实格式是否一致。",
    "45000002": "TOS 上传的对象不存在或已过期：签名 URL 有效期默认 3600s。",
}


# --------------------------------------------------------------- TOS 签名直传
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def presign_tos(method: str, url: str, ak: str, sk: str, region: str,
                expires: int = 3600) -> str:
    """火山 TOS V4 预签名 URL（查询串鉴权）。"""
    p = urlparse(url)
    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{date_stamp}/{region}/tos/request"
    signed_headers = "host"
    canonical_headers = f"host:{p.hostname}\n"
    params = {
        "X-Tos-Algorithm": "TOS4-HMAC-SHA256",
        "X-Tos-Credential": f"{ak}/{scope}",
        "X-Tos-Date": amz_date,
        "X-Tos-Expires": str(expires),
        "X-Tos-SignedHeaders": signed_headers,
    }
    canonical_qs = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items()))
    canonical_request = "\n".join([
        method, quote(p.path, safe="/"), canonical_qs, canonical_headers,
        signed_headers, "UNSIGNED-PAYLOAD",
    ])
    string_to_sign = "\n".join([
        "TOS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signing_key = _sign(_sign(_sign(_sign(sk.encode("utf-8"), date_stamp), region),
                              "tos"), "request")
    sig = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{p.scheme}://{p.hostname}{p.path}?{canonical_qs}&X-Tos-Signature={sig}"


MIME = {"wav": "audio/wav", "mp3": "audio/mpeg", "mp4": "audio/mp4",
        "m4a": "audio/mp4", "ogg": "audio/ogg", "flac": "audio/flac"}


class TOSUploader:
    def __init__(self, bucket: str, region: str, ak: str, sk: str):
        self.bucket = bucket
        self.region = region
        self.ak = ak
        self.sk = sk

    def _url(self, key: str) -> str:
        return f"https://{self.bucket}.tos-{self.region}.volces.com/{key}"

    def upload(self, path: str, fmt: str) -> str:
        key = f"shortvideo-asr/{uuid.uuid4()}.{fmt}"
        raw = self._url(key)
        put_url = presign_tos("PUT", raw, self.ak, self.sk, self.region, expires=600)
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            req = urllib.request.Request(
                put_url, data=f.read(),
                headers={"Content-Type": MIME.get(fmt, "application/octet-stream")},
                method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    if r.status not in (200, 201):
                        raise ShortVideoError(f"TOS 上传失败 HTTP {r.status}", code="tos_upload_failed")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                raise ShortVideoError(f"TOS 上传失败 HTTP {e.code}: {body}",
                               code="tos_upload_failed",
                               hint="检查 asr.volc.tosBucket / tosRegion / accessKeyId / "
                                    "secretAccessKey，以及桶策略是否允许该 AK 写对象。") from e
            except Exception as e:  # noqa: BLE001
                raise ShortVideoError(f"TOS 上传失败: {e}", code="tos_upload_failed") from e
        log(f"  已上传 {size / 1048576:.1f} MB 到 TOS（{key}）")
        return presign_tos("GET", raw, self.ak, self.sk, self.region, expires=3600)


# --------------------------------------------------------------- 引擎
class VolcEngine:
    remote = True
    name = "volc"

    def __init__(self, cfg):
        self.cfg = cfg
        v = (cfg.get("asr.volc") or {})
        self.key = v.get("apiKey") or os.environ.get("SV_VOLC_KEY") or ""
        self.tier = (v.get("tier") or "express").lower()
        self.speakers = bool(v.get("speakers", False))
        self.timeout = int(v.get("timeoutSec") or 900)
        self.poll = int(v.get("pollSec") or 3)
        self.audio_url = v.get("audioUrl") or ""
        self.model_name = f"doubao-{self.tier}"
        self.hot_words = v.get("hotWords") or []
        self.bucket = v.get("tosBucket") or ""
        self.region = v.get("tosRegion") or "cn-beijing"
        self.ak = (v.get("accessKeyId") or os.environ.get("VOLCENGINE_ACCESS_KEY_ID") or "")
        self.sk = (v.get("secretAccessKey")
                   or os.environ.get("VOLCENGINE_SECRET_ACCESS_KEY") or "")
        if not self.key:
            raise ShortVideoError(
                "未配置豆包（火山）语音识别的 API Key", code="asr_unconfigured",
                hint="到 https://console.volcengine.com/speech/new/ 创建应用并复制 API Key，"
                     "然后运行：sv_settings set asr.volc.apiKey <你的key>")
        if self.tier not in TIERS:
            raise ShortVideoError(f"未知豆包档位: {self.tier}", code="bad_engine",
                           hint="可选：express / standard / offpeak")

    # ------------------------------------------------------------ audio
    def _audio_url(self, wav_path: str) -> tuple[str, str]:
        fmt = os.path.splitext(wav_path)[1].lstrip(".").lower() or "wav"
        if self.audio_url:
            return self.audio_url, fmt
        if self.bucket and self.ak and self.sk:
            up = TOSUploader(self.bucket, self.region, self.ak, self.sk)
            return up.upload(wav_path, fmt), fmt
        raise ShortVideoError(
            "豆包录音文件识别只接受公网 URL，本机音频无法直接提交", code="asr_needs_url",
            hint="二选一：① sv_settings set asr.volc.tosBucket <桶名> 并配好 "
                 "accessKeyId / secretAccessKey（引擎会自动直传）；"
                 "② 自己上传后 sv_settings set asr.volc.audioUrl <公网地址>")

    # ------------------------------------------------------------ HTTP
    def _headers(self, request_id: str, tier: dict, sequence: bool) -> dict:
        h = {
            "Content-Type": "application/json",
            "x-api-key": self.key,
            "X-Api-Resource-Id": tier["resourceId"],
            "X-Api-Request-Id": request_id,
        }
        if sequence:
            h["X-Api-Sequence"] = "-1"
        return h

    def _post(self, url: str, headers: dict, body: dict) -> tuple[str, str, dict]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status = r.headers.get("X-Api-Status-Code", "")
                message = r.headers.get("X-Api-Message", "")
                payload = json.loads(r.read().decode("utf-8", "replace") or "{}")
                return status, message, payload
        except urllib.error.HTTPError as e:
            status = e.headers.get("X-Api-Status-Code", "")
            message = e.headers.get("X-Api-Message", "") or e.read().decode("utf-8", "replace")[:300]
            self._raise(status or str(e.code), message)
        except Exception as e:  # noqa: BLE001
            raise ShortVideoError(f"豆包 ASR 请求失败: {e}", code="asr_call_failed") from e

    def _raise(self, status: str, message: str):
        hint = ERR_HINTS.get(status, "")
        raise ShortVideoError(f"豆包 ASR 返回 {status}: {message}", code="asr_http_error",
                       hint=hint or None)

    def _body(self, audio_url: str, fmt: str) -> dict:
        req = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
            "enable_speaker_info": self.speakers,
        }
        if self.hot_words:
            # 上下文化热词：把术语表直接喂给模型，显著减少同音错字
            req["corpus"] = {"context": json.dumps(
                {"hotwords": [{"word": w} for w in self.hot_words]}, ensure_ascii=False)}
        return {"user": {"uid": "dyx"}, "audio": {"url": audio_url, "format": fmt},
                "request": req}

    # ------------------------------------------------------------ flows
    def _flash(self, audio_url: str, fmt: str) -> dict:
        tier = TIERS["express"]
        rid = str(uuid.uuid4())
        status, msg, payload = self._post(tier["flash"],
                                          self._headers(rid, tier, sequence=False),
                                          self._body(audio_url, fmt))
        if status == SILENT:
            return {"result": {"text": "", "utterances": []}}
        if status != OK:
            self._raise(status, msg)
        return payload

    def _poll_flow(self, audio_url: str, fmt: str) -> dict:
        tier = TIERS[self.tier]
        rid = str(uuid.uuid4())
        status, msg, _ = self._post(tier["submit"],
                                    self._headers(rid, tier, sequence=True),
                                    self._body(audio_url, fmt))
        if status != OK:
            self._raise(status, msg)
        log(f"  已提交豆包转写任务（{self.tier}），等待结果…")
        waited = 0
        while waited < self.timeout:
            time.sleep(self.poll)
            waited += self.poll
            status, msg, payload = self._post(tier["query"],
                                             self._headers(rid, tier, sequence=False), {})
            if status == OK:
                return payload
            if status in PENDING:
                continue
            if status == SILENT:
                return {"result": {"text": "", "utterances": []}}
            self._raise(status, msg)
        raise ShortVideoError(f"豆包转写超时（{self.timeout}s）", code="asr_timeout")

    # ------------------------------------------------------------ contract
    def transcribe(self, wav_path: str, language: str = "zh") -> dict:
        audio_url, fmt = self._audio_url(wav_path)
        data = self._flash(audio_url, fmt) if self.tier == "express" \
            else self._poll_flow(audio_url, fmt)
        result = data.get("result") or {}
        segments: list[dict] = []
        for u in result.get("utterances") or []:
            text = (u.get("text") or "").strip()
            if not text:
                continue
            st = u.get("start_time")
            en = u.get("end_time")
            if st is None or en is None:
                continue
            segments.append({"start": round(float(st) / 1000.0, 2),
                             "end": round(float(en) / 1000.0, 2),
                             "text": text})
        if not segments and result.get("text"):
            segments = [{"start": 0.0, "end": 0.0, "text": result["text"].strip()}]
        duration = segments[-1]["end"] if segments else 0.0
        return {
            "engine": self.name,
            "model": self.model_name,
            "language": language or "auto",
            "duration": round(duration, 2),
            "segments": segments,
            "text": result.get("text") or "".join(s["text"] for s in segments),
        }
