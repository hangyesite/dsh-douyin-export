/**
 * 任务目录：每发起一个任务就建一个目录，这个任务的所有产出都落在里面。
 *
 *   <home>/tasks/<taskId>/
 *     task.json        任务描述（输入 / 选项 / 状态 / 关联 job）
 *     transcript/      转写结果 JSON（每条一个文件）
 *     export/          排版导出（md / srt / docx …）
 *     input.txt        原始粘贴内容
 */
import fs from 'node:fs'
import path from 'node:path'

const SEC_RE = /MS4wLjABAAAA[\w-]{10,}/
const ID_RE = /(\d{15,25})/
const SHORT_RE = /v\.douyin\.com\//i

export function tasksRoot(home) {
  return path.join(home, 'tasks')
}

/** 把用户粘贴的多行文本拆成「作品 id / 作者 sec_uid / 待引擎解析」，只做粗分。 */
export function parseTargets(text) {
  const videos = []
  const authors = []
  const unresolved = []
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim()
    if (!line) continue
    const sec = line.match(SEC_RE)
    if (sec) {
      if (!authors.includes(sec[0])) authors.push(sec[0])
      continue
    }
    const id = line.match(ID_RE)
    if (id) {
      if (!videos.includes(id[1])) videos.push(id[1])
      continue
    }
    if (SHORT_RE.test(line) || /douyin\.com/i.test(line)) {
      if (!unresolved.includes(line)) unresolved.push(line)
      continue
    }
    if (!videos.includes(line) && !unresolved.includes(line)) unresolved.push(line)
  }
  return { text: String(text || ''), videos, authors, unresolved }
}

/** 按输入（作者 sec_uid / 作品 id）找已有任务：同一个作者只留一个任务。 */
export function findTaskForInput(home, input, limit = 200) {
  if (!input) return null
  const authors = Array.isArray(input.authors) ? input.authors : []
  const videos = Array.isArray(input.videos) ? input.videos : []
  if (!authors.length && !videos.length) return null
  let list = []
  try {
    list = listTasks(home, limit) || []
  } catch (err) {
    return null
  }
  for (const task of list) {
    if (!task || task.status === 'cancelled') continue
    const ti = task.input || {}
    const ta = Array.isArray(ti.authors) ? ti.authors : []
    const tv = Array.isArray(ti.videos) ? ti.videos : []
    if (authors.some((sec) => ta.includes(sec))) return task
    if (videos.some((id) => tv.includes(id))) return task
  }
  return null
}

export function taskDir(home, id) {
  return path.join(tasksRoot(home), String(id))
}

function writeTask(dir, task) {
  fs.writeFileSync(path.join(dir, 'task.json'), JSON.stringify(task, null, 2), 'utf8')
}

export function createTask(home, spec) {
  const root = tasksRoot(home)
  fs.mkdirSync(root, { recursive: true })
  const id = `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`
  const dir = path.join(root, id)
  fs.mkdirSync(path.join(dir, 'transcript'), { recursive: true })
  fs.mkdirSync(path.join(dir, 'export'), { recursive: true })
  const now = new Date().toISOString()
  const task = {
    id,
    dir,
    createdAt: now,
    updatedAt: now,
    status: 'running',
    kind: spec.kind || 'download',
    title: spec.title || '未命名任务',
    input: spec.input || { text: '', videos: [], authors: [], unresolved: [] },
    options: spec.options || {},
    jobId: spec.jobId || null,
    runs: spec.jobId ? [{ jobId: spec.jobId, kind: spec.kind || 'download', label: spec.title, at: now }] : [],
    stages: Array.isArray(spec.stages) ? spec.stages : defaultStages(spec.kind),
    counts: {},
    note: '',
  }
  writeTask(dir, task)
  return task
}

/** 一个任务可以有多段：下载 / 转写 / 导出，各自带状态与计数。 */
export function defaultStages(kind) {
  if (kind === 'download') return [{ key: 'download', label: '下载', status: 'running', total: null, done: 0, failed: 0 }]
  if (kind === 'transcribe') return [{ key: 'transcribe', label: '转写', status: 'running', total: null, done: 0, failed: 0 }]
  if (kind === 'export') return [{ key: 'export', label: '导出', status: 'running', total: null, done: 0, failed: 0 }]
  return []
}

/** 新建或更新某个阶段。patch 里可以带 status / total / done / failed / note。 */
export function upsertStage(home, id, key, patch) {
  const task = readTaskById(home, id)
  if (!task) return null
  const stages = Array.isArray(task.stages) ? task.stages.slice() : []
  const label = (patch && patch.label) || { download: '下载', transcribe: '转写', export: '导出' }[key] || key
  let stage = stages.find((item) => item.key === key)
  if (!stage) {
    stage = { key, label, status: 'pending', total: null, done: 0, failed: 0 }
    stages.push(stage)
  }
  Object.assign(stage, patch || {}, { key, label })
  return updateTask(home, id, { stages })
}

export function readTaskById(home, id) {
  const dir = taskDir(home, id)
  try {
    const task = JSON.parse(fs.readFileSync(path.join(dir, 'task.json'), 'utf8'))
    task.dir = dir
    return task
  } catch {
    return null
  }
}

export function updateTask(home, id, patch) {
  const dir = taskDir(home, id)
  let task = null
  try {
    task = JSON.parse(fs.readFileSync(path.join(dir, 'task.json'), 'utf8'))
  } catch {
    return null
  }
  const next = { ...task, ...patch, updatedAt: new Date().toISOString(), dir }
  try {
    if (patch.runs && task.runs && Array.isArray(task.runs) && !Array.isArray(patch.runs)) {
      next.runs = task.runs
    }
    writeTask(dir, next)
  } catch {
    /* 磁盘满等异常不致命 */
  }
  return next
}

/** 追加一次运行记录（同一个任务可以再发起转写 / 导出）。 */
export function appendRun(home, id, run) {
  const task = readTaskById(home, id)
  if (!task) return null
  const runs = Array.isArray(task.runs) ? task.runs.slice() : []
  runs.push(run)
  return updateTask(home, id, { runs, status: 'running' })
}

function walkFiles(base, rel, out, depth) {
  if (depth > 4 || out.length >= 400) return
  let entries = []
  try {
    entries = fs.readdirSync(path.join(base, rel), { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    const relPath = rel ? `${rel}/${entry.name}` : entry.name
    if (entry.isDirectory()) walkFiles(base, relPath, out, depth + 1)
    else {
      try {
        const stat = fs.statSync(path.join(base, relPath))
        out.push({ name: entry.name, rel: relPath, size: stat.size, mtime: stat.mtime.toISOString() })
      } catch {
        /* 忽略 */
      }
    }
  }
}

/** 任务目录里的产出文件：合集排最前，其次是转写/视频/音频，再按格式分目录。 */
export function taskFiles(dir, limit = 200) {
  const merged = []
  walkFiles(path.join(dir, 'export'), '合集', merged, 0)
  const rest = []
  for (const top of ['transcript', 'video', 'audio']) walkFiles(dir, top, rest, 0)
  let entries = []
  try {
    entries = fs.readdirSync(path.join(dir, 'export'), { withFileTypes: true })
  } catch (err) {
    entries = []
  }
  const fmtDirs = entries
    .filter((entry) => entry.isDirectory() && entry.name !== '合集')
    .map((entry) => entry.name)
    .sort()
  for (const name of fmtDirs) walkFiles(path.join(dir, 'export'), name, rest, 0)
  for (const entry of entries) {
    if (entry.isDirectory()) continue
    try {
      const stat = fs.statSync(path.join(dir, 'export', entry.name))
      rest.push({
        name: entry.name,
        rel: `export/${entry.name}`,
        size: stat.size,
        mtime: stat.mtime.toISOString(),
      })
    } catch (err) {
      /* 忽略 */
    }
  }
  const all = merged.concat(rest)
  const out = all.slice(0, limit)
  Object.defineProperty(out, 'total', { value: all.length, enumerable: false })
  return out
}

/** 产出文件的总数（面板用来显示「共 N 个，列出前 200」）。 */
export function taskFileStats(dir) {
  const merged = []
  walkFiles(path.join(dir, 'export'), '合集', merged, 0)
  let total = 0
  const walkCount = (base, rel) => {
    let entries = []
    try {
      entries = fs.readdirSync(path.join(base, rel), { withFileTypes: true })
    } catch (err) {
      return
    }
    for (const entry of entries) {
      if (entry.isDirectory()) walkCount(base, rel ? `${rel}/${entry.name}` : entry.name)
      else total += 1
    }
  }
  for (const top of ['export', 'transcript', 'video', 'audio']) walkCount(dir, top)
  return { total, merged: merged.length }
}

/** 把 tasks 功能上线前的历史 job 补建成任务，让旧的运行记录也出现在任务列表里。 */
export function adoptJob(home, job) {
  const id = String((job && job.id) || '')
  if (!id) return null
  if (readTaskById(home, id)) return null
  const dir = taskDir(home, id)
  fs.mkdirSync(path.join(dir, 'transcript'), { recursive: true })
  fs.mkdirSync(path.join(dir, 'export'), { recursive: true })
  const argv = Array.isArray(job.argv) ? job.argv : []
  const outAt = argv.indexOf('--out')
  const outDir = outAt >= 0 ? String(argv[outAt + 1] || '') : ''
  const now = new Date().toISOString()
  const task = {
    id,
    dir,
    createdAt: job.startedAt || now,
    updatedAt: now,
    status: 'running',
    kind: argv[0] || 'task',
    title: `历史：${String(job.label || argv[0] || '运行记录').replace(/MS4wLjABAAAA[\w-]+/g, '作者')}`,
    input: { text: '', videos: [], authors: [], unresolved: [] },
    options: { legacy: true },
    jobId: id,
    runs: [{ jobId: id, kind: argv[0] || 'task', label: job.label || '', at: job.startedAt || now }],
    counts: {},
    note: outDir ? `历史运行记录（产出目录：${outDir}）` : '历史运行记录（当时未使用任务目录）',
  }
  writeTask(dir, task)
  return task
}

export function listTasks(home, limit = 50) {
  const root = tasksRoot(home)
  if (!fs.existsSync(root)) return []
  let ids = []
  try {
    ids = fs.readdirSync(root, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name)
  } catch {
    return []
  }
  const tasks = []
  for (const id of ids) {
    const task = readTaskById(home, id)
    if (task) tasks.push(task)
  }
  tasks.sort((a, b) => (a.createdAt < b.createdAt ? 1 : -1))
  return tasks.slice(0, limit)
}
