# Changelog

## 0.1.2

- Fixed: the polish model choice now takes effect immediately after saving settings (the cached engine settings are invalidated on every config write).

## 0.1.1

- AI polish now defaults to the harness's own model service (e.g. `deepseek-official / deepseek-flash`): no API key required.
- The polish model is selectable in the panel: any model registered in DSH, or a custom OpenAI-compatible endpoint (engine `http` mode).
- Fixed `store.list_patches()` filtering so per-video patch lists load again.
- `polish.mode` / `polish.provider` config keys added; shipped defaults stay blank (no vendor, no key).

## 0.1.0 — 2026-11-08

First public release.

### Fetch & download
- Watermark-free download through the `aweme/v1/play` endpoint, highest-bitrate variant, ffprobe-verified.
- Metadata through a real browser session (Playwright + system Chrome/Edge, persistent profile); plain HTTP is blocked by Douyin's signature chain.
- Incremental author sync that stops at the first already-known video; `full` rescans.
- One input field accepts video links, share texts, bare IDs, profile links, `sec_uid`s and multi-line batches.
- Image-only posts are detected and skipped instead of failing as downloads.

### Transcription
- Five engines: local SenseVoice (CPU int8), local faster-whisper, any OpenAI-compatible endpoint, Aliyun Bailian, Volcengine Doubao.
- Sentence-level timestamps for engines that do not emit them (ffmpeg silence detection) and a flexible merge pass so paragraphs read naturally.
- Cloud concurrency with account-error circuit breaking; consecutive-failure pause for everything else.
- Silent videos are flagged `no speech` and excluded from the pending queue.

### Proofreading (optional)
- Chat-model term mining with context hints, JSON-or-prose tolerant parsing, and hedging filters.
- AI output is always a candidate; only human-accepted terms are applied.
- Per-occurrence fixes (`wrong => right || sentence`) for homophones that only one sentence gets wrong.
- Deterministic longest-first replacement into a separate `polished` column; raw ASR text preserved; one-click undo.

### Export
- Markdown (YAML front-matter), TXT, SRT, VTT, JSON, HTML, DOCX — per video, into per-format folders.
- Merged collection (MD/TXT/HTML/DOCX) ordered newest-first.
- Optional timestamps, optional stat fields (likes / comments / shares / collects, chosen individually), optional source link and publish date.
- DOCX font handling that strips Office theme fonts and fixes `w:lang`, so Chinese renders as Chinese instead of Japanese glyphs.

### DSH integration
- 12 agent tools, a `douyin-export` Skill, a `douyin.confirm` action card for destructive operations, scheduled incremental sync, and a web panel (left sidebar + settings section) with Tasks / Library / Proofread / Settings tabs.
- Per-task directories with a stage table and a live progress snapshot (percent, rate, ETA).
- Task detail explains every failure by category and offers to retry only the recoverable ones.
- Stdio MCP server exposing eight operations for other clients.
