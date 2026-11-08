/**
 * Douyin export — a Host-only Cordis plugin.
 *
 * The plugin owns no media logic itself: it registers agent tools that drive a
 * bundled Python engine (`engine/dyx`) as a child process and turn the engine's
 * single JSON result into the tool's output. Every long job can also be run in
 * the background and polled with `dyx_job`.
 *
 * Why a child process: metadata has to come from a real headed Chrome session
 * (Playwright), and transcription needs heavy native wheels. Keeping that in
 * Python keeps this package dependency-free and restartable.
 */
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { registerDouyinRoutes } from './server.js'
import * as tasks from './tasks.js'
import { registerSkills } from './skills.js'
import { registerActionKinds } from './action-cards.js'

export const name = 'douyin-export'
export const inject = ['tools', 'skills']

const PKG_DIR = path.dirname(fileURLToPath(import.meta.url))
const ENGINE_DIR = path.join(PKG_DIR, 'engine')

const MERGED_FORMATS = ['md', 'txt', 'html', 'docx']

const DEFAULT_TIMEOUT = 15 * 60 * 1000
const JSON_TAIL = 2 * 1024 * 1024

// ------------------------------------------------------- tool definitions
//
// The harness normally ships `defineTool` in `@deepseek-ai/dsh-tools`, but that
// package is only resolvable from inside the harness install, not from this
// bundle's own directory. The helper is pure sugar — it compiles the author
// DSL into the raw JSON Schema subset the registry enforces — so it is
// reimplemented here to keep the package dependency-free.

const SCALAR_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'null'])

function compileValueSchema(spec) {
  if (!spec || typeof spec !== 'object') throw new Error('schema node must be an object')
  const node = {}
  if (spec.description) node.description = spec.description
  const type = spec.type
  if (type === 'json') return node
  if (SCALAR_TYPES.has(type)) {
    node.type = type
    if (Array.isArray(spec.enum)) node.enum = [...spec.enum]
    return node
  }
  if (type === 'array') {
    node.type = 'array'
    if (spec.items) node.items = compileValueSchema(spec.items)
    return node
  }
  if (type === 'object') {
    node.type = 'object'
    node.additionalProperties = spec.additionalProperties === true
    if (spec.properties) node.properties = compilePropertyMap(spec.properties).properties
    return node
  }
  throw new Error(`unsupported schema type: ${String(type)}`)
}

function compilePropertyMap(spec) {
  const properties = {}
  const required = []
  for (const key of Object.keys(spec || {})) {
    const item = spec[key]
    properties[key] = compileValueSchema(item)
    if (item && item.required === true) required.push(key)
  }
  return required.length ? { properties, required } : { properties }
}

function defineTool(options) {
  if (options.timeoutMs !== undefined
    && (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0)) {
    throw new Error(`tool ${options.name}: timeoutMs must be a positive finite number`)
  }
  const parameters = { type: 'object', ...compilePropertyMap(options.parameters) }
  const definition = {
    name: options.name,
    description: options.description,
    parameters,
    output: {
      schema: compileValueSchema(options.output.schema),
      render: (args, value) => options.output.render(args, value),
    },
    execute: async (args, exec) => options.execute(args, exec),
  }
  if (options.timeoutMs !== undefined) definition.timeoutMs = options.timeoutMs
  return definition
}

/** The one output shape every tool in this plugin returns. */
const RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ok: { type: 'boolean' },
    summary: { type: 'string' },
    data: { type: 'json' },
  },
}

const renderSummary = (_args, value) => [{ type: 'text', text: value.summary }]

// ---------------------------------------------------------------- process

function resolveHome(config) {
  const fromConfig = config && typeof config.home === 'string' ? config.home.trim() : ''
  if (fromConfig) return fromConfig
  const fromEnv = (process.env.DY_DOUYIN_HOME || '').trim()
  if (fromEnv) return fromEnv
  return path.join(os.homedir(), '.dsh-douyin')
}

function pythonCandidates(config) {
  const pinned = [
    config && typeof config.python === 'string' ? config.python.trim() : '',
    (process.env.DY_PYTHON || '').trim(),
  ].filter(Boolean)
  const fallback = process.platform === 'win32' ? ['python', 'py', 'python3'] : ['python3', 'python']
  return [...new Set([...pinned, ...fallback])]
}

function childEnv() {
  const env = { ...process.env, PYTHONPATH: ENGINE_DIR, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
  delete env.PYTHONHOME
  return env
}

/** Spawn the engine, retrying the next interpreter when the current one is absent. */
function spawnEngine(config, argv, options = {}) {
  const home = resolveHome(config)
  const args = ['-X', 'utf8', '-m', 'dyx', '--home', home, ...argv]
  const candidates = pythonCandidates(config)
  return new Promise((resolve, reject) => {
    let index = 0
    const attempt = () => {
      const exe = candidates[index]
      const child = spawn(exe, args, {
        cwd: options.cwd || ENGINE_DIR,
        env: childEnv(),
        windowsHide: true,
        stdio: options.stdio || ['ignore', 'pipe', 'pipe'],
      })
      let settled = false
      child.once('error', (error) => {
        if (settled) return
        settled = true
        if (error.code === 'ENOENT' && index + 1 < candidates.length) {
          index += 1
          attempt()
          return
        }
        reject(new Error(
          `无法启动 Python（尝试过 ${candidates.join(', ')}）。请安装 Python 3.10+ 并在插件配置里设置 python 路径。原始错误：${error.message}`,
        ))
      })
      child.once('spawn', () => { settled = true })
      resolve({ child, exe })
    }
    attempt()
  })
}

function parseResult(stdout) {
  const trimmed = (stdout || '').trim()
  if (!trimmed) return null
  try {
    return JSON.parse(trimmed)
  } catch {
    const at = trimmed.indexOf('{')
    if (at < 0) return null
    try {
      return JSON.parse(trimmed.slice(at))
    } catch {
      return null
    }
  }
}

/** Run one engine command to completion and return its parsed JSON result. */
async function runEngine(config, argv, options = {}) {
  const timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT
  const { child } = await spawnEngine(config, argv)
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', (chunk) => {
    stdout += chunk.toString('utf8')
    if (stdout.length > JSON_TAIL * 4) stdout = stdout.slice(-JSON_TAIL)
  })
  child.stderr.on('data', (chunk) => {
    stderr += chunk.toString('utf8')
    if (stderr.length > 64 * 1024) stderr = stderr.slice(-64 * 1024)
  })
  const code = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch { /* already gone */ }
      reject(new Error(`引擎执行超时（${Math.round(timeoutMs / 1000)}s）。长任务请传 background: true，再用 dyx_job 轮询。`))
    }, timeoutMs)
    child.once('close', (value) => { clearTimeout(timer); resolve(value) })
  })
  const parsed = parseResult(stdout)
  if (parsed && parsed.ok === false) {
    const error = parsed.error || {}
    throw new Error(`${error.message || '引擎返回失败'}${error.hint ? `（提示：${error.hint}）` : ''}`)
  }
  if (parsed) return parsed
  const tail = (stderr || stdout).trim().split('\n').slice(-12).join('\n')
  throw new Error(`引擎没有返回可解析的结果（退出码 ${code}）。最后输出：\n${tail}`)
}

// ------------------------------------------------------------------- jobs

function jobsDir(config) {
  return path.join(resolveHome(config), 'jobs')
}

function startJob(config, argv, label) {
  const dir = jobsDir(config)
  fs.mkdirSync(dir, { recursive: true })
  const id = `job_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
  const logPath = path.join(dir, `${id}.log`)
  const metaPath = path.join(dir, `${id}.json`)
  const progressPath = path.join(dir, `${id}.progress.json`)
  try {
    fs.unlinkSync(progressPath)
  } catch {
    /* 上一轮没留进度文件是正常情况 */
  }
  const home = resolveHome(config)
  const args = ['-X', 'utf8', '-m', 'dyx', '--home', home, '--log-file', logPath, ...argv]
  const out = fs.openSync(logPath, 'a')
  const candidates = pythonCandidates(config)
  let child = null
  let lastError = null
  for (const exe of candidates) {
    try {
      child = spawn(exe, args, {
        cwd: ENGINE_DIR,
        env: { ...childEnv(), DY_PROGRESS_FILE: progressPath },
        detached: true,
        windowsHide: true,
        stdio: ['ignore', out, out],
      })
      break
    } catch (error) {
      lastError = error
    }
  }
  if (!child) throw new Error(`无法启动 Python：${lastError ? lastError.message : '未知错误'}`)
  child.unref()
  fs.closeSync(out)
  const meta = {
    id,
    label,
    argv,
    pid: child.pid,
    startedAt: new Date().toISOString(),
    log: logPath,
    progress: progressPath,
  }
  fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2), 'utf8')
  return meta
}

function readProgress(file) {
  if (!file) return null
  try {
    if (!fs.existsSync(file)) return null
    const raw = fs.readFileSync(file, 'utf8')
    if (!raw.trim()) return null
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function readJob(config, id) {
  const metaPath = path.join(jobsDir(config), `${id}.json`)
  if (!fs.existsSync(metaPath)) return null
  const meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'))
  let alive = false
  try {
    process.kill(meta.pid, 0)
    alive = true
  } catch {
    alive = false
  }
  let log = ''
  if (meta.log && fs.existsSync(meta.log)) {
    const size = fs.statSync(meta.log).size
    const start = Math.max(0, size - 8192)
    const fd = fs.openSync(meta.log, 'r')
    const buffer = Buffer.alloc(size - start)
    fs.readSync(fd, buffer, 0, buffer.length, start)
    fs.closeSync(fd)
    log = buffer.toString('utf8')
  }
  const result = parseResult(log.slice(log.lastIndexOf('\n{\n') + 1)) || parseResult(log)
  return {
    ...meta,
    alive,
    log: log.split('\n').slice(-40).join('\n'),
    result,
    snapshot: readProgress(meta.progress),
  }
}

function listJobs(config) {
  const dir = jobsDir(config)
  if (!fs.existsSync(dir)) return []
  return fs
    .readdirSync(dir)
    .filter((file) => file.endsWith('.json'))
    .map((file) => {
      const meta = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'))
      let alive = false
      try { process.kill(meta.pid, 0); alive = true } catch { alive = false }
      return {
        id: meta.id,
        label: meta.label,
        pid: meta.pid,
        startedAt: meta.startedAt,
        alive,
        snapshot: readProgress(meta.progress),
      }
    })
    .sort((a, b) => String(b.startedAt).localeCompare(String(a.startedAt)))
}

function cancelJob(config, id) {
  const meta = readJob(config, id)
  if (!meta) throw new Error(`没有找到任务 ${id}`)
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/PID', String(meta.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    } else {
      process.kill(-meta.pid, 'SIGTERM')
    }
    return true
  } catch (error) {
    throw new Error(`取消任务失败：${error.message}`)
  }
}

// ---------------------------------------------------------------- helpers

function fmtBytes(bytes) {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function fmtYuan(value) {
  if (value === null || value === undefined) return '按量计费（取决服务商）'
  const num = Number(value)
  if (!Number.isFinite(num)) return '—'
  if (num === 0) return '0 元（免费）'
  return `≈${num.toFixed(2)} 元/小时`
}

/** 一行一引擎，只陈述事实：在哪跑、多少钱、有没有时间戳、免费额度。 */
function fmtEngineTable(rows) {
  if (!Array.isArray(rows) || !rows.length) return ''
  return rows.map((row) => {
    const place = row.location === 'local' ? '本地' : '云端'
    return `· ${row.id}（${place}）${fmtYuan(row.yuanPerHour)}`
      + `｜时间戳：${row.timestamps || '—'}｜免费额度：${row.freeQuota}`
  }).join('\n')
}

function listify(value) {
  if (!value) return []
  const array = Array.isArray(value) ? value : [value]
  return array.map((item) => String(item).trim()).filter(Boolean)
}

/** 由发起方给出的输入，整理成任务里要记的形状。 */
function inputFor(ids, author, text) {
  const videos = (ids || []).map(String).filter(Boolean)
  const secs = []
  if (author) {
    const m = String(author).match(/MS4wLjABAAAA[\w-]+/)
    if (m) secs.push(m[0])
    else if (/^MS4w/.test(String(author))) secs.push(String(author))
  }
  const parsed = text ? tasks.parseTargets(String(text)) : null
  if (parsed) {
    for (const id of parsed.videos || []) if (!videos.includes(id)) videos.push(id)
    for (const sec of parsed.authors || []) if (!secs.includes(sec)) secs.push(sec)
  }
  return {
    text: text ? String(text) : '',
    videos,
    authors: secs,
    unresolved: parsed ? parsed.unresolved || [] : [],
  }
}

/** 同一作者/同一批作品已有任务时复用，别再新建一条。 */
function reuseTrackedTask(config, track) {
  if (!track || !track.input) return null
  const home = resolveHome(config)
  try {
    const hit = tasks.findTaskForInput(home, track.input)
    if (!hit) return null
    const kind = track.kind || 'download'
    tasks.upsertStage(home, hit.id, kind, { status: 'running', done: 0, failed: 0 })
    return tasks.readTaskById(home, hit.id)
  } catch (err) {
    return null
  }
}

/** 任务目录：插件自己建，产出都放这里。 */
function createTrackedTask(config, argv, track) {
  const home = resolveHome(config)
  const kind = track.kind || 'download'
  try {
    const task = tasks.createTask(home, {
      kind,
      title: track.title || kind,
      input: track.input || { text: '', videos: [], authors: [], unresolved: [] },
      options: { engine: track.engine || null, formats: track.formats || null },
      stages: track.stages || tasks.defaultStages(kind),
    })
    if (track.input && track.input.text) {
      try {
        fs.writeFileSync(path.join(task.dir, 'input.txt'), String(track.input.text), 'utf8')
      } catch (err) {
        /* 输入存档失败不影响任务 */
      }
    }
    return task
  } catch (err) {
    return null
  }
}

function attachJob(config, task, jobId) {
  try {
    const home = resolveHome(config)
    tasks.appendRun(home, task.id, {
      jobId, kind: task.kind, label: task.title, at: new Date().toISOString(),
    })
    tasks.updateTask(home, task.id, { jobId })
  } catch (err) {
    /* 绑定失败就只在任务里留个空壳，不影响运行 */
  }
}

function finishStage(config, task, parsed) {
  try {
    const home = resolveHome(config)
    const result = (parsed && parsed.result) || {}
    const patch = { status: 'done' }
    if (typeof result.count === 'number') {
      patch.done = result.count
      patch.total = result.count + (typeof result.failedCount === 'number' ? result.failedCount : 0)
    }
    if (typeof result.failedCount === 'number') patch.failed = result.failedCount
    tasks.upsertStage(home, task.id, task.kind, patch)
    tasks.updateTask(home, task.id, { status: 'done' })
  } catch (err) {
    /* 收尾写阶段失败不影响结果 */
  }
}

/** Shared handling for the five heavy commands: inline, or detached + polled. */
async function runHeavy(config, argv, { label, background, track }) {
  let finalArgv = argv
  let task = null
  if (track) {
    task = reuseTrackedTask(config, track)
    if (!task) task = createTrackedTask(config, argv, { ...track, title: track.title || label })
    if (task) finalArgv = [...argv, '--out', task.dir]
  }
  if (background) {
    const meta = startJob(config, finalArgv, label)
    if (task) attachJob(config, task, meta.id)
    return {
      ok: true,
      summary: `已在后台开始：${label}（任务 ${task ? task.id : meta.id}，进程 ${meta.pid}）。`
        + (task ? '到面板「任务」页可以看到它的进度与产出目录。' : '用 dyx_job 查看进度。'),
      data: { ...meta, taskId: task ? task.id : null },
    }
  }
  const parsed = await runEngine(config, finalArgv)
  if (task) finishStage(config, task, parsed)
  return { ok: true, summary: label, data: parsed, taskId: task ? task.id : null }
}

// ------------------------------------------------------------------ tools

function summariseIngest(data) {
  const parts = []
  const authors = Array.isArray(data.authors) ? data.authors : []
  const videos = data.videos || {}
  const unknown = (data.input && data.input.unknown) || []
  if (authors.length) {
    for (const entry of authors) {
      if (entry.error) { parts.push(`作者 ${entry.sec_uid || ''} 失败：${entry.error}`); continue }
      const who = entry.author ? `${entry.author.nickname}(${entry.author.unique_id || ''})` : (entry.sec_uid || '作者')
      parts.push(`${who} 新增 ${entry.new ?? 0} 个作品（${entry.stopReason || ''}）`)
      if (entry.warning) parts.push(entry.warning)
    }
  }
  if (videos.requested) {
    parts.push(`单个作品 ${videos.requested} 个，新取 ${videos.fetched || 0}，库里已有 ${videos.known || 0}`
      + ((videos.failed || []).length ? `，失败 ${videos.failed.length}` : ''))
  }
  const dl = data.download
  if (dl) {
    const bytes = (dl.ok || []).reduce((sum, item) => sum + (item.size || 0), 0)
    parts.push(`下载成功 ${dl.count || 0} 个（${fmtBytes(bytes)}）`
      + ((dl.failed || []).length ? `，失败 ${dl.failed.length} 个` : ''))
  }
  const tr = data.transcribe
  if (tr) parts.push(`转写 ${tr.count || 0} 个`)
  const ex = data.export
  if (ex) parts.push(`导出 ${ex.count || 0} 个文件`)
  if (unknown.length) parts.push(`另有 ${unknown.length} 行无法识别，已跳过`)
  return parts.length ? parts.join('；') + '。' : '没有找到可处理的内容。'
}

export function apply(ctx, config = {}) {
  const home = resolveHome(config)
  const defaultEngine = typeof config.engine === 'string' ? config.engine : ''
  const defaultFormats = listify(config.formats)

  ctx.logger?.info?.(`[douyin-export] engine=${ENGINE_DIR} home=${home}`)

  // 把包内 skills/ 注册进 DSH 技能目录，让 Agent 知道什么时候该调这些工具。
  // 服务缺失或注册失败都不影响插件其它能力。
  ctx.effect(() => {
    let off
    try {
      off = registerSkills(ctx, path.join(PKG_DIR, 'skills'))
    } catch (error) {
      ctx.logger?.warn?.(`[douyin-export] 技能注册跳过：${String(error)}`)
    }
    return () => off?.()
  })

  // 可选确认卡（DSH 没装 action-cards 服务时静默跳过）。
  ctx.effect(() => {
    let off
    try {
      off = registerActionKinds(ctx)
    } catch (error) {
      ctx.logger?.warn?.(`[douyin-export] 确认卡注册跳过：${String(error)}`)
    }
    return () => off?.()
  })

  ctx.tools.register(defineTool({
    name: 'dyx_run',
    description:
      '一站式处理抖音内容：识别输入 → 抓元数据 → 下载无水印视频 → 转写 → 排版导出。'
      + 'target 支持三种写法，可混合、每行一个：'
      + '(1) 作者主页链接 —— 先翻页把该作者的作品清单全部抓下来（支持差量/全量），再逐条处理；'
      + '(2) 单个作品链接或纯数字 ID —— 直接抓这一条；'
      + '(3) 抖音分享口令 —— 含 v.douyin.com 短链，自动解析出真实作品或作者。'
      + '抓元数据时会打开 Chrome，但它在屏幕外工作；下载/转写/导出完全后台。',
    parameters: {
      target: { type: 'string', required: true, description: '作品链接/ID、作者主页链接、或分享口令；多行文本表示批量，一行一个' },
      limit: { type: 'integer', description: '作者主页模式下最多处理多少个作品' },
      formats: { type: 'array', items: { type: 'string' }, description: '导出格式，可选 md/txt/srt/vtt/json/html/docx' },
      engine: { type: 'string', description: '转写引擎，五个任选，互不排斥：sensevoice（本地 CPU，0 元）/ faster-whisper（本地，0 元）/ openai（任意 OpenAI 兼容端点，价格随服务商）/ dashscope（阿里百炼，词级时间戳，按 token 约 0.06 元/小时）/ volc（火山豆包，需公网音频 URL，0.8 元/小时）。不传则用配置项 asr.engine。完整对照表见 dyx_settings action=engines。' },
      full: { type: 'boolean', description: '作者模式全量重扫（默认差量：遇到库里已有的旧作品就停）' },
      background: { type: 'boolean', description: '设为 true 时放到后台执行，返回任务 ID，用 dyx_job 轮询' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = ['run', args.target]
      if (args.limit) argv.push('--limit', String(args.limit))
      if (args.full) argv.push('--full')
      const formats = listify(args.formats).length ? listify(args.formats) : defaultFormats
      if (formats.length) argv.push('--formats', ...formats)
      const engine = args.engine || defaultEngine
      if (engine) argv.push('--engine', engine)
      const result = await runHeavy(config, argv, {
        label: `处理 ${String(args.target).split('\n').filter(Boolean).length || 1} 个输入`,
        background: args.background,
        track: { kind: 'download', input: inputFor([], '', args.target), engine, formats },
      })
      if (args.background) return result
      return { ok: true, summary: summariseIngest(result.data.result || {}), data: result.data }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_sync',
    description:
      '同步一个抖音作者主页的作品清单到本地库（支持差量：默认遇到已存在的旧作品就停止）。'
      + '只抓元数据，不下载视频。第一次会弹出 Chrome 窗口。',
    parameters: {
      author: { type: 'string', required: true, description: '作者主页链接或 sec_uid' },
      limit: { type: 'integer', description: '本次最多抓取多少个新作品（默认不限，靠差量停止）' },
      full: { type: 'boolean', description: 'true 表示全量重扫，不在遇到旧作品时停止' },
      download: { type: 'boolean', description: 'true 表示同步后立刻下载新作品的无水印视频' },
      background: { type: 'boolean', description: '后台执行，返回任务 ID' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = ['sync', args.author]
      if (args.limit) argv.push('--limit', String(args.limit))
      if (args.full) argv.push('--full')
      if (args.download) argv.push('--download')
      const result = await runHeavy(config, argv, { label: `同步 ${args.author}`, background: args.background })
      if (args.background) return result
      const sync = result.data.result || {}
      const author = sync.author || {}
      return {
        ok: true,
        summary: `作者 ${author.nickname || '未知'}（${author.unique_id || ''}）共 ${author.aweme_count ?? '?'} 个作品；`
          + `本次新增 ${sync.new} 个，停止原因 ${sync.stopReason}，耗时 ${sync.elapsedSec}s。`
          + (sync.warning ? `\n注意：${sync.warning}` : ''),
        data: result.data,
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_download',
    description:
      '下载无水印视频。三种入口可以任选：targets（推荐，粘贴什么都能认：作品链接/纯 ID/作者主页/分享口令，'
      + '一行一个，作者会先抓清单再下载）、ids（已知作品 ID 列表）、author（库里已同步过的作者）。'
      + '下载走纯 HTTP，不需要浏览器；只有作者模式需要先抓清单时才开浏览器。',
    parameters: {
      targets: { type: 'string', description: '多行文本：作品链接/ID、作者主页链接、抖音分享口令，一行一个' },
      ids: { type: 'array', items: { type: 'string' }, description: '作品 ID 列表' },
      author: { type: 'string', description: '按作者过滤（主页链接或 sec_uid），只下库里已有的作品' },
      limit: { type: 'integer', description: '最多下载多少个' },
      full: { type: 'boolean', description: 'targets 里有作者时，true 表示全量重扫' },
      background: { type: 'boolean', description: '后台执行，返回任务 ID' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const targets = typeof args.targets === 'string' ? args.targets.trim() : ''
      if (targets) {
        const argv = ['add', targets, '--download']
        if (args.full) argv.push('--full')
        if (args.limit) argv.push('--limit-per-author', String(args.limit))
        const result = await runHeavy(config, argv, {
          label: '识别并下载',
          background: args.background,
          track: { kind: 'download', input: inputFor([], '', targets) },
        })
        if (args.background) return result
        return { ok: true, summary: summariseIngest(result.data.result || {}), data: result.data }
      }
      const argv = ['download']
      const ids = listify(args.ids)
      if (ids.length) argv.push('--ids', ...ids)
      if (args.author) argv.push('--author', args.author)
      if (args.limit) argv.push('--limit', String(args.limit))
      const result = await runHeavy(config, argv, {
        label: '下载视频',
        background: args.background,
        track: { kind: 'download', input: inputFor(ids, args.author, '') },
      })
      if (args.background) return result
      const data = result.data.result || {}
      const bytes = (data.ok || []).reduce((sum, item) => sum + (item.size || 0), 0)
      const failures = (data.failed || []).map((item) => `${item.aweme_id}: ${item.error}`).join('；')
      return {
        ok: true,
        summary: `下载成功 ${data.count || 0} 个（共 ${fmtBytes(bytes)}），失败 ${data.failedCount || 0} 个，跳过 ${(data.skipped || []).length} 个。`
          + (failures ? `失败详情：${failures}` : ''),
        data: result.data,
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_transcribe',
    description:
      '把已下载视频的音轨转写成文字。默认用本地 SenseVoice（CPU int8，无需 GPU，自带标点）；'
      + '也可选 faster-whisper 或 OpenAI 兼容的云端 API。首次使用云端引擎需要在 dyx_settings 里配置 baseURL/apiKey。',
    parameters: {
      ids: { type: 'array', items: { type: 'string' }, description: '作品 ID 列表' },
      author: { type: 'string', description: '按作者过滤（主页链接或 sec_uid）' },
      limit: { type: 'integer', description: '最多转写多少个' },
      engine: { type: 'string', description: '同上：sensevoice / faster-whisper / openai / dashscope / volc，任一' },
      force: { type: 'boolean', description: 'true 表示覆盖已有转写结果' },
      background: { type: 'boolean', description: '后台执行，返回任务 ID' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = ['transcribe']
      const ids = listify(args.ids)
      if (ids.length) argv.push('--ids', ...ids)
      if (args.author) argv.push('--author', args.author)
      if (args.limit) argv.push('--limit', String(args.limit))
      const engine = args.engine || defaultEngine
      if (engine) argv.push('--engine', engine)
      if (args.force) argv.push('--force')
      const result = await runHeavy(config, argv, {
        label: '转写视频',
        background: args.background,
        track: { kind: 'transcribe', input: inputFor(ids, args.author, ''), engine },
      })
      if (args.background) return result
      const data = result.data.result || {}
      const failures = (data.failed || []).map((item) => `${item.aweme_id}: ${item.error}`).join('；')
      const noSpeech = (data.noSpeech || []).length
      return {
        ok: true,
        summary: `转写成功 ${data.count || 0} 个，跳过 ${(data.skipped || []).length} 个既有结果。`
          + (noSpeech ? `无人声跳过 ${noSpeech} 个。` : '')
          + (failures ? `失败：${failures}` : ''),
        data: result.data,
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_export',
    description:
      '把转写结果排版导出。默认 Markdown（YAML front-matter + 带 [mm:ss] 时间戳的段落）；'
      + '也可以同时输出 txt/srt/vtt/json/html/docx。'
      + '开 merge 时额外把多条转写合并成一个大文件（md/txt/html/docx），带目录与总时长。',
    parameters: {
      ids: { type: 'array', items: { type: 'string' }, description: '作品 ID 列表' },
      author: { type: 'string', description: '按作者过滤（主页链接或 sec_uid）' },
      limit: { type: 'integer', description: '最多导出多少个' },
      formats: { type: 'array', items: { type: 'string' }, description: 'md/txt/srt/vtt/json/html/docx' },
      merge: { type: 'boolean', description: '同时合并成一个合集文件（默认 false）' },
      mergeFormats: { type: 'array', items: { type: 'string' }, description: '合集格式，默认 md/txt/html/docx' },
      mergeTitle: { type: 'string', description: '合集标题，默认「作者·转写合集」' },
      statsFields: { type: 'array', items: { type: 'string' }, description: '只导这些互动数据：digg_count / comment_count / share_count / collect_count（只导数字，不导评论内容）' },
      timestamps: { type: 'boolean', description: '导出文本带 [mm:ss] 段首时间戳，默认 false（JSON 永远保留）' },
      withLink: { type: 'boolean', description: '正文里写抖音原视频链接，默认 false（front-matter 一直有 url）' },
      published: { type: 'boolean', description: '正文里写发布日期，默认 false（front-matter 一直有）' },
      mergeOrder: { type: 'string', enum: ['desc', 'asc'], description: '合集排序：desc 最新在前（默认），asc 最老在前' },
      background: { type: 'boolean', description: '放后台跑（合并大合集建议开），默认合并时自动开' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = ['export']
      const ids = listify(args.ids)
      if (ids.length) argv.push('--ids', ...ids)
      if (args.author) argv.push('--author', args.author)
      if (args.limit) argv.push('--limit', String(args.limit))
      const formats = listify(args.formats).length ? listify(args.formats) : defaultFormats
      if (formats.length) argv.push('--formats', ...formats)
      const statsFields = listify(args.statsFields)
      if (statsFields.length) argv.push('--stats-fields', ...statsFields)
      if (args.withLink) argv.push('--with-link')
      if (args.published) argv.push('--published')
      if (args.mergeOrder === 'asc' || args.mergeOrder === 'desc') {
        argv.push('--merge-order', args.mergeOrder)
      }
      if (args.timestamps) argv.push('--timestamps')
      const merge = Boolean(args.merge)
      const mergeFormats = listify(args.mergeFormats)
      if (merge) {
        argv.push('--merge', '--merge-formats', ...(mergeFormats.length ? mergeFormats : MERGED_FORMATS))
        if (args.mergeTitle) argv.push('--merge-title', String(args.mergeTitle))
      }
      const result = await runHeavy(config, argv, {
        label: merge ? '导出文字（含合并）' : '导出文字',
        background: Boolean(args.background) || merge,
        track: { kind: 'export', input: inputFor(ids, args.author, ''), formats },
      })
      if (result.data && !result.data.result && result.data.taskId) {
        return { ok: true, summary: result.summary, data: result.data }
      }
      const data = result.data.result || {}
      return {
        ok: true,
        summary: `导出 ${data.count || 0} 个作品。`
          + (merge
            ? `合集 ${data.mergedCount || 0} 个文件${(data.merged && data.merged[0]) ? `：${data.merged.map((m) => path.basename(m.path)).join('、')}` : ''}。`
            : '')
          + ((data.missingTranscript || []).length
            ? `另有 ${data.missingTranscript.length} 个还没有转写结果，先运行 dyx_transcribe。`
            : ''),
        data: result.data,
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_failures',
    description:
      '诊断失败原因：把下载失败按 404/403/风控 分类，列出跳过的图集作品、无人声作品，'
      + '并说明历史欠费等一次性问题。只读本地库与日志，不发网络请求。',
    parameters: {
      author: { type: 'string', description: '作者主页链接或 sec_uid' },
      ids: { type: 'array', items: { type: 'string' }, description: '只看这些作品 ID' },
      limit: { type: 'integer', description: '每类最多列多少条明细，默认 200' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = ['failures', '--limit', String(args.limit || 200)]
      if (args.author) argv.push('--author', args.author)
      const ids = listify(args.ids)
      if (ids.length) argv.push('--ids', ...ids)
      const data = await runEngine(config, argv)
      const report = (data && data.report) || {}
      const notes = Array.isArray(report.notes) ? report.notes : []
      return {
        ok: true,
        summary: notes.length ? notes.join(' ') : '没有发现失败或跳过。',
        data,
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_polish',
    description:
      'AI 校对（可选能力）：抽样把转写稿交给大模型挑同音错字，生成「错→对」术语表，'
      + '再对全库做确定性替换。只改导出用的文本，原始转写照旧保留；不重写正文、不动时间戳。'
      + '需要先在设置里配好 polish.baseUrl / polish.apiKey（chat 模型）。',
    parameters: {
      author: { type: 'string', description: '只处理这个作者（主页链接或 sec_uid）' },
      ids: { type: 'array', items: { type: 'string' }, description: '只处理这些作品 ID' },
      sample: { type: 'integer', description: '抽样多少条代表作去问模型（默认取配置 polish.sampleSize）' },
      applyOnly: { type: 'boolean', description: '只按现有术语表刷库，不再问模型' },
      accept: { type: 'array', items: { type: 'string' }, description: '手工收录术语，例如 "达叔=>达说"' },
      acceptLocal: {
        type: 'array',
        items: { type: 'string' },
        description: '只改这一处（按上下文定位），格式「错=>正 || 含该词的原句」，不影响别处',
      },
      show: { type: 'boolean', description: '只列出术语表' },
      reset: { type: 'boolean', description: '撤销所有校对（清空校对文本，导出回到原始转写）' },
      background: { type: 'boolean', description: '放后台跑（抽样多时建议开）' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = ['polish']
      if (args.author) argv.push('--author', args.author)
      const ids = listify(args.ids)
      if (ids.length) argv.push('--ids', ...ids)
      if (args.sample) argv.push('--sample', String(args.sample))
      const accept = listify(args.accept)
      if (accept.length) argv.push('--accept', ...accept)
      const acceptLocal = listify(args.acceptLocal)
      if (acceptLocal.length) argv.push('--accept-local', ...acceptLocal)
      if (args.applyOnly) argv.push('--apply-only')
      if (args.show) argv.push('--show')
      if (args.reset) argv.push('--reset')

      const result = await runHeavy(config, argv, {
        label: args.show ? '查看校对术语表' : args.reset ? '撤销校对' : 'AI 校对',
        background: args.background,
      })
      if (args.background) return result
      const data = result.data || {}
      if (args.show) {
        const terms = data.terms || []
        return {
          ok: true,
          summary: terms.length
            ? `术语表 ${terms.length} 条：`
              + terms.slice(0, 8).map((t) => `${t.wrong}→${t.right}`).join('、')
              + (terms.length > 8 ? ' …' : '')
            : '术语表还是空的。先跑一次校对（不给 show）让我挖一轮。',
          data,
        }
      }
      if (args.reset) {
        return { ok: true, summary: `已撤销校对：${data.reset ?? 0} 条转写回到原始文本。`, data }
      }
      const mine = data.mine || {}
      const applied = data.apply || {}
      const terms = data.terms || []
      const summary =
        `抽样 ${mine.sampled ?? 0} 条，本轮挑出 ${(mine.terms || []).length} 个错词；`
        + `替换应用 ${applied.changed ?? 0} 条（术语 ${applied.terms ?? 0} 条生效`
        + `${applied.weak ? `，另有 ${applied.weak} 条未达门槛只做展示` : ''}）。`
        + (terms.length ? ` 现有术语表 ${terms.length} 条。` : '')
      return { ok: true, summary, data }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_library',
    description: '查看本地库：统计数字、作者列表，或作品清单（可按作者/状态筛选）。',
    parameters: {
      view: { type: 'string', required: true, enum: ['stats', 'authors', 'videos'], description: '要看的内容' },
      author: { type: 'string', description: 'videos 视图：按作者过滤' },
      status: { type: 'string', description: 'videos 视图：listed / downloaded / failed' },
      limit: { type: 'integer', description: 'videos 视图返回多少条，默认 50' },
      offset: { type: 'integer', description: 'videos 视图偏移' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = [args.view === 'stats' ? 'library' : args.view]
      if (args.view === 'videos') {
        if (args.author) argv.push('--author', args.author)
        if (args.status) argv.push('--status', args.status)
        if (args.limit) argv.push('--limit', String(args.limit))
        if (args.offset) argv.push('--offset', String(args.offset))
      }
      const result = await runEngine(config, argv)
      const data = result
      if (args.view === 'stats') {
        const stats = data.stats || {}
        return {
          ok: true,
          summary: `作品 ${stats.videos} 个，已下载 ${stats.downloaded} 个，已转写 ${stats.transcribed} 个，`
            + `导出 ${stats.exports} 次，失败 ${stats.failed} 个，占用 ${fmtBytes(stats.bytes)}。`
            + `库位置 ${home}。`,
          data,
        }
      }
      if (args.view === 'authors') {
        const authors = data.authors || []
        const lines = authors.slice(0, 20).map((item) => `${item.nickname}（${item.unique_id}）已存 ${item.stored}/${item.aweme_count}`)
        return { ok: true, summary: `共 ${authors.length} 个作者。\n${lines.join('\n')}`, data }
      }
      const videos = data.videos || []
      const lines = videos.slice(0, 20).map((item) => `${item.aweme_id} [${item.status}] ${String(item.desc || '').slice(0, 40)}`)
      return { ok: true, summary: `共 ${data.count} 条。\n${lines.join('\n')}`, data }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_settings',
    description:
      '读写插件设置。可读取当前配置、修改某一项（如输出目录、云端 ASR 的 baseUrl/apiKey/model、ffmpeg 路径）、'
      + '下载本地 SenseVoice 模型，或 action=engines 列出全部转写引擎与其价格。'
      + '修改后用 dyx_library 或再次运行任务确认生效。',
    parameters: {
      action: { type: 'string', required: true, enum: ['get', 'set', 'install-model', 'reset', 'engines'], description: '操作；engines = 列出所有转写引擎及价格/时间戳/免费额度' },
      key: { type: 'string', description: 'set 用的点号路径，如 outputDir、asr.openai.baseUrl、asr.openai.apiKey、media.ffmpegPath' },
      value: { type: 'string', description: 'set 用的值；JSON 字面量会按 JSON 解析，否则按字符串' },
      engine: { type: 'string', description: 'install-model 用：默认 sensevoice' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      if (args.action === 'engines') {
        const list = await runEngine(config, ['engines'])
        const rows = list.engines || []
        return {
          ok: true,
          summary: `可选转写引擎 ${rows.length} 个（都在这里，插件不替你选）：\n${fmtEngineTable(rows)}`,
          data: list,
        }
      }
      const argv = ['settings', args.action]
      if (args.key) argv.push('--key', args.key)
      if (args.value !== undefined && args.value !== null) argv.push('--value', String(args.value))
      if (args.engine) argv.push('--engine', args.engine)
      const result = await runEngine(config, argv, { timeoutMs: 30 * 60 * 1000 })
      if (args.action === 'get') {
        const table = fmtEngineTable(result.engines)
        return {
          ok: true,
          summary: `配置文件：${result.configPath}\n${JSON.stringify(result.config, null, 2)}`
            + (table ? `\n\n可选转写引擎：\n${table}` : ''),
          data: result,
        }
      }
      if (args.action === 'install-model') {
        return { ok: true, summary: `模型已安装到 ${result.result?.modelDir}`, data: result }
      }
      return { ok: true, summary: `${args.action} 完成。`, data: result }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_login',
    description:
      '可选：在打开的 Chrome 里用任意抖音账号扫码登录，用于之后可能需要的更多访问权限。'
      + '大多数下载和作者主页同步都不需要登录，只有遇到"需要登录"错误时再调用。',
    parameters: {
      waitSeconds: { type: 'integer', description: '等待扫码的秒数，默认 240' },
      background: { type: 'boolean', description: '后台执行（推荐：扫码期间可以先做别的事），用 dyx_job 查看结果' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      const argv = ['login', '--wait', String(args.waitSeconds || 240)]
      if (args.background) {
        return runHeavy(config, argv, { label: '等待抖音扫码登录', background: true })
      }
      const result = await runEngine(config, argv, { timeoutMs: 20 * 60 * 1000 })
      return { ok: true, summary: `登录结果：${JSON.stringify(result.result)}`, data: result }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dyx_job',
    description: '查看或取消后台任务。后台任务由 dyx_run / dyx_sync / dyx_download / dyx_transcribe 传 background: true 创建。',
    parameters: {
      action: { type: 'string', required: true, enum: ['list', 'status', 'cancel'], description: '操作' },
      id: { type: 'string', description: 'status/cancel 用的任务 ID' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      if (args.action === 'list') {
        const jobs = listJobs(config)
        if (!jobs.length) return { ok: true, summary: '没有后台任务。', data: { jobs } }
        const lines = jobs.map((job) => `${job.id} ${job.alive ? '运行中' : '已结束'} — ${job.label}（开始于 ${job.startedAt}）`)
        return { ok: true, summary: lines.join('\n'), data: { jobs } }
      }
      if (!args.id) throw new Error('status / cancel 需要提供 id')
      if (args.action === 'cancel') {
        cancelJob(config, args.id)
        return { ok: true, summary: `已请求取消 ${args.id}。`, data: { id: args.id } }
      }
      const job = readJob(config, args.id)
      if (!job) throw new Error(`没有找到任务 ${args.id}`)
      const done = job.result && typeof job.result === 'object'
      return {
        ok: true,
        summary: `任务 ${job.id}（${job.label}）${job.alive ? '仍在运行' : done ? '已结束' : '已结束但没有可解析结果'}。\n最近日志：\n${job.log}`,
        data: job,
      }
    },
  }))


  // 定时订阅：把「某个作者的增量同步」注册成 DSH 计划任务，到点由 DSH 起一个会话自动跑。
  // 依赖 Host 的 schedule 服务（DSH 自带）；没有该服务时只报错，不影响其它工具。
  ctx.tools.register(defineTool({
    name: 'dyx_subscribe',
    description:
      '把「某个抖音作者的增量同步」注册成 DSH 计划任务：到点 DSH 自动开一个会话跑 dyx_sync，并把结果告诉你。'
      + 'action=create 新建（需要 author，可选 rrule/download/transcribe）；action=list 看已有的；action=delete 删除（需要 id）。',
    parameters: {
      action: {
        type: 'string',
        enum: ['create', 'list', 'delete'],
        description: 'create 新建订阅；list 列出抖音相关订阅；delete 删除（配合 id）',
      },
      author: { type: 'string', description: '作者主页链接 / sec_uid / 抖音号（create 必填）' },
      rrule: {
        type: 'string',
        description:
          '两行文本：DTSTART 行 + RRULE 行，例如 "DTSTART;TZID=Asia/Shanghai:20261109T080000\\nRRULE:FREQ=DAILY"。'
          + '不传默认每天 08:00（Asia/Shanghai）',
      },
      label: { type: 'string', description: '作者称呼，只用于计划任务标题（例如"达说"）；不传则用链接/ID 截断' },
      download: { type: 'boolean', description: '同步后是否顺便下载新作品（默认 true）' },
      transcribe: { type: 'boolean', description: '下载后是否顺便转写（默认 false）' },
      id: { type: 'string', description: 'action=delete 时的计划任务 id' },
    },
    output: { schema: RESULT_SCHEMA, render: renderSummary },
    execute: async (args) => {
      let schedule
      try {
        schedule = ctx.get('schedule')
      } catch {
        schedule = undefined
      }
      if (schedule === undefined) {
        throw new Error('当前环境没有计划任务服务（schedule），无法使用订阅；可以改用 dyx_sync 手动同步。')
      }
      const action = typeof args.action === 'string' ? args.action : 'create'

      const pad = (n) => String(n).padStart(2, '0')
      const defaultRRule = () => {
        const next = new Date(Date.now() + 24 * 60 * 60 * 1000)
        const stamp = `${next.getFullYear()}${pad(next.getMonth() + 1)}${pad(next.getDate())}T080000`
        return `DTSTART;TZID=Asia/Shanghai:${stamp}\nRRULE:FREQ=DAILY`
      }

      if (action === 'list') {
        const page = await schedule.list({ limit: 100 })
        const items = (page.items || []).filter((item) => String(item.title || '').includes('抖音'))
        return {
          ok: true,
          summary: items.length
            ? items.map((item) => `${item.id}｜${item.title}｜${item.status}｜下次 ${item.scheduledAt}`).join('\n')
            : '还没有抖音相关的计划任务。用 dyx_subscribe action=create 建一个。',
          data: { items, total: items.length },
        }
      }

      if (action === 'delete') {
        const id = typeof args.id === 'string' ? args.id.trim() : ''
        if (!id) throw new Error('action=delete 需要 id（先用 action=list 查）')
        const result = await schedule.edit({ id, action: 'delete' })
        return {
          ok: true,
          summary: result && result.deleted ? `已删除计划任务 ${id}。` : `没找到计划任务 ${id}。`,
          data: result,
        }
      }

      const author = typeof args.author === 'string' ? args.author.trim() : ''
      if (!author) throw new Error('请在 author 里给出作者主页链接、sec_uid 或抖音号')
      const rrule = typeof args.rrule === 'string' && args.rrule.includes('RRULE') ? args.rrule : defaultRRule()
      const download = args.download !== false
      const transcribe = args.transcribe === true

      const given = typeof args.label === 'string' ? args.label.trim() : ''
      const label = given || (author.length > 28 ? `${author.slice(0, 28)}…` : author)
      const prompt = [
        `这是定时任务：抖音作者「${label}」的增量同步。`,
        `调用 dyx_sync，author 传 ${JSON.stringify(author)}，download 传 ${download ? 'true' : 'false'}，background 传 false。`,
        transcribe ? '同步完成后接着调用 dyx_transcribe（engine 用当前配置），再调用 dyx_export（formats 用当前配置）把新视频导出。' : '',
        '完成后用一两句话汇报：新增几条、跳过几条、失败几条（失败按 dyx_failures 的原因分类说）。',
        '如果返回里带 warning 说被登录墙截断，调用 dyx_login 让用户扫码后再同步一次。',
        '这是自动任务，不要反问用户、不要要求确认；没有新作品就直接说「没有新作品」。',
      ].filter(Boolean).join(' ')

      const entry = await schedule.create({
        title: `抖音：${label} 增量同步`,
        prompt,
        timing: { kind: 'rrule', rrule },
        target: { kind: 'new_once' },
      })
      return {
        ok: true,
        summary: `已创建计划任务「${entry.title}」，下一次 ${entry.scheduledAt}（id ${entry.id}）。`,
        data: entry,
      }
    },
  }))

  // Web GUI：/api/douyin/* 路由（没有 webServer 时自动降级为只提供工具）
  ctx.effect(() =>
    registerDouyinRoutes(ctx, {
      fs,
      path,
      config,
      home: () => resolveHome(config),
      runEngine,
      tasks,
      startJob,
      readJob,
      listJobs,
      cancelJob,
    }),
  )

  ctx.logger?.info?.('[douyin-export] 12 tools registered')
}
