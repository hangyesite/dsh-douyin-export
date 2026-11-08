# -*- coding: utf-8 -*-
"""转写引擎目录：把「有哪些引擎、跑在哪、多少钱、要不要额外准备」摊开给使用者自己挑。

设计原则（重要）：插件**不替使用者做选择**。所有引擎一视同仁地暴露，
每一条都写清成本与前置条件；默认值只是「不配任何东西也能跑」的那个（本地，0 元），
不是推荐结论。价格来自各家官方计费页 + 本项目实测口径，见 PRICE_SOURCES。
"""
from __future__ import annotations

# 单位统一为「元 / 音频小时」，方便横向比较。None = 取决端点或用量，见 note。
ENGINES: list[dict] = [
    {
        "id": "sensevoice",
        "label": "SenseVoice（本地，sherpa-onnx）",
        "location": "local",
        "yuanPerHour": 0.0,
        "freeQuota": "不限（本机算力）",
        "timestamps": "句级（ffmpeg 静音边界切分）",
        "needs": ["首次需下载模型 model.int8.onnx + tokens.txt（约 228MB）"],
        "note": "CPU int8 可跑，不占显卡；自带标点；475 秒音频约 4.5 秒转完。",
    },
    {
        "id": "faster-whisper",
        "label": "faster-whisper（本地，whisper.cpp/CTranslate2）",
        "location": "local",
        "yuanPerHour": 0.0,
        "freeQuota": "不限（本机算力）",
        "timestamps": "词级/句级（模型自带）",
        "needs": ["选一个模型（tiny…large-v3）", "PyAV 如需读文件"],
        "note": "中文准确率弱于 SenseVoice（base 档实测同音错较多）；large-v3 在纯 CPU 上约 1 倍实时。",
    },
    {
        "id": "openai",
        "label": "OpenAI 兼容端点（任何 /v1/audio/transcriptions）",
        "location": "cloud",
        "yuanPerHour": None,
        "freeQuota": "取决于所选服务商",
        "timestamps": "取决于服务商（多数只回整段文本，无时间戳）",
        "needs": ["baseUrl", "apiKey", "model"],
        "note": "这一档是「通用适配器」：OpenAI 官方、硅基流动、Groq、自建网关都能接。"
                "实测硅基流动 FunAudioLLM/SenseVoiceSmall 限免、但不返回时间戳，正文里会混入日文假名与 emoji。",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼 DashScope（原生 multimodal，词级时间戳）",
        "location": "cloud",
        "yuanPerHour": 0.06,
        "freeQuota": "按秒计费模型 36,000 秒（10 小时）/90 天；token 计费模型 100 万 token（≈22 小时音频）",
        "timestamps": "词级 begin_time/end_time（毫秒），可重建句级",
        "needs": ["百炼 API Key"],
        "note": "默认模型 qwen-audio-3.1-asr-flash 按 token 计费：实测 12.5 输入 token/音频秒 + 2.3 输出 token/音频秒，"
                "折合约 0.06 元/小时——是全表里最便宜的云端档。"
                "换成 qwen-audio-3.0-asr-flash / fun-asr-flash-* 则按 0.00022 元/秒（≈0.79 元/小时）。",
    },
    {
        "id": "volc",
        "label": "火山引擎豆包语音（openspeech v3）",
        "location": "cloud",
        "yuanPerHour": 0.8,
        "freeQuota": "20 小时/半年（需在控制台手动点「试用」领取）",
        "timestamps": "句级 utterances（start_time/end_time 毫秒）",
        "needs": ["豆包语音 API Key", "公网可访问的音频 URL（或配 TOS 直传）"],
        "note": "默认走「录音文件识别模型 2.0 标准版」volc.seedasr.auc：0.8 元/小时（资源包 30 小时 23 元 ≈0.77 元/小时）。"
                "旧版极速版 4.5 元/小时；流式 2.0 小时版 1 元/小时；并发版按路数买（500 元/路/月）。",
    },
]

PRICE_SOURCES = [
    "阿里百炼模型价格页（华北2 北京，后付费原价）: https://help.aliyun.com/zh/model-studio/model-pricing",
    "火山引擎豆包语音计费说明: https://docs.volcengine.com/docs/doubao-voice/billing-instructions",
    "本机实测 token 口径: .ref/probe_price.py、.ref/probe_31.py（60 秒中文音频，qwen-audio-3.1-asr-flash 用 750 输入 + 137 输出 token）",
]

BY_ID = {e["id"]: e for e in ENGINES}
ALIASES = {
    "aliyun": "dashscope",
    "bailian": "dashscope",
    "qwen": "dashscope",
    "doubao": "volc",
    "volcengine": "volc",
    "whisper": "faster-whisper",
    "local": "sensevoice",
}


def resolve(engine_id: str | None) -> str:
    """把别名归一到目录里的规范 id；不认识的返回原样（交给引擎层报错）。"""
    if not engine_id:
        return "sensevoice"
    key = engine_id.strip().lower()
    key = ALIASES.get(key, key)
    return key if key in BY_ID else engine_id


def engines() -> list[dict]:
    return [dict(e) for e in ENGINES]


def estimate(engine_id: str, seconds: float) -> dict:
    """按目录价估算一段音频的成本；未知价返回 priced=False 而不是瞎猜。"""
    eng = BY_ID.get(resolve(engine_id))
    if not eng or eng.get("yuanPerHour") is None:
        return {"engine": engine_id, "priced": False,
                "reason": "该引擎价格取决于所选服务商，无法统一估算"}
    hours = round(max(0.0, float(seconds)) / 3600.0, 3)
    return {
        "engine": eng["id"],
        "priced": True,
        "hours": hours,
        "yuanPerHour": eng["yuanPerHour"],
        "yuan": round(hours * eng["yuanPerHour"], 3),
        "freeQuota": eng["freeQuota"],
    }


def summary_rows(seconds: float | None = None) -> list[dict]:
    """给 CLI/工具输出用的一行一引擎表。"""
    rows = []
    for e in ENGINES:
        row = {
            "id": e["id"],
            "label": e["label"],
            "location": e["location"],
            "yuanPerHour": e["yuanPerHour"],
            "freeQuota": e["freeQuota"],
            "timestamps": e["timestamps"],
        }
        if seconds is not None and e["yuanPerHour"] is not None:
            row["yuanForThisRun"] = round(max(0.0, float(seconds)) / 3600.0 * e["yuanPerHour"], 3)
        rows.append(row)
    return rows
