---
name: shortvideo-export
description: Download watermark-free Douyin videos, sync an author's posts incrementally, transcribe audio with a local or cloud ASR engine, proofread the transcript with an AI term list, and export formatted Markdown/TXT/SRT/VTT/JSON/HTML/DOCX. Use when the user pastes a douyin.com / v.douyin.com link, a Douyin author profile, or asks to pull a Douyin video into text.
---

# Douyin export (MCP variant)

This skill is written for **any MCP client** (Claude Desktop / Claude Code / Cursor / Cline / Continue / …).
The tools are served by this repository's MCP server (`python -X utf8 -m shortvideo.mcp --home <config dir>`, see
[`README.md`](README.md)); every tool returns the engine's raw JSON `{ "ok": true, ... }`.

> Running inside **DeepSeek Harness (DSH)** instead? Use the Cordis plugin's own skill at
> `skills/shortvideo-export/SKILL.md` — the tools there are named `sv_*` instead of `shortvideo_*`.

## Tools

| Tool | What it does |
|---|---|
| `shortvideo_run` | One shot: link / share text / ID / profile → download → transcribe → export |
| `shortvideo_sync` | Sync an author's post list only (incremental by default) |
| `shortvideo_download` | Download videos by `ids` or by `author` |
| `shortvideo_transcribe` | Transcribe (engine: `sensevoice` \| `faster-whisper` \| `openai` \| `dashscope` \| `volc` \| `official`). `official` is the platform subtitle track and works for **Xiaohongshu notes only** |
| `shortvideo_export` | Export formats (`md txt srt vtt json html docx`), optionally one merged collection |
| `shortvideo_polish` | AI term proofreading: `action=show` \| `sample` \| `accept` \| `apply` \| `reset` |
| `shortvideo_library` | Stats / authors / video list |
| `shortvideo_failures` | Diagnose failures (404, 403, risk control, image posts, no speech, arrears) |
| `shortvideo_settings` | Read/write config, install the local model, list engines |

Any input field (`target`, `author`, `ids`, `ids` arrays) accepts: a full `douyin.com/video/…` URL, a
`v.douyin.com` share link, a bare 15–25 digit ID, a `douyin.com/user/<sec_uid>` profile URL, or a
`sec_uid` itself. Multiple lines are treated as a batch.

## Rules

1. Never invent a video ID. Use only what the user gave you, or what `shortvideo_library` / `shortvideo_sync` returned.
2. For an author, prefer the incremental `shortvideo_sync` first, then `shortvideo_download`; a full re-scan costs minutes.
3. Long jobs (a whole author, dozens of videos) block the MCP call. Warn the user and set a client timeout;
   under DSH use the plugin's `background: true` instead.
4. Never quote prices. The engines are presented as equals; billing is the user's own account.
5. Metadata and post lists need a real browser session (the MCP server drives Playwright with the system
   Chrome/Edge). The first run may open a window; if a login wall appears, run `shortvideo_settings` or the CLI
   `dyx login` and scan the QR code once — the profile is then reused.
6. Transcription engines require `config.json` keys for the cloud ones (`asr.openai.*`, `asr.dashscope.*`,
   `asr.volc.*`). Never echo a key back to the user; `shortvideo_settings` masks them.
7. AI proofreading candidates must be confirmed by the user. The model happily proposes destructive
   "corrections" (e.g. 研学 → 眼学). Default behaviour is: mine candidates, show them, let the user accept.
8. Downloads and transcripts land in the configured `outputDir`, or in `out` when the tool takes one.
