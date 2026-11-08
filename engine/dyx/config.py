"""Configuration: layered defaults <- config.json <- environment."""
from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_HOME = os.path.join(os.path.expanduser("~"), ".dsh-douyin")

DEFAULTS: dict[str, Any] = {
    # where the library (db, chrome profile, models) lives
    "home": DEFAULT_HOME,
    # where exported media and transcripts go
    "outputDir": os.path.join(os.path.expanduser("~"), "DouyinExport"),
    # browser
    "browser": {
        "channel": "chrome",          # chrome | msedge | chromium
        "headless": False,
            "showWindow": False,            # headless breaks Douyin's anti-bot signing
        "profileDir": "",             # default <home>/chrome-profile
        "warmup": True,               # visit /hot first to obtain risk-control cookies
        "navTimeoutMs": 60000,
        "settleMs": 9000,
    },
    # fetching
    "fetch": {
        "pageSize": 18,
        "maxScrolls": 400,
        "requestDelayMs": 900,
    },
    # media
    "media": {
        "ffmpegPath": "",
        "preferRatio": "1080p",
        "keepAudio": True,
    },
    # speech to text
    "asr": {
        "engine": "sensevoice",
            "replacements": [],
            # 云端 ASR 并发路数（本地模型强制串行；云端 4 路通常比串行快 3 倍以上）
            "concurrency": 4,       # sensevoice | faster-whisper | openai
        "language": "zh",
        "workers": 1,
            # 熔断：同一种错连续这么多条就整体暂停（0 = 关闭）；
            # 账号/余额类错误不等计数，第一条就暂停。
            "breakerConsecutive": 5,
        "sensevoice": {
            "modelDir": "",           # default <home>/models/sensevoice
            "threads": 4,
            "useItn": True,
        },
        "fasterWhisper": {
            "model": "small",
            "computeType": "int8",
            "cpuThreads": 0,          # 0 = cpu_count
            "beamSize": 5,
            "vad": True,
        },
        "openai": {
            "baseUrl": "https://api.openai.com/v1",
            "apiKey": "",
            "model": "whisper-1",
            "timeoutSec": 600,
        },
        # 阿里云百炼（DashScope）原生识别，带词级→句级时间戳
        "dashscope": {
            "apiKey": "",
            # 3.1 按 token 计费（≈0.06 元/小时）；3.0 按秒计费（≈0.79 元/小时），
            # 两者都返回词级时间戳，换模型只需改这一个字段。
            "model": "qwen-audio-3.1-asr-flash",
            "chunkSec": 180,
            "timeoutSec": 600,
        },
        # 火山引擎豆包（需公网可访问的音频 URL 或 TOS 直传配置）
        "volc": {
            "apiKey": "",
            "tier": "express",
            "speakers": False,
            "timeoutSec": 900,
            "pollSec": 3,
            "audioUrl": "",
            "hotWords": [],
            "tosBucket": "",
            "tosRegion": "cn-beijing",
            "accessKeyId": "",
            "secretAccessKey": "",
        },
    },
    # AI 校对（跟语音识别是两套东西：这里要的是一个 chat 模型）
    # 留空 apiKey 就等于不启用；进来的是「错→对」术语表，不重写正文。
    "polish": {
        # 任意 OpenAI 兼容的 chat 端点；插件不预置、不推荐任何厂商
        "mode": "host",
                "provider": "",
                "baseUrl": "",
        "apiKey": "",
        "model": "",
        "concurrency": 2,
        "sampleSize": 20,          # 挖术语表时抽多少条作品
        "maxCharsPerCall": 6000,   # 单次送进去的字数上限（超了分块）
        "minHits": 2,              # 至少几条视频都指出同一错词，才算「值得采纳」的候选
        "timeoutSec": 120,
    },
    # export
    "export": {
        "formats": ["md"],
        # 时间戳默认不带（导出高级设置里勾选才加 [mm:ss]）；JSON 永远保留
        "timestampStyle": "none",
        # 兼容老开关（新逻辑看 statsFields）
        "includeStats": False,
        # DOCX 中文字体（Word 按名字引用本机字体）
        "docxFont": "微软雅黑",
        # 正文里是否写抖音原视频链接（front-matter 里一直有 url）
        "includeLink": False,
        # 正文里是否写发布日期（front-matter 里一直有）
        "includePublished": False,
        # 合集排序：desc=最新在前（默认） / asc=最老在前
        "mergeOrder": "desc",
        # 导出哪几项互动数据：digg_count / comment_count / share_count / collect_count
        # 空列表=不导（只导数字，不导评论内容）
        "statsFields": [],    # clock | srt | none
        "paragraphGapSec": 2.0,
    },
}

_CONFIG_KEYS = set(DEFAULTS.keys())


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, home: str | None = None):
        env_home = os.environ.get("DY_DOUYIN_HOME")
        self.home = os.path.abspath(
            os.path.expanduser(home or env_home or DEFAULT_HOME))
        self.path = os.path.join(self.home, "config.json")
        self.data: dict[str, Any] = _deep_merge(DEFAULTS, {})
        self.data["home"] = self.home
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data = _deep_merge(self.data, json.load(f))
        self._apply_env()
        self._normalize()

    def _apply_env(self) -> None:
        if os.environ.get("DY_DOUYIN_OUTPUT"):
            self.data["outputDir"] = os.environ["DY_DOUYIN_OUTPUT"]
        if os.environ.get("DY_OPENAI_BASE"):
            self.data["asr"]["openai"]["baseUrl"] = os.environ["DY_OPENAI_BASE"]
        if os.environ.get("DY_OPENAI_KEY"):
            self.data["asr"]["openai"]["apiKey"] = os.environ["DY_OPENAI_KEY"]
        if os.environ.get("DY_OPENAI_MODEL"):
            self.data["asr"]["openai"]["model"] = os.environ["DY_OPENAI_MODEL"]
        if os.environ.get("DY_DASHSCOPE_KEY"):
            self.data["asr"]["dashscope"]["apiKey"] = os.environ["DY_DASHSCOPE_KEY"]
        if os.environ.get("DASHSCOPE_API_KEY"):
            self.data["asr"]["dashscope"]["apiKey"] = os.environ["DASHSCOPE_API_KEY"]
        if os.environ.get("DY_VOLC_KEY"):
            self.data["asr"]["volc"]["apiKey"] = os.environ["DY_VOLC_KEY"]
        if os.environ.get("VOLCENGINE_ACCESS_KEY_ID"):
            self.data["asr"]["volc"]["accessKeyId"] = os.environ["VOLCENGINE_ACCESS_KEY_ID"]
        if os.environ.get("VOLCENGINE_SECRET_ACCESS_KEY"):
            self.data["asr"]["volc"]["secretAccessKey"] = os.environ["VOLCENGINE_SECRET_ACCESS_KEY"]

    def _normalize(self) -> None:
        self.data["home"] = self.home
        self.data["outputDir"] = os.path.abspath(os.path.expanduser(self.data["outputDir"]))
        b = self.data["browser"]
        if not b.get("profileDir"):
            b["profileDir"] = os.path.join(self.home, "chrome-profile")
        b["profileDir"] = os.path.abspath(os.path.expanduser(b["profileDir"]))
        sv = self.data["asr"]["sensevoice"]
        if not sv.get("modelDir"):
            sv["modelDir"] = os.path.join(self.home, "models",
                                          "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")

    # ------------------------------------------------------------ access
    def get(self, dotted: str, default=None):
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        cur = self.data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        self._normalize()

    def save(self) -> None:
        os.makedirs(self.home, exist_ok=True)
        persist = json.loads(json.dumps(self.data))
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(persist, f, ensure_ascii=False, indent=1)

    def public(self) -> dict:
        d = json.loads(json.dumps(self.data))
        def _mask(s: str) -> str:
            return (s[:4] + "***" + s[-2:]) if len(s) > 8 else ("***" if s else "")
        for path in (("asr", "openai", "apiKey"), ("asr", "dashscope", "apiKey"),
                     ("asr", "volc", "apiKey"), ("asr", "volc", "secretAccessKey"),
                     ("polish", "apiKey")):
            node = d
            for k in path[:-1]:
                node = node.get(k) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, dict) and node.get(path[-1]):
                node[path[-1]] = _mask(node[path[-1]])
        return d

    # ------------------------------------------------------------ paths
    @property
    def db_path(self) -> str:
        return os.path.join(self.home, "library.db")

    @property
    def chrome_profile(self) -> str:
        return self.data["browser"]["profileDir"]

    @property
    def output_dir(self) -> str:
        return self.data["outputDir"]
