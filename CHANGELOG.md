# 变更记录

## 0.1.4 — 2026-11-09

### 新增
- 小红书（xiaohongshu）平台支持：作者主页全量同步、单条笔记、无水印下载、转写、导出。
- 转写引擎新增「平台官方字幕」（`official`，仅小红书，0 元、句级、逐字最准），缺失时按 `asr.officialFallback` 回落。
- 登录页独立成页签，按平台列出扫码登录（抖音 / 小红书），可扩展。
- 合并导出（`--merge`）：md / txt / html / docx 合集，默认最新在前，可开关发布日期。
- 导出高级设置：互动数据逐项勾选、时间戳、原文链接、发布日期、合集排序。
- 失败诊断（`sv_failures`）与「只重试可抢救的失败」（`retry`）；云端 ASR 熔断/自动冷却。
- 任务模型：每次发起生成一个任务目录（`video/`、`audio/`、`transcript/`、`export/`），列表显示各阶段进度。

### 修复
- `--author` 只认抖音 sec_uid，小红书 24 位 id 会被忽略并退化成全库操作。
- 合集/正文里的原视频链接写死抖音域名，小红书条目点开 404。
- 单条导出文件名不含 id，同名作品互相覆盖（398 条只落地 365 个文件）。
- CLI 的 `--with-link/--with-stats/--timestamps/--published` 默认 False 会覆盖配置文件里的开关。
- 小红书作品时长缺失，合集头部显示 `00:00`。
- 面板 Web 路由在 `webServer` 未就绪时静默不注册，导致资料库/任务全空、登录按钮 404。
- `official` 引擎未做平台绑定校验，抖音作品选用会乱；现在接口层、引擎层、面板三处都会拦（`engine_platform_mismatch`）。

### 变更
- 默认 home 迁到 `D:\DSH-data\shortvideo`（`C:\Users\<user>\.dsh-shortvideo` 为指向它的 junction），
  任务目录、导出文档、库文件都在 D 盘；媒体目录仍由 `outputDir` 决定。
- 插件显示名改为中性的「短视频导出 / ShortVideo Export」。
