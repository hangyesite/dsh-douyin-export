# 抖音导出（DSH 插件）

下载抖音无水印视频，用本地或云端语音识别转成文字，可选用对话模型做同音字校对，再排版导出 Markdown、TXT、SRT、VTT、JSON、HTML、DOCX —— 可以一条一个文件，也可以合并成一个合集。

以 DSH 插件形态提供：注册 12 个 Agent 工具、一个技能、一张确认动作卡、一个定时增量入口，以及带任务进度的 Web 面板；重活由随包分发的 Python 引擎执行。

## 能做什么

- **取数** —— 视频链接、分享口令、纯作品 ID、作者主页链接、`sec_uid`，粘什么都能认；作者默认增量同步，遇到已有的作品就停。
- **下载** —— 保存无水印 MP4（自动取最高码率档），ffprobe 校验后再抽出 16kHz 单声道 WAV 供转写。
- **转写** —— 五种引擎任意选（两种本地、三种云端）；支持单条与批量、任务级进度、账号类错误自动熔断暂停、无人声视频标记跳过而不是判失败。
- **校对** —— 可选。对话模型先从样本里挖出「错→对」术语表，你确认哪些是真的，再由本地做确定性替换；不重写正文、不动时间戳。支持「只改这一处」（`错=>对 || 原句`），处理只有个别句子听错的同音字。
- **导出** —— Markdown 带 YAML front-matter，另有 TXT / SRT / VTT / JSON / HTML / DOCX；可选合并合集（MD/TXT/HTML/DOCX），默认最新在前。
- **任务** —— 每次运行独立目录（`video/`、`audio/`、`transcript/`、`export/<格式>/`、`export/合集/`），带阶段表（下载 / 转写 / 导出）和实时进度快照。
- **面板** —— 左栏图标 + 设置分区两个入口，四个页签：任务 / 资料库 / 校对 / 设置 & 登录。

## 运行条件

| 需要 | 说明 |
| --- | --- |
| DSH | 使用 `ctx.tools`、`ctx.slots`、`ctx.skills`、`actionCards`、`schedule` |
| Python | 3.10+，命令行里的 `python`（或用 `DY_PYTHON` / `python` 配置项指定） |
| ffmpeg | `ffmpeg` 与 `ffprobe` 在 PATH 里，或设置 `media.ffmpegPath` |
| 浏览器 | 系统 Chrome 或 Edge（由 Playwright 驱动取抖音元数据） |
| 云端密钥 | 仅当你选云端引擎时需要；本地 SenseVoice 不需要账号 |

Python 依赖不打进包里，按需安装：

```sh
python -m pip install playwright sherpa-onnx faster-whisper python-docx
python -m playwright install chromium   # 已有 Chrome/Edge 可跳过
```

## 安装

```sh
dsh plugin add https://github.com/<owner>/douyin-export
```

或用本地目录：DSH 插件管理 → 安装 bundle → 指向本目录。

## 第一次使用

1. **设置 & 登录** → 需要时点「登录」，扫码一次即可，之后凭 profile 复用；但**绝大多数下载根本不需要登录**。
2. 本地转写要模型：设置里「下载模型」（SenseVoice `int8`，约 228MB），或把 `asr.sensevoice.modelDir` 指到已有目录。
3. 在**任务 → 新建任务**里粘链接，选做什么（只下载，或下载+转写+导出）。

> 作者作品超过约 18 条需要登录态才能继续翻页。这是抖音侧的限制：未登录访客的作品页底部就写着「登录后查看更多作品」。扫码一次后，后续同步就能拿到全量。

## 工具一览

| 工具 | 用途 |
| --- | --- |
| `dyx_run` | 一站式：取数 → 下载 → 转写 → 导出 |
| `dyx_sync` | 同步作者作品清单（默认增量，`full` 全量重扫） |
| `dyx_download` | 按 ID 或作者下载无水印视频 |
| `dyx_transcribe` | 用指定引擎转写 |
| `dyx_export` | 导出（可分条，也可加合集） |
| `dyx_polish` | 挖候选、列术语、采纳（全局或只改一处）、应用、撤销 |
| `dyx_failures` | 按类别解释下载/转写失败原因，并给出能否重试 |
| `dyx_library` | 库统计、作者、作品清单 |
| `dyx_settings` | 读写配置、列出引擎 |
| `dyx_login` | 打开浏览器扫码登录 |
| `dyx_job` | 列出 / 查看 / 取消后台任务 |
| `dyx_subscribe` | 注册定时增量同步（DSH 计划任务） |

长操作返回任务 id 后继续在后台跑，面板看进度，`dyx_job` 从对话侧查。

## 转写引擎

| 引擎 | 运行位置 | 时间戳 | 需要 |
| --- | --- | --- | --- |
| `sensevoice` | 本地 CPU（int8） | 句级（ffmpeg 静音切分） | 下载模型 |
| `faster-whisper` | 本地 CPU/GPU | 词级 | `faster-whisper` 与模型 |
| `openai` | 任意 OpenAI 兼容 `/audio/transcriptions` | 取决于服务商 | baseURL、key、model |
| `dashscope` | 阿里云百炼（Qwen / Fun-ASR） | 词级（`begin_time`/`end_time`） | API Key |
| `volc` | 火山引擎豆包 ASR | 句级（`utterances`） | API Key，以及公网音频 URL 或 TOS 桶 |

插件对各引擎一视同仁：不推荐、不显示价格，面板、工具、CLI 三条路都能选。

AI 校对单独配置（`polish.*`），因为它要的是 **chat 模型**，和 ASR 是两套；填任意 OpenAI 兼容端点即可。

## 配置

配置在 `<home>/config.json`（默认 `~/.dsh-douyin`，可用 `DY_DOUYIN_HOME` 改）。主要项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `outputDir` | `~/DouyinExport` | 不走任务目录时的输出根 |
| `browser.headless` | `false` | 保持 `false`，无头时页面签名会失败 |
| `browser.profileDir` | `<home>/chrome-profile` | 持久化浏览器 profile |
| `media.ffmpegPath` | 自动 | ffmpeg 路径（ffprobe 取同目录） |
| `media.preferRatio` | `1080p` | 首选清晰度，失败自动降级 |
| `asr.engine` | `sensevoice` | 默认引擎 |
| `asr.concurrency` | `4` | 云端并发（本地引擎强制串行） |
| `asr.breakerConsecutive` | `5` | 连续失败 N 次暂停整批（0 关闭） |
| `asr.replacements` | `[]` | 固定替换，`["错=对", …]` |
| `polish.baseUrl` / `model` / `apiKey` | 空 | 校对用的对话模型 |
| `export.formats` | `["md"]` | 默认导出格式 |
| `export.timestampStyle` | `none` | `none` / `clock`（`[mm:ss]`）/ `srt` |
| `export.statsFields` | `[]` | 可分别选 `digg_count`、`comment_count`、`share_count`、`collect_count` |
| `export.includeLink` / `includePublished` | `false` | 正文里写原文链接 / 发布日期 |
| `export.mergeOrder` | `desc` | 合集顺序，最新在前 |
| `export.docxFont` | `微软雅黑` | DOCX 字体（会清掉主题字体并把 `w:lang` 改成中文，否则 Word 按日文字形渲染） |

完整键表见英文版 README。

## 校对是怎么做的

1. **挖** —— 对话模型读样本，只回 `错词 => 正词`（可带原句）；含糊其辞的行丢掉，候选必须真的出现在原文里。
2. **采纳** —— AI 结果只是候选，未采纳不生效。可以全局采纳，也可以「只改这一处」（`错=>对 || 原句`），后者用来处理只有某些句子听错的同音字。
3. **应用** —— 确定性替换：长词优先，再叠加逐处补丁，结果写进 `polished` 列，原始 ASR 文本永远保留；导出读 `polished`。
4. **撤销** —— 一键撤销（或 `dyx_polish reset`），导出立刻回到原始转写。

## 隐私

- 插件不预置任何密钥、路径、账号，所有默认值为空。
- 你填的密钥只写在本机 `<home>/config.json`，任何接口返回都打码。
- 视频与文稿都在本机 `<home>` 下；除抖音外，只访问**你自己配置**的 ASR / 对话端点。

## 已知限制

- 抖音前端常改版；元数据之所以走真实浏览器，是因为纯 HTTP 被 `a_bogus` / Argus 签名链拦住。
- 未登录的作品页只给约 18 条，登录一次才能拿全。
- 图集作品没有视频也没有音频，标记为跳过并在任务失败说明里列出。
- 上游删除或设限的视频返回 404/403，无法恢复；`dyx_failures` 会把它们和可重试的区分开。
- 云端引擎按你自己账号计费；服务商报账号类错误时整批自动暂停，不会把队列白烧完。

## 命令行（不装 DSH 也能用）

```sh
export PYTHONPATH=<plugin>/engine
python -m dyx --home ~/.dsh-douyin sync <sec_uid> --limit 30
python -m dyx --home ~/.dsh-douyin download --author <sec_uid> --limit 10
python -m dyx --home ~/.dsh-douyin transcribe --author <sec_uid> --engine dashscope
python -m dyx --home ~/.dsh-douyin export --author <sec_uid> --formats md srt --merge
python -m dyx --home ~/.dsh-douyin failures --author <sec_uid>
```

每条命令在 stdout 输出一个 JSON 对象，进度写 stderr；退出码 0 成功、1 已处理错误、2 参数错误。

## MCP

`engine/dyx/mcp.py` 是 stdio MCP 服务器，暴露 8 个同名操作，配置示例见 `mcp/README.md`。

## 许可证

MIT，见 [LICENSE](LICENSE)。
