---
name: douyin-export
description: 抖音视频下载、作者主页增量同步、语音转写、AI 术语校对与多格式排版导出（本地 SenseVoice / faster-whisper、OpenAI 兼容、阿里云百炼、豆包火山引擎四种转写引擎可选）。
whenToUse: 用户给出抖音作品链接、分享口令、作品 ID、作者主页链接，或要求把已下载的视频转成文字、纠正转写错字、导出 Markdown/SRT/DOCX/合集，或设置定时增量同步时使用。
---

> Not running inside DSH? Use the framework-neutral MCP variant instead:
> [`mcp/SKILL.md`](../../mcp/SKILL.md) — same capabilities with `douyin_*` tool names.

# 抖音导出

这个插件把「抖音无水印下载 → 语音转写 → 排版导出」串成一条流水线，所有产出落在任务目录 `<home>/tasks/<taskId>/` 下。

## 什么时候调哪个工具

| 用户说的话 | 调用 |
|---|---|
| 粘一个/多个链接、分享口令、作品 ID、作者主页链接，或混着给 | `dyx_run`（`target` 多行文本，粘什么都能认） |
| 只要列表/元数据，先不下载 | `dyx_sync` |
| 只要下载，不要转写 | `dyx_download`（`ids` 或 `author`） |
| 已有视频，要转文字 | `dyx_transcribe`（`ids`/`author`/`limit`/`engine`/`force`） |
| 已有转写，要导出文件 | `dyx_export`（`formats`、`merge`、`statsFields`、`withLink`、`mergeOrder`） |
| 看库里有什么、进度如何 | `dyx_library`（`stats`/`authors`/`videos`） |
| 转写有同音错字、要按术语纠正 | `dyx_polish`（`show`/`sample`/`accept`/`acceptLocal`/`applyOnly`/`reset`） |
| 失败了多少、为什么失败 | `dyx_failures` |
| 重试可抢救的失败 | `dyx_download` 之前先用 `dyx_failures` 判断，永久失败（404/403）不要重试 |
| 改配置、装本地模型、看引擎与价格 | `dyx_settings`（`get`/`set`/`install-model`/`engines`） |
| 需要登录（超过一页的旧作品、私密内容） | `dyx_login`（弹出二维码，用户扫码一次即可，之后复用 profile） |
| 查询后台任务 | `dyx_job`（`list`/`status`/`cancel`） |
| 让某个作者每天自动同步新作品 | `dyx_subscribe`（`action`=`create`/`list`/`delete`，`author`，可选 `rrule`/`download`/`transcribe`） |

## 规则

1. **不要自己编造 aweme_id 或 sec_uid**。用户没给全就调用 `dyx_sync`/`dyx_library` 去查，或直接问用户。
2. **作者默认增量**：`dyx_sync` / `dyx_run` 遇到库里已有的作品就停（`full: true` 才全量重扫）。用户说「更新一下」就是增量。
3. **长任务走后台**：作者全量同步、批量下载、批量转写、合集导出都设 `background: true`，然后用一句话告诉用户在面板「任务」页看进度；不要在前台等。
4. **价格不主动报**：插件界面不显示价格，用户问成本时才用 `dyx_settings action="engines"` 查，并说明「按你自己账号的计费走」。
5. **四家引擎平等**：不要替用户选引擎。用户没说就用当前配置；用户问「哪个好」就客观列出时间戳能力/是否需要 Key/是否本机跑，不推荐。
6. **危险操作先确认**：删除任务、清空库、覆盖已有导出、把私人视频外发，先走 `douyin.confirm` 动作卡（见下），用户点「确认执行」后再做。
7. **不写死个人信息**：不要把作者的 sec_uid、用户本机路径、任何 Key 写进回复正文或文件内容里，除非用户明确要求写进某个文件。

## 动作卡（可选确认）

需要用户二次确认时，先加载本技能（已在加载中），再调用 `propose_action`：

- `action`: `douyin.confirm`
- `title`: 一句话说明要做什么（用户语言）
- `brief`: 会发生什么、影响范围（例如「将删除任务 t1a1b9b3c44b 的 1490 个导出文件，视频和转写记录保留」）
- `details`: `{ "op": "delete-task", "target": "t1a1b9b3c44b" }`
  - `op` 取值：`delete-task` / `clear-library` / `overwrite-export` / `publish-video`
  - `target`：任务 id、作者 sec_uid 或文件路径

卡片出现在回答下方，用户点「确认执行」后你会收到 `phase: accepted` 的状态通知，那时才真正执行；点「不用了」则收到 `dismissed`，不要重试。

## 输出约定

- 每条视频一篇：`export/md/<日期>_<aweme_id>_<标题>.md`，front-matter 带标题/作者/时长/分辨率/统计，正文按段落带 `[mm:ss]`（可在高级设置里关掉）。
- 合集：`export/合集/<作者>·转写合集.md|txt|html|docx`，默认最新在前。
- 转写文本和音轨在 `transcript/`、`audio/`，视频在 `video/`。

## 失败说明

- `HTTP 404`：作品已被作者删除或转私密，永久失败，不要重试。
- `HTTP 403`：内容受限，永久失败。
- 图集作品：只有图片，按设计跳过，不算失败。
- 无人声：纯音乐/无声视频，标记 `no_speech`，不算失败。
- 账号类错误（欠费/无效 Key/未授权）：立刻停下并告诉用户，不要逐条重试；充值或改 Key 后再续跑。

## 校对（AI 术语纠正）

`dyx_polish` 只改**导出用的文本**，不重跑 ASR、不动时间戳；原始转写一直保留。

- **`show`**：看现有术语表、候选、逐处修正。
- **`sample`**：抽样若干条代表作，让独立的 chat 模型挑同音错字，生成「错→对」候选。抽样不逐条全文跑，成本可控。
- **`accept`**：把用户点过头的候选写成正式术语（`错=>正`），之后全库确定性替换。
- **`acceptLocal`**：只改某一处（`错=>正 || 含该词的原句`），用于「这个词在这里对、在那里错」的情况。
- **`applyOnly`**：只按现有术语表刷一遍，不再问模型。
- **`reset`**：撤销全部校对，导出回到原始转写。

规则：

8. **候选要用户点头**：模型挑出来的只是候选，**不要自动采纳**，把「错→正 + 原句」摆给用户，让他决定采纳/只改这处/丢掉。已发现模型会给出「研学→眼学」「现象→假象」这类破坏性建议。
9. **校对模型单独配置**：用 `polish.baseUrl`/`polish.model`/`polish.apiKey`，与转写引擎无关；插件不预置任何厂商和 Key，用户没配就说明「先在设置页填校对模型的地址与 Key」。
