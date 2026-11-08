# 把「抖音导出」当成 MCP 服务器用

同一个 Python 引擎既能被 DSH 的 Cordis 插件调用，也能直接以 **MCP（Model Context Protocol）服务器**的方式
暴露给任何支持 MCP 的客户端 —— Claude Desktop、Cursor、以及 DSH 自己的 `mcp-client`。

这样插件对外发布时，用户不必装 Cordis 插件，只挂一个 MCP 服务器就能用全部能力。

## 启动方式

```bash
# 在插件包目录里（或设置好 PYTHONPATH 指向 <包目录>/engine）
python -X utf8 -m dyx.mcp --home <配置目录>
```

- 传输：stdio，换行分隔的 JSON-RPC 2.0（MCP stdio 规范）
- `--home` 指配置目录（默认 `~/.dsh-douyin`），里面放 `config.json` 和 `library.db`
- 不需要额外依赖：只用标准库 + 引擎本身已有的依赖

## 暴露的工具

| MCP 工具 | 作用 |
|---|---|
| `douyin_run` | 一站式：链接/ID/口令/作者主页 → 下载 → 转写 → 导出 |
| `douyin_sync` | 只同步作者作品清单（默认增量） |
| `douyin_download` | 下载无水印视频（ids 或 author） |
| `douyin_transcribe` | 转写（sensevoice / faster-whisper / openai / dashscope / volc） |
| `douyin_export` | 排版导出（md/txt/srt/vtt/json/html/docx，可合并合集） |
| `douyin_library` | 看统计 / 作者 / 作品清单 |
| `douyin_failures` | 失败原因诊断（404/403/风控/图集/无人声/欠费） |
| `douyin_settings` | 读写配置、装本地模型、看引擎与价格 |

每个工具的参数与 CLI 一致，返回值是引擎的原始 JSON（`{ ok, ... }`）。

## 接到 Claude Desktop

`claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "douyin": {
      "command": "python",
      "args": ["-X", "utf8", "-m", "dyx.mcp", "--home", "C:/Users/you/.dsh-douyin"],
      "env": { "PYTHONPATH": "D:/path/to/douyin-export/engine" }
    }
  }
}
```

## 接到 DSH（作为一个 MCP bundle）

DSH 自带 `@deepseek-ai/dsh-mcp-client`，配置式 bundle 就够（不需要 Host/Client 入口文件）：

```yaml
# cordis.patch.yml
- insert:
    - id: douyin-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: douyin
        transport: stdio
        command: python
        args:
          - -X
          - utf8
          - -m
          - dyx.mcp
          - --home
          - <你的配置目录>
        env:
          PYTHONPATH: <插件包目录>/engine
        failOnStartupError: true
```

配上 `package.json`（最小配置式 bundle）：

```json
{
  "name": "@local/douyin-mcp",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "exports": { "./locale/*.json": "./locale/*.json", "./icon": "./icon.svg" },
  "files": ["cordis.patch.yml", "locale/*.json", "icon.svg"],
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

装好后工具名会变成 `mcp__douyin__douyin_run` 这样。

## 自测

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"douyin_library","arguments":{"view":"stats"}}}' \
  | python -X utf8 -m dyx.mcp --home <配置目录>
```

## 注意

- MCP 服务器没有「任务目录」概念时用 `out` 参数指定产物目录；不传就落到配置的 `outputDir`。
- 长任务（作者全量同步、批量转写）在 MCP 里是同步阻塞的，客户端要自带超时；需要后台请直接用 DSH 插件的 `background`。
- 配置里的 Key 只在本机 `<home>/config.json`，MCP 不会把 Key 写进任何响应。
