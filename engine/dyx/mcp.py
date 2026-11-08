# -*- coding: utf-8 -*-
"""把抖音导出引擎暴露成 MCP（Model Context Protocol）服务器。

这样任何支持 MCP 的客户端（Claude Desktop、Cursor、以及 DSH 自己的 mcp-client）都能用同一套能力，
不必依赖 Cordis 插件层。

传输：stdio，换行分隔的 JSON-RPC 2.0（MCP stdio 规范）。
用法：
    python -X utf8 -m dyx.mcp --home <配置目录>

支持的方法：initialize、notifications/initialized、ping、tools/list、tools/call。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from . import __version__

SERVER_NAME = "douyin-export"
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "douyin_run",
        "description": "一站式处理抖音内容：抓元数据 → 下载无水印视频 → 转写 → 排版导出。target 可混粘作品链接/ID/分享口令/作者主页，一行一个。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "作品链接/ID、作者主页链接、分享口令；多行表示批量"},
                "limit": {"type": "integer", "description": "作者模式下最多处理多少个作品"},
                "formats": {"type": "array", "items": {"type": "string"}, "description": "导出格式 md/txt/srt/vtt/json/html/docx"},
                "engine": {"type": "string", "description": "转写引擎 sensevoice/faster-whisper/openai/dashscope/volc"},
                "full": {"type": "boolean", "description": "作者模式全量重扫（默认差量）"},
                "out": {"type": "string", "description": "产物目录（默认配置里的 outputDir）"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "douyin_sync",
        "description": "只同步某个作者的作品清单到本地库（不下载）。默认增量：遇到库里已有的旧作品就停。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "author": {"type": "string", "description": "作者主页链接 / sec_uid / 抖音号"},
                "limit": {"type": "integer"},
                "full": {"type": "boolean"},
                "download": {"type": "boolean", "description": "同步后顺便下载新作品"},
            },
            "required": ["author"],
        },
    },
    {
        "name": "douyin_download",
        "description": "下载无水印视频。给 ids 或 author 二选一。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
                "author": {"type": "string"},
                "limit": {"type": "integer"},
                "out": {"type": "string"},
            },
        },
    },
    {
        "name": "douyin_transcribe",
        "description": "把已下载的视频转写成文字。可用引擎：sensevoice（本地 CPU）/ faster-whisper（本地）/ openai（OpenAI 兼容）/ dashscope（阿里百炼）/ volc（豆包）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
                "author": {"type": "string"},
                "limit": {"type": "integer"},
                "engine": {"type": "string"},
                "force": {"type": "boolean", "description": "覆盖已有转写"},
                "out": {"type": "string"},
            },
        },
    },
    {
        "name": "douyin_export",
        "description": "把转写结果排版导出。支持分条导出与合并合集。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}},
                "author": {"type": "string"},
                "limit": {"type": "integer"},
                "formats": {"type": "array", "items": {"type": "string"}},
                "merge": {"type": "boolean", "description": "同时输出一个合集文件"},
                "out": {"type": "string"},
            },
        },
    },
    {
        "name": "douyin_library",
        "description": "查看本地库：统计、作者列表或作品清单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["stats", "authors", "videos"]},
                "author": {"type": "string"},
                "status": {"type": "string", "enum": ["listed", "downloaded", "failed", "skipped"]},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        },
    },
    {
        "name": "douyin_failures",
        "description": "诊断失败原因：下载失败按 404/403/风控 分类，列出跳过的图集与无人声作品，标出可重试的类别。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "author": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "douyin_settings",
        "description": "读写配置：action=get 查看全部配置；set 改一项（key/value）；install-model 下载本地模型；engines 列出全部转写引擎与价格。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "install-model", "engines", "reset"]},
                "key": {"type": "string", "description": "点号路径，如 asr.engine、outputDir"},
                "value": {"type": "string"},
                "engine": {"type": "string"},
            },
            "required": ["action"],
        },
    },
]

HOME = ""


def _engine_argv() -> list:
    return [sys.executable, "-X", "utf8", "-m", "dyx", "--home", HOME]


def _run_cli(argv: list) -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONHOME", None)
    proc = subprocess.run(
        _engine_argv() + argv,
        capture_output=True,
        env=env,
    )
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-8:]
        return {"ok": False, "error": {"code": "engine_failed", "message": "\n".join(err) or "引擎没有输出"}}
    try:
        return json.loads(out)
    except ValueError:
        return {"ok": False, "error": {"code": "engine_output", "message": out[:800]}}


def _build_argv(name: str, args: dict) -> list:
    def fmt_list(value):
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        return str(value)

    if name == "douyin_run":
        argv = ["run", str(args.get("target", ""))]
        if args.get("limit"):
            argv += ["--limit", str(int(args["limit"]))]
        if args.get("formats"):
            argv += ["--formats", fmt_list(args["formats"])]
        if args.get("engine"):
            argv += ["--engine", str(args["engine"])]
        if args.get("full"):
            argv += ["--full"]
        if args.get("out"):
            argv += ["--out", str(args["out"])]
        return argv

    if name == "douyin_sync":
        argv = ["sync", str(args.get("author", ""))]
        if args.get("limit"):
            argv += ["--limit", str(int(args["limit"]))]
        if args.get("full"):
            argv += ["--full"]
        if args.get("download"):
            argv += ["--download"]
        return argv

    if name == "douyin_download":
        argv = ["download"]
        if args.get("ids"):
            argv += ["--ids", *[str(i) for i in args["ids"]]]
        if args.get("author"):
            argv += ["--author", str(args["author"])]
        if args.get("limit"):
            argv += ["--limit", str(int(args["limit"]))]
        if args.get("out"):
            argv += ["--out", str(args["out"])]
        return argv

    if name == "douyin_transcribe":
        argv = ["transcribe"]
        if args.get("ids"):
            argv += ["--ids", *[str(i) for i in args["ids"]]]
        if args.get("author"):
            argv += ["--author", str(args["author"])]
        if args.get("limit"):
            argv += ["--limit", str(int(args["limit"]))]
        if args.get("engine"):
            argv += ["--engine", str(args["engine"])]
        if args.get("force"):
            argv += ["--force"]
        if args.get("out"):
            argv += ["--out", str(args["out"])]
        return argv

    if name == "douyin_export":
        argv = ["export"]
        if args.get("ids"):
            argv += ["--ids", *[str(i) for i in args["ids"]]]
        if args.get("author"):
            argv += ["--author", str(args["author"])]
        if args.get("limit"):
            argv += ["--limit", str(int(args["limit"]))]
        if args.get("formats"):
            argv += ["--formats", fmt_list(args["formats"])]
        if args.get("merge"):
            argv += ["--merge"]
        if args.get("out"):
            argv += ["--out", str(args["out"])]
        return argv

    if name == "douyin_library":
        view = str(args.get("view") or "stats")
        if view == "stats":
            return ["library"]
        argv = [view]
        if args.get("author"):
            argv += ["--author", str(args["author"])]
        if args.get("status") and view == "videos":
            argv += ["--status", str(args["status"])]
        if args.get("limit"):
            argv += ["--limit", str(int(args["limit"]))]
        if args.get("offset"):
            argv += ["--offset", str(int(args["offset"]))]
        return argv

    if name == "douyin_failures":
        argv = ["failures"]
        if args.get("author"):
            argv += ["--author", str(args["author"])]
        if args.get("ids"):
            argv += ["--ids", *[str(i) for i in args["ids"]]]
        if args.get("limit"):
            argv += ["--limit", str(int(args["limit"]))]
        return argv

    if name == "douyin_settings":
        action = str(args.get("action") or "get")
        argv = ["settings", action]
        if action == "set":
            argv += ["--key", str(args.get("key", "")), "--value", str(args.get("value", ""))]
        if action == "install-model" and args.get("engine"):
            argv += ["--engine", str(args["engine"])]
        return argv

    raise ValueError(f"unknown tool {name}")


def _call(name: str, args: dict) -> dict:
    result = _run_cli(_build_argv(name, args))
    ok = bool(result.get("ok"))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        **({} if ok else {"isError": True}),
    }


def _reply(msg_id, result=None, error=None) -> dict:
    body = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return body


def _handle(message: dict):
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        return _reply(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _reply(msg_id, {})
    if method == "tools/list":
        return _reply(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if not any(tool["name"] == name for tool in TOOLS):
            return _reply(msg_id, error={"code": -32602, "message": f"unknown tool: {name}"})
        try:
            return _reply(msg_id, _call(name, params.get("arguments") or {}))
        except Exception as exc:  # noqa: BLE001 - 出错要回给客户端而不是崩掉
            return _reply(msg_id, error={"code": -32603, "message": str(exc)})
    if msg_id is None:
        return None
    return _reply(msg_id, error={"code": -32601, "message": f"unknown method: {method}"})


def main(argv=None) -> int:
    global HOME
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--home" in argv:
        HOME = argv[argv.index("--home") + 1]
    if not HOME:
        HOME = os.environ.get("DY_DOUYIN_HOME") or os.path.join(os.path.expanduser("~"), ".dsh-douyin")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        response = _handle(message)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
