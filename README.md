# ShortVideo Export — DSH plugin

Download watermark-free Douyin (抖音) and Xiaohongshu (小红书) videos, transcribe them with local or cloud speech recognition, proofread the text with a chat model, and export a formatted transcript in Markdown, TXT, SRT, VTT, JSON, HTML or DOCX — one file per video, or one merged collection.

Built as a DSH plugin: it registers agent tools, a Skill, a confirmation action card, a scheduled-sync entry and a web panel with per-task progress. The heavy lifting runs in a bundled Python engine.

## What it does

- **Fetch** — paste a video link, a video ID, a share text, a profile link or a raw `sec_uid`. Authors sync incrementally: the sync stops as soon as it reaches videos it already has.
- **Download** — saves the watermark-free MP4 (highest bitrate variant available) with ffprobe verification, then extracts a 16 kHz mono WAV for transcription.
- **Transcribe** — six interchangeable engines (two local, three cloud, plus Xiaohongshu's own subtitle track when a note carries one). Per-video or batched, with per-task progress, pause-on-account-error circuit breaking, and "no speech" detection so silent videos are skipped instead of failing.
- **Proofread** — an optional chat model mines a `wrong → right` term table from a sample, you accept what is real, and the replacement is applied deterministically. Nothing is rewritten, timestamps are untouched. Per-occurrence fixes (`wrong => right || the sentence`) resolve homophones that only one sentence gets wrong.
- **Export** — Markdown with YAML front-matter, plus TXT / SRT / VTT / JSON / HTML / DOCX. Optional merged collection (MD/TXT/HTML/DOCX), newest first.
- **Tasks** — every run gets its own directory (`video/`, `audio/`, `transcript/`, `export/<format>/`, `export/合集/`), a stage table (download / transcribe / export) and a live progress snapshot.
- **Panel** — left-sidebar icon and a settings section. Tabs: New task, Tasks, Library (filter by author / task / status / keyword), Proofread, **Login**, Settings.
- **Login tab** — one card per platform (currently Douyin and Xiaohongshu), each with its own login state and QR code; adding a platform only means adding one entry to `PLATFORMS`.

## Requirements

| Need | Notes |
| --- | --- |
| DSH | any recent build; the plugin uses `ctx.tools`, `ctx.slots`, `ctx.skills`, `actionCards` and `schedule` |
| Python | 3.10+ on `PATH` as `python` (or set `SV_PYTHON` / the `python` config key) |
| ffmpeg | `ffmpeg` + `ffprobe` on `PATH`, or set `media.ffmpegPath` |
| Browser | system Chrome or Edge — driven by Playwright: Douyin metadata, and the video address inside a Xiaohongshu note page |
| Cloud keys | only if you pick a cloud engine; local SenseVoice needs no account |

In one line: **Python 3.10+** (`playwright`, `numpy`, `python-docx`, plus `sherpa-onnx` for the default local engine), **`ffmpeg`/`ffprobe` on PATH**, and **system Chrome or Edge**; the first local run downloads a ~228 MB model.

Python packages are installed on first use of an engine and are **not** bundled:

```sh
python -m pip install playwright numpy python-docx
# optional, per local engine you actually use
python -m pip install sherpa-onnx        # default local engine (SenseVoice)
python -m pip install faster-whisper     # alternative local engine
python -m playwright install chromium   # only if you have no Chrome/Edge
```

## Install

```sh
dsh plugin add https://github.com/hangyesite/dsh-shortvideo-export
```

Or from a local checkout, in DSH: Plugin Manager → install bundle → point at this directory.

## First run

1. Open the **Login** tab and scan the QR code once per platform (Douyin, Xiaohongshu) if you want a signed-in session. The profile persists afterwards, and **most Douyin downloads work without any login**.
2. Local transcription needs a model: Settings → *Download model* (SenseVoice `int8`, ~228 MB) — or set `asr.sensevoice.modelDir` to an existing directory.
3. Paste a link into **Tasks → New task** and pick what to do (download only, or download + transcribe + export).

> Authors with more than ~18 videos require a logged-in Douyin session to page further. That is a Douyin-side limit, not a plugin limit: the profile page shows "登录后查看更多作品" to anonymous visitors. `sv_login` opens a window, you scan once, and later syncs see the full list.

## Agent tools

| Tool | Purpose |
| --- | --- |
| `sv_run` | one-shot: fetch → download → transcribe → export for any mix of links, IDs and profile links |
| `sv_sync` | sync one author's video list (incremental by default, `full` to rescan) |
| `sv_download` | download watermark-free video for IDs or an author |
| `sv_transcribe` | transcribe downloaded videos with a chosen engine |
| `sv_export` | export transcripts; per-video files and/or a merged collection |
| `sv_polish` | mine term candidates, list terms, accept (global or per-occurrence), apply, reset |
| `sv_failures` | explain download / transcription failures by category, with retry advice |
| `sv_library` | library stats, authors, and video lists |
| `sv_settings` | read/write settings, list engines |
| `sv_login` | open the browser for a one-time QR login |
| `sv_job` | list / inspect / cancel background jobs |
| `sv_subscribe` | register a scheduled incremental sync (DSH scheduled task) |

Long operations return a job id and keep running in the background; the panel shows their progress, and `sv_job` reports from the agent side.

## Transcription engines

| Engine id | Where it runs | Timestamps | Needs |
| --- | --- | --- | --- |
| `sensevoice` | local CPU (int8) | sentence-level (from ffmpeg silence detection) | bundled-model download |
| `faster-whisper` | local CPU/GPU | word-level | `faster-whisper`, a model |
| `openai` | any OpenAI-compatible `/audio/transcriptions` | word/sentence depends on provider | base URL, key, model |
| `dashscope` | Aliyun Bailian (Qwen/Fun-ASR) | word-level (`begin_time`/`end_time`) | API key |
| `volc` | Volcengine Doubao ASR | sentence-level (`utterances`) | API key, and a public audio URL or TOS bucket |
| `official` | the platform's own subtitle track (Xiaohongshu Chinese track) | sentence-level (the subtitle's own timeline) | `pip install xhshow`, one Xiaohongshu login; **Xiaohongshu notes only** |

The plugin treats them as equals: no engine is recommended, no price is displayed, and every engine is reachable from the panel, the tools and the CLI. Pick per task.

> `official` is bound to a platform: it reads the subtitle track stored with a Xiaohongshu note, so it only works on Xiaohongshu tasks. If you pick it while the input has no Xiaohongshu link (or mixes Douyin in), the panel refuses to create the task and the API answers `engine_platform_mismatch`; the engine itself also rejects mixed batches.

AI proofreading is configured separately (`polish.*`) because it needs a **chat** model, not an ASR one — any OpenAI-compatible endpoint works.

## Configuration

Settings live in `<home>/config.json` (`~/.dsh-shortvideo` by default, `SV_HOME` to override). Most are editable from the panel; all are readable/writable by `sv_settings` and by `python -m shortvideo settings`.

| Key | Default | Meaning |
| --- | --- | --- |
| `outputDir` | `~/DouyinExport` | fallback output root when no task directory is used |
| `browser.channel` | `chrome` | `chrome`, `msedge`, or `""` for bundled Chromium |
| `browser.headless` | `false` | keep it `false`; Douyin's page signing fails headless |
| `browser.profileDir` | `<home>/chrome-profile` | persistent browser profile |
| `fetch.pageSize` / `fetch.maxScrolls` | `18` / `400` | paging limits |
| `media.ffmpegPath` | auto | path to `ffmpeg` (ffprobe is looked up next to it) |
| `media.preferRatio` | `1080p` | preferred download ratio, falls back automatically |
| `asr.engine` | `sensevoice` | default engine for `sv_transcribe` |
| `asr.language` | `zh` | language hint |
| `asr.concurrency` | `4` | parallel cloud requests (local engines stay serial) |
| `asr.breakerConsecutive` | `5` | pause the batch after N consecutive failures (0 disables) |
| `asr.replacements` | `[]` | fixed replacements, `["wrong=right", …]` |
| `asr.sensevoice.*` | model dir, threads, ITN | local model settings |
| `asr.fasterWhisper.*` | model, compute type, beam | local whisper settings |
| `asr.openai.*` | base URL, key, model | OpenAI-compatible ASR |
| `asr.dashscope.*` | key, model | Aliyun Bailian ASR |
| `asr.volc.*` | key, tier, TOS bucket/region | Volcengine ASR |
| `polish.baseUrl` / `polish.model` / `polish.apiKey` | empty | chat model for proofreading |
| `polish.concurrency` / `sampleSize` / `minHits` | `2` / `20` / `2` | mining behaviour |
| `export.formats` | `["md"]` | default outputs |
| `export.timestampStyle` | `none` | `none`, `clock` (`[mm:ss]`), `srt` |
| `export.statsFields` | `[]` | any of `digg_count`, `comment_count`, `share_count`, `collect_count` |
| `export.includeLink` / `includePublished` | `false` | write the source link / publish date into the body |
| `export.mergeOrder` | `desc` | merged collection order, newest first |
| `export.docxFont` | `微软雅黑` | DOCX font (theme fonts and a non-zh `w:lang` are stripped, otherwise Word picks Japanese glyphs) |

## Output layout

```
<home>/tasks/<taskId>/
├─ task.json          # title, input, stages, runs
├─ input.txt          # what you pasted
├─ video/<author>/    # watermark-free mp4
├─ audio/<author>/    # 16 kHz mono wav
├─ transcript/        # per-video JSON segments
└─ export/
   ├─ md/  txt/  srt/  vtt/  json/  html/  docx/
   └─ 合集/            # merged collection
```

Markdown looks like this:

```markdown
---
title: 文物保护法实施条例开始征求意见
aweme_id: "7700158942361664842"
url: https://www.douyin.com/video/7700158942361664842
author: 达说
published: 2026-11-07
duration: "05:31"
resolution: 1028x576
transcript:
  engine: dashscope
  model: qwen-audio-3.1-asr-flash
  segments: 51
---

# 文物保护法实施条例开始征求意见

> 05:31

[00:00] 朋友们大家好，欢迎收看达说。……
```

## Proofreading, precisely

1. **Mine** — a chat model reads a sample of transcripts and answers only `wrong => right` lines (optionally with the sentence that contains it). Answers that hedge ("possibly", "needs confirmation") are dropped, and a candidate must actually appear in the source text.
2. **Accept** — AI candidates are only *candidates*. Nothing is applied until you accept it, either globally for that author or **only at that one occurrence** (`wrong => right || the sentence`), which is how homophones that only some sentences get wrong stay correct elsewhere.
3. **Apply** — replacement is deterministic and local: longest term first, then per-occurrence patches, then the text is stored as `polished` next to the untouched `text`. Exports read `polished`; the ASR result is never lost.
4. **Undo** — one click (or `sv_polish reset`) drops every polished text and re-export uses the raw transcript again.

## Privacy

- No key, path or account is shipped with the plugin; every default is empty.
- Keys you enter are written to `<home>/config.json` on your machine only, and are masked in every API response.
- Your media and transcripts stay under `<home>`. The only outbound traffic besides Douyin itself is to the ASR / chat endpoint **you** configure.

## Known limitations

- Douyin changes its front end often; the metadata path drives a real browser precisely because plain HTTP is blocked by the `a_bogus` / Argus signature chain.
- Anonymous profile pages are cut at roughly 18 videos; log in once for the rest.
- Image-only posts (图集) have no video or audio — they are marked `skipped` and listed in the task's failure explanation.
- Videos deleted or restricted upstream return 404/403 and cannot be recovered; `sv_failures` separates those from retryable cases.
- Cloud engines cost money on your own account, and a batch stops itself when the provider reports an account problem instead of burning the whole queue.

## CLI (headless)

The same engine runs without DSH:

```sh
export PYTHONPATH=<plugin>/engine
python -m shortvideo --home ~/.dsh-shortvideo sync <sec_uid> --limit 30
python -m shortvideo --home ~/.dsh-shortvideo download --author <sec_uid> --limit 10
python -m shortvideo --home ~/.dsh-shortvideo transcribe --author <sec_uid> --engine dashscope
python -m shortvideo --home ~/.dsh-shortvideo export --author <sec_uid> --formats md srt --merge
python -m shortvideo --home ~/.dsh-shortvideo failures --author <sec_uid>
```

Every command prints a single JSON object on stdout; progress goes to stderr. Exit code is 0 on success, 1 on a handled error, 2 on a usage error.

## MCP

`plugin/engine/shortvideo/mcp.py` is a stdio MCP server exposing eight of the same operations (`shortvideo_run`, `shortvideo_sync`, `shortvideo_download`, `shortvideo_transcribe`, `shortvideo_export`, `shortvideo_library`, `shortvideo_failures`, `shortvideo_settings`). See `mcp/README.md` for a Claude Desktop or DSH configuration snippet.

## Versioning

This project follows [Semantic Versioning](https://semver.org/) strictly, and every released version is a git tag.

| Change | Bump |
| --- | --- |
| Bug fix, wording, docs, internal refactor with identical behaviour | patch (0.1.x) |
| New user-facing capability, new tool parameter, new config key with a default | minor (0.x.0) |
| Breaking change to a tool signature, config key, output layout, or the minimum DSH version | minor while `0.x`, major from `1.0.0` on |

Rules:

- A version is never reused: `CHANGELOG.md` gets one entry per release, newest first.
- Every release is a commit plus an annotated tag `v<version>`, and `package.json` carries the same version.
- Plugin-market submissions point at the latest tag, not at a moving branch.
- The `0.1.x` series is the first public line. While the major version is `0`, a minor bump may still change a tool signature or config key; `1.0.0` is reserved for the point where the public API is frozen.

## License

MIT — see [LICENSE](LICENSE).

> AI polish runs on the harness's own model service by default (for example `deepseek-official / deepseek-flash`), so **no API key is needed**. Pick any model registered in the harness from the polish page, or switch `polish.mode` to `http` and fill `baseUrl` / `apiKey` for a custom endpoint.
