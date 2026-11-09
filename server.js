/**
 * 短视频导出 —— Web GUI 的 HTTP 接口层。
 *
 * DSH 的 webServer.register({kind, path, handler}) 用的是 **Node 原生**
 * (req: IncomingMessage, res: ServerResponse) 回调，不是 Web `Request/Response`：
 * 老版本写法若直接 return Response，服务端不会写回，浏览器就一直转圈。
 * 所以这里统一用 `send(res, status, value)` 写回。
 *
 * 只做三件事：
 *   1. 把引擎的 JSON 结果原样转给前端；
 *   2. 把长任务交给 detached 子进程（复用 index.js 的 startJob），前端轮询；
 *   3. 把登录二维码截图吐给前端，页面直接把二维码画出来，不依赖浏览器窗口。
 */

const JSON_TYPE = 'application/json; charset=utf-8'

function send(res, status, value) {
  const body = Buffer.from(JSON.stringify(value), 'utf8')
  res.writeHead(status, {
    'content-type': JSON_TYPE,
    'content-length': body.length,
    'cache-control': 'no-store',
  })
  res.end(body)
}

function sendText(res, status, text, type = 'text/plain; charset=utf-8') {
  const body = Buffer.from(text, 'utf8')
  res.writeHead(status, { 'content-type': type, 'content-length': body.length })
  res.end(body)
}

function sendBytes(res, status, bytes, type) {
  res.writeHead(status, { 'content-type': type, 'content-length': bytes.length, 'cache-control': 'no-store' })
  res.end(bytes)
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = []
    let size = 0
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > 8 * 1024 * 1024) {
        req.destroy()
        resolve(null)
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim()
      if (!raw) return resolve({})
      try {
        resolve(JSON.parse(raw))
      } catch {
        resolve(null)
      }
    })
    req.on('error', () => resolve(null))
  })
}

function unwrap(parsed) {
  if (parsed && typeof parsed === 'object' && 'result' in parsed) return parsed.result
  return parsed
}

function asList(value) {
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean)
  if (typeof value === 'string' && value.trim()) {
    return value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean)
  }
  return []
}

function listifyPairs(value) {
  const raw = Array.isArray(value) ? value : typeof value === 'string' ? [value] : []
  const out = []
  for (const item of raw) {
    const text = String(item || '').trim()
    if (!text) continue
    const parts = text.split(/=>|→|=/)
    if (parts.length < 2) continue
    const wrong = parts[0].trim()
    const right = parts.slice(1).join('=').trim()
    if (wrong && right && wrong !== right) out.push(`${wrong}=>${right}`)
  }
  return out
}

export function registerDouyinRoutes(ctx, deps) {
  const { fs, path, config } = deps
  let server = null
  try {
    server = ctx.get('webServer')
  } catch {
    server = null
  }
  if (!server || typeof server.register !== 'function') {
    ctx.logger?.info?.('[shortvideo-export] webServer not ready yet, will retry')
    return null
  }

  const home = deps.home()
  const PLATFORMS = ['douyin', 'xiaohongshu']
  const qrPath = path.join(home, 'login-qr.png')
  const statusPath = path.join(home, 'login-status.json')
  const qrPathOf = (platform) => path.join(home, `login-qr-${platform || 'douyin'}.png`)
  const statusPathOf = (platform) => path.join(home, `login-status-${platform || 'douyin'}.json`)
  const jobsPath = path.join(home, 'jobs')

  const readStatus = (platform) => {
    const want = platform || 'douyin'
    for (const file of [statusPathOf(want), ...(want === 'douyin' ? [statusPath] : [])]) {
      try {
        return JSON.parse(fs.readFileSync(file, 'utf8'))
      } catch {
        /* try next */
      }
    }
    return null
  }

  const wrap = (fn) => async (req, res) => {
    try {
      await fn(req, res, new URL(req.url, 'http://127.0.0.1'))
    } catch (error) {
      if (!res.headersSent) send(res, 200, { ok: false, error: String(error?.message || error) })
      else res.end()
    }
  }

  const tasksHandler = wrap(async (req, res, url) => {
    const rest = String(url.pathname || '').replace(/^\/api\/(?:douyin|shortvideo)\/tasks\/?/, '')
    const parts = rest.split('/').filter(Boolean).map((part) => decodeURIComponent(part))

    // 后台 job 已经结束时，把 task.json 里残留的 running 阶段收口，
    // 否则任务跑完了详情里还写着「转写中 / 未开始」，进度也看不到。
    const reconcile = (task) => {
      if (!task || !task.jobId) return task
      let job = null
      try {
        job = deps.readJob(config, task.jobId)
      } catch (err) {
        return task
      }
      if (!job || job.alive) return task
      const snap = job.snapshot || null
      const ok = !(job.result && job.result.ok === false)
      const data = (job.result && job.result.result) || null
      const list = Array.isArray(task.stages) ? task.stages : []
      const stages = list.map((stage) => {
        const next = { ...stage }
        const hit = snap && snap.kind === stage.key
        if (next.status === 'running' || hit) {
          next.status = ok ? 'done' : 'failed'
          if (snap) {
            if (snap.total) next.total = snap.total
            if (typeof snap.done === 'number') next.done = snap.done
            if (typeof snap.failed === 'number') next.failed = snap.failed
          } else if (data && typeof data.count === 'number') {
            next.total = data.count
            next.done = data.count
          }
        }
        return next
      })
      const status = ok ? 'done' : 'failed'
      const same = JSON.stringify(stages) === JSON.stringify(list) && task.status === status
      if (!same) {
        try {
          deps.tasks.updateTask(home, task.id, {
            stages,
            status,
            finishedAt: task.finishedAt || new Date().toISOString(),
          })
        } catch (err) {
          /* 收口失败不影响读 */
        }
      }
      return { ...task, stages, status }
    }

    const stageView = (task, job, extra = {}) => {
      const list = Array.isArray(task.stages) ? task.stages : []
      const snap = job && job.alive ? job.snapshot : null
      const report = extra.failures && !extra.failures.error ? extra.failures : null
      return list.map((stage) => {
        const out = { key: stage.key, label: stage.label || stage.key, status: stage.status || 'pending' }
        if (snap && snap.kind === stage.key) {
          out.status = 'running'
          if (snap.total) out.total = snap.total
          if (typeof snap.done === 'number') out.done = snap.done
          if (typeof snap.failed === 'number') out.failed = snap.failed
          out.percent = typeof snap.percent === 'number' ? snap.percent : null
          out.etaSec = typeof snap.etaSec === 'number' ? snap.etaSec : null
          out.perSec = typeof snap.perSec === 'number' ? snap.perSec : null
          return out
        }
        if (typeof stage.total === 'number') out.total = stage.total
        if (typeof stage.done === 'number') out.done = stage.done
        if (typeof stage.failed === 'number') out.failed = stage.failed
        // 没有活着的快照时，用库里的真实数字校正阶段状态，
        // 免得任务跑完了详情里还写着「转写中」。
        if (report) {
          if (stage.key === 'download') {
            if (report.scope && report.scope.videos) out.total = report.scope.videos
            out.done = ((report.status && report.status.downloaded) || 0) +
              ((report.status && report.status.skipped) || 0)
            out.failed = ((report.download && report.download.failed) || 0) || out.failed
          } else if (stage.key === 'transcribe') {
            if (report.status && report.status.downloaded) out.total = report.status.downloaded
            out.done = ((report.transcribe && report.transcribe.transcribed) || 0) +
              (((report.transcribe && report.transcribe.noSpeech) || []).length)
            out.failed = Math.max(0, (out.total || 0) - out.done)
          } else if (stage.key === 'export') {
            const exported = (extra.files || []).filter(
              (item) => String((item && item.rel) || '').startsWith('export/'))
            if (exported.length) {
              out.done = exported.length
              out.total = exported.length
            }
          }
          if (typeof out.total === 'number' && out.total > 0 &&
              typeof out.done === 'number') {
            out.percent = Math.min(100, Math.round((out.done * 100) / out.total))
            const settled = out.done + (out.failed || 0)
            if (settled >= out.total) out.status = 'done'
            else if (out.done > 0) out.status = stage.status === 'running' ? 'running' : 'paused'
          }
          if (!out.total && out.status === 'running') out.status = 'done'
        }
        return out
      })
    }

    const summarize = (task, extra = {}) => {
      const job = task.jobId ? deps.readJob(config, task.jobId) : null
      const alive = Boolean(job && job.alive)
      let status = task.status
      if (status === 'running' && !job) status = 'failed'
      if (status === 'running' && job && !alive) {
        status = job.result && job.result.ok === false ? 'failed' : 'done'
      }
      const inner = job && job.result && job.result.result ? job.result.result : null
      if (inner && inner.paused && status !== 'failed') status = 'paused'
      return {
        id: task.id,
        title: task.title,
        kind: task.kind,
        createdAt: task.createdAt,
        updatedAt: task.updatedAt,
        status,
        dir: task.dir.replace(home, '~'),
        input: {
          videos: (task.input && task.input.videos) || [],
          authors: (task.input && task.input.authors) || [],
          unresolved: (task.input && task.input.unresolved) || [],
        },
        options: task.options || {},
        stages: stageView(task, job, extra),
        runs: Array.isArray(task.runs) ? task.runs.length : 0,
        job: job
          ? { id: job.id, label: job.label, alive, startedAt: job.startedAt, snapshot: job.snapshot }
          : null,
      }
    }

    if (req.method === 'POST' && parts[0] === 'adopt') {
      const jobs = deps.listJobs(config) || []
      const known = deps.tasks.listTasks(home, 500) || []
      const used = new Set(known.map((item) => item.jobId).filter(Boolean))
      let added = 0
      for (const job of jobs) {
        if (used.has(job.id)) continue
        try {
          if (deps.tasks.adoptJob(home, job)) added += 1
        } catch (err) {
          /* 单条失败不影响其它 */
        }
      }
      return send(res, 200, { ok: true, added, total: jobs.length })
    }

    if (req.method === 'POST') {
      const id = parts[0]
      const action = parts[1]
      const task = id ? deps.tasks.readTaskById(home, id) : null
      if (!task) return send(res, 404, { ok: false, error: '没有这个任务' })
      const body = (await readBody(req)) || {}
      // 「平台官方字幕」只能跑小红书任务
      if (action === 'transcribe' && String(body.engine || '') === 'official') {
        const source = String((task.input && task.input.text) || '')
        const hasXhs = /xiaohongshu\.com|xhslink\.(?:cn|com)/i.test(source)
        const hasDouyin = /douyin\.com|iesdouyin|aweme\.snssdk/i.test(source)
        if (!hasXhs || hasDouyin) {
          return send(res, 400, {
            ok: false,
            code: 'engine_platform_mismatch',
            error: '这个任务不是小红书下的，「平台官方字幕」用不了。请改选其他转写引擎。',
          })
        }
      }
      const allIds = Array.isArray(task.input && task.input.videos) ? task.input.videos : []
      const ids = allIds.length <= 200 ? allIds : []
      const sec =
        Array.isArray(task.input && task.input.authors) && task.input.authors.length === 1
          ? task.input.authors[0]
          : ''
      const formats = asList(body.formats).length
        ? asList(body.formats)
        : asList(task.options && task.options.formats)
      const engine = body.engine ? String(body.engine) : ((task.options && task.options.engine) || '')
      const strFormats = formats.length ? formats.join(',') : ''
      if (action === 'open') {
        try {
          const { spawn } = await import('node:child_process')
          const opener =
            process.platform === 'win32' ? 'explorer' : process.platform === 'darwin' ? 'open' : 'xdg-open'
          const child = spawn(opener, [task.dir], { detached: true, stdio: 'ignore' })
          child.unref()
          return send(res, 200, { ok: true, dir: task.dir })
        } catch (err) {
          return send(res, 500, { ok: false, error: `打不开目录：${(err && err.message) || err}` })
        }
      }
      if (action === 'retry') {
        // 只重跑「可抢救」的失败（风控/超时/磁盘），404/403 由引擎自己挡掉
        try {
          deps.tasks.upsertStage(home, task.id, 'download', { status: 'running' })
        } catch (err) {
          /* 阶段登记失败不影响任务 */
        }
        const argv = ['retry', '--out', task.dir, '--limit', '500']
        if (sec) argv.push('--author', sec)
        else if (ids.length) argv.push('--ids', ...ids)
        else {
          return send(res, 400, { ok: false, error: '这个任务里没有可识别的作品 id 或作者' })
        }
        const label = `${task.title} · 重试可抢救的失败`
        const job = deps.startJob(config, argv, label)
        deps.tasks.appendRun(home, id, {
          jobId: job.id,
          kind: 'retry',
          label,
          at: new Date().toISOString(),
        })
        deps.tasks.updateTask(home, id, { jobId: job.id })
        return send(res, 200, { ok: true, job })
      }
      if (action !== 'transcribe' && action !== 'export') {
        return send(res, 400, { ok: false, error: `不认识的操作：${action}` })
      }
      try {
        deps.tasks.upsertStage(home, task.id, action, { status: 'running' })
      } catch (err) {
        /* 阶段登记失败不影响任务本身 */
      }
      const argv = [action, '--out', task.dir]
      if (ids.length) argv.push('--ids', ...ids)
      else if (sec) argv.push('--author', sec)
      else {
        return send(res, 400, {
          ok: false,
          error: allIds.length
            ? '这个任务的作品超过 200 条，请到资料库按作者发起'
            : '这个任务里没有可识别的作品 id 或作者，请到资料库发起',
        })
      }
      if (action === 'transcribe' && engine) argv.push('--engine', engine)
      if (strFormats) argv.push('--formats', strFormats)
      if (body && body.stats) argv.push('--with-stats')
      const sfields = Array.isArray(body && body.statsFields) ? body.statsFields : []
      if (sfields.length) argv.push('--stats-fields', ...sfields)
      if (body && body.withLink) argv.push('--with-link')
      if (body && body.published) argv.push('--published')
      if (body && (body.mergeOrder === 'asc' || body.mergeOrder === 'desc')) {
        argv.push('--merge-order', body.mergeOrder)
      }
      if (body && body.timestamps) argv.push('--timestamps')
      if (action === 'export' && body && body.merge) {
        const mf = Array.isArray(body.mergeFormats) && body.mergeFormats.length
          ? body.mergeFormats
          : ['md', 'txt', 'html', 'docx']
        argv.push('--merge', '--merge-formats', mf.join(','))
      }
      const label = `${task.title} · ${action === 'transcribe' ? '转写' : '导出'}`
      const job = deps.startJob(config, argv, label)
      deps.tasks.appendRun(home, id, { jobId: job.id, kind: action, label, at: new Date().toISOString() })
      deps.tasks.updateTask(home, id, { jobId: job.id })
      return send(res, 200, { ok: true, job })
    }

    if (!parts.length) {
      const tasks = deps.tasks.listTasks(home, 60).map(reconcile).map(summarize)
      return send(res, 200, { ok: true, tasks, root: deps.tasks.tasksRoot(home) })
    }

    const task = reconcile(deps.tasks.readTaskById(home, parts[0]))
    if (!task) return send(res, 404, { ok: false, error: '没有这个任务' })
    const job = task.jobId ? deps.readJob(config, task.jobId) : null

    // 失败原因诊断：按任务的作者或作品查一次，失败不影响详情返回
    let failures = null
    try {
      const sec = ((task.input && task.input.authors) || [])[0]
      const ids = ((task.input && task.input.videos) || []).slice(0, 300)
      const argv = ['failures', '--limit', '200']
      if (sec) argv.push('--author', sec)
      else if (ids.length) argv.push('--ids', ...ids)
      if (sec || ids.length) {
        const payload = unwrap(await deps.runEngine(config, argv, { timeoutMs: 45000 }))
        failures = (payload && payload.report) || null
      }
    } catch (error) {
      failures = { error: String(error?.message || error) }
    }

    const files = deps.tasks.taskFiles(task.dir)
    const fileStats = deps.tasks.taskFileStats
      ? deps.tasks.taskFileStats(task.dir)
      : { total: files.length, merged: 0 }
    return send(res, 200, {
      ok: true,
      task: summarize(task, { failures, files }),
      log: job ? job.log : '',
      result: job ? job.result : null,
      snapshot: job ? job.snapshot : null,
      files,
      fileStats,
      failures,
    })
  })

  // AI 校对：术语表挖取 / 采纳 / 应用 / 撤销
  const listifyLocal = (value) => {
    const raw = Array.isArray(value) ? value : [value]
    return raw
      .map((item) => {
        if (!item) return ''
        if (typeof item === 'string') return item.trim()
        const wrong = String(item.wrong || '').trim()
        const right = String(item.right || '').trim()
        const context = String(item.context || '').trim()
        if (!wrong || !right) return ''
        return context ? `${wrong}=>${right} || ${context}` : `${wrong}=>${right}`
      })
      .filter(Boolean)
  }

  const polishHandler = wrap(async (req, res, url) => {
    const action = (url.pathname.split('/')[4] || '').trim() || 'show'
    const body = req.method === 'POST' ? await readBody(req) : {}
    const author = (body && body.author) || url.searchParams.get('author') || ''
    const task = (body && body.task) || url.searchParams.get('task') || ''
    const rawIds = (body && body.ids) || url.searchParams.get('ids') || ''
    const ids = Array.isArray(rawIds)
      ? rawIds.map((v) => String(v).trim()).filter(Boolean)
      : String(rawIds).split(/[\s,;]+/).map((v) => v.trim()).filter(Boolean)
    const argv = ['polish']
    if (author) argv.push('--author', String(author))
    if (task) argv.push('--task', String(task))
    if (ids.length) argv.push('--ids', ...ids)

    try {
      if (action === 'mine') {
        const sample = Number((body && body.sample) || 15)
        const mode = deps.polishHost ? await deps.polishHost.mode() : 'http'
        if (mode !== 'http' && deps.polishHost) {
          const hosted = await deps.polishHost.mine({ author, ids, task, sample })
          const patches = unwrap(
            await deps.runEngine(config, ['polish', '--patches', '--limit', '200'], { timeoutMs: 30000 }),
          )
          return send(res, 200, {
            ok: true,
            ...hosted,
            mode: 'host',
            patches: (patches && patches.patches) || [],
          })
        }
        argv.push('--sample', String(sample), '--no-apply')
      } else if (action === 'accept') {
        const pairs = listifyPairs(body && body.pairs)
        if (!pairs.length) return send(res, 400, { ok: false, error: '缺少 pairs' })
        argv.push('--accept', ...pairs)
      } else if (action === 'accept-local') {
        const items = listifyLocal(body && body.items)
        if (!items.length) return send(res, 400, { ok: false, error: '缺少 items' })
        argv.push('--accept-local', ...items)
      } else if (action === 'drop') {
        const pairs = listifyPairs(body && body.pairs)
        if (!pairs.length) return send(res, 400, { ok: false, error: '缺少 pairs' })
        argv.push('--drop', ...pairs)
      } else if (action === 'apply') {
        argv.push('--apply-only')
      } else if (action === 'reset') {
        argv.push('--reset')
      } else {
        argv.push('--show')
      }
      const data = unwrap(await deps.runEngine(config, argv, { timeoutMs: 10 * 60 * 1000 }))
      const out = { ok: true, ...data }
      if (!out.patches) {
        try {
          const patches = unwrap(
            await deps.runEngine(config, ['polish', '--patches', '--limit', '200'], { timeoutMs: 30000 }),
          )
          out.patches = (patches && patches.patches) || []
        } catch {
          out.patches = []
        }
      }
      send(res, 200, out)
    } catch (error) {
      send(res, 200, { ok: false, error: String(error?.message || error) })
    }
  })

  const routes = [
    // 概览：库统计 + 最近任务 + 登录状态 + 配置
    wrap(async (req, res) => {
      const logins = {}
      for (const pf of PLATFORMS) logins[pf] = readStatus(pf)
      const out = { ok: true, home, login: logins.douyin || null, logins, platforms: PLATFORMS, jobs: [] }
      try {
        out.stats = unwrap(await deps.runEngine(config, ['library'], { timeoutMs: 25000 }))
      } catch (error) {
        out.statsError = String(error?.message || error)
      }
      try {
        out.jobs = deps.listJobs(config).slice(0, 12)
      } catch {
        out.jobs = []
      }
      try {
        const settings = unwrap(await deps.runEngine(config, ['settings', 'get'], { timeoutMs: 25000 }))
        out.config = (settings && settings.config) || null
        out.configPath = (settings && settings.configPath) || null
      } catch {
        out.config = null
      }
      send(res, 200, out)
    }),

    // AI 校对可用模型（DSH 已注册的 provider / model）+ 当前生效路由
    wrap(async (req, res) => {
      try {
        if (!deps.polishHost) return send(res, 200, { ok: true, routes: [], mode: 'http' })
        const routes = await deps.polishHost.listRoutes()
        let current = null
        try {
          current = await deps.polishHost.resolveRoute()
        } catch {
          current = null
        }
        const mode = await deps.polishHost.mode()
        send(res, 200, { ok: true, mode, routes, current })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message || error) })
      }
    }),

    // 引擎目录（含单价）
    wrap(async (req, res) => {
      try {
        const data = unwrap(await deps.runEngine(config, ['engines'], { timeoutMs: 20000 }))
        send(res, 200, { ok: true, ...data })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message || error) })
      }
    }),

    // 资料库作品列表（可按作者 / 任务 / 状态 / 关键词筛选）
    wrap(async (req, res, url) => {
      const limit = Number(url.searchParams.get('limit') || 20)
      const offset = Number(url.searchParams.get('offset') || 0)
      const author = url.searchParams.get('author') || ''
      const task = url.searchParams.get('task') || ''
      const status = url.searchParams.get('status') || ''
      const q = url.searchParams.get('q') || ''
      const argv = ['videos', '--limit', String(limit), '--offset', String(offset)]
      if (author) argv.push('--author', author)
      if (task) argv.push('--task', task)
      if (status) argv.push('--status', status)
      if (q) argv.push('--q', q)
      try {
        send(res, 200, { ok: true, ...unwrap(await deps.runEngine(config, argv, { timeoutMs: 30000 })) })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message || error) })
      }
    }),

    // 只读登录状态（前端高频轮询，不碰引擎）
    wrap(async (req, res, url) => {
      const pf = url.searchParams.get('platform') || 'douyin'
      send(res, 200, { ok: true, platform: pf, login: readStatus(pf) })
    }),

    // 登录二维码图片
    wrap(async (req, res, url) => {
      const pf = url.searchParams.get('platform') || 'douyin'
      const file = pf === 'douyin' ? qrPath : qrPathOf(pf)
      try {
        sendBytes(res, 200, fs.readFileSync(file), 'image/png')
      } catch {
        sendText(res, 404, '二维码还没生成')
      }
    }),

    // 新建任务：先建任务目录，这个任务的产出全部落在里面
    wrap(async (req, res) => {
      const body = await readBody(req)
      if (!body) return send(res, 400, { ok: false, error: '请求体不是合法 JSON' })
      const text = String(body.text || '').trim()
      if (!text) return send(res, 400, { ok: false, error: '请至少粘贴一个链接 / 抖音号 / 主页地址' })
      const mode = body.mode === 'full' ? 'full' : 'download'
      const formats = asList(body.formats)
      const engine = body.engine ? String(body.engine) : ''
      // 平台官方字幕只适用于小红书：输入里没有小红书、或混了抖音，直接拒绝建任务
      if (engine === 'official') {
        const hasXhs = /xiaohongshu\.com|xhslink\.(?:cn|com)/i.test(text)
        const hasDouyin = /douyin\.com|iesdouyin|aweme\.snssdk/i.test(text)
        if (!hasXhs || hasDouyin) {
          return send(res, 400, {
            ok: false,
            error: '「平台官方字幕」只能用于小红书作品。请改选其他转写引擎（如阿里云百炼 / 本地 SenseVoice），或只粘小红书链接。',
            code: 'engine_platform_mismatch',
          })
        }
      }
      const input = deps.tasks.parseTargets(text)
      const lines = input.videos.length + input.authors.length + input.unresolved.length
      const title = `${mode === 'full' ? '下载 + 转写 + 导出' : '只下载'}（${lines} 条输入）`
      const task = deps.tasks.createTask(home, {
        kind: mode,
        title,
        input,
        options: { formats, engine, full: !!body.full },
      })
      const inputPath = path.join(task.dir, 'input.txt')
      fs.writeFileSync(inputPath, text, 'utf8')
      const argv = mode === 'full'
        ? ['run', '--file', inputPath, '--out', task.dir]
        : ['add', '--file', inputPath, '--download', '--out', task.dir]
      if (mode === 'full') {
        if (body.full) argv.push('--full')
        if (formats.length) argv.push('--formats', formats.join(','))
        if (engine) argv.push('--engine', engine)
        if (body.merge) {
          const mf = Array.isArray(body.mergeFormats) && body.mergeFormats.length
            ? body.mergeFormats
            : ['md', 'txt', 'html', 'docx']
          argv.push('--merge', '--merge-formats', mf.join(','))
        }
        if (body.stats) argv.push('--with-stats')
        if (Array.isArray(body.statsFields) && body.statsFields.length) {
          argv.push('--stats-fields', ...body.statsFields)
        }
        if (body.withLink) argv.push('--with-link')
        if (body.published) argv.push('--published')
        if (body.mergeOrder === 'asc' || body.mergeOrder === 'desc') {
          argv.push('--merge-order', body.mergeOrder)
        }
        if (body.timestamps) argv.push('--timestamps')
      }
      const job = deps.startJob(config, argv, `${title} · ${task.id}`)
      deps.tasks.updateTask(home, task.id, { jobId: job.id })
      task.jobId = job.id
      send(res, 200, { ok: true, job, task })
    }),

    // 开始扫码登录
    wrap(async (req, res) => {
      const body = (await readBody(req)) || {}
      const pf = String(body.platform || 'douyin')
      if (!PLATFORMS.includes(pf)) return send(res, 400, { ok: false, error: `不支持的平台：${pf}` })
      const wait = Math.min(900, Math.max(60, Number(body.waitSeconds || 300)))
      const qr = qrPathOf(pf)
      const st = statusPathOf(pf)
      try {
        fs.rmSync(qr, { force: true })
        fs.rmSync(st, { force: true })
      } catch {
        /* ignore */
      }
      const label = pf === 'xiaohongshu' ? '等待小红书扫码登录' : '等待抖音扫码登录'
      const job = deps.startJob(
        config,
        ['login', '--platform', pf, '--wait', String(wait), '--qr-out', qr, '--status-out', st],
        label,
      )
      send(res, 200, { ok: true, platform: pf, job })
    }),

    // 单个任务详情（含日志尾巴）
    wrap(async (req, res, url) => {
      const id = url.searchParams.get('id') || ''
      if (!id) return send(res, 400, { ok: false, error: '缺少 id' })
      const job = deps.readJob(config, id)
      if (!job) return send(res, 404, { ok: false, error: '没有这个任务' })
      send(res, 200, { ok: true, job })
    }),

    // 取消任务
    wrap(async (req, res) => {
      const body = (await readBody(req)) || {}
      const id = String(body.id || '')
      if (!id) return send(res, 400, { ok: false, error: '缺少 id' })
      try {
        deps.cancelJob(config, id)
        send(res, 200, { ok: true })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message || error) })
      }
    }),

    // 改配置
    wrap(async (req, res) => {
      const body = await readBody(req)
      if (!body || !body.key) return send(res, 400, { ok: false, error: '缺少 key' })
      try {
        const data = unwrap(
          await deps.runEngine(config, ['settings', 'set', '--key', String(body.key), '--value', String(body.value ?? '')], {
            timeoutMs: 20000,
          }),
        )
        try {
          if (deps.polishHost && typeof deps.polishHost.invalidate === 'function') {
            deps.polishHost.invalidate()
          }
        } catch {
          /* 缓存失效失败不影响保存结果 */
        }
        send(res, 200, { ok: true, data })
      } catch (error) {
        send(res, 200, { ok: false, error: String(error?.message || error) })
      }
    }),
  ]

  const paths = [
    '/api/shortvideo/state',
    '/api/shortvideo/llm-models',
    '/api/shortvideo/engines',
    '/api/shortvideo/videos',
    '/api/shortvideo/status',
    '/api/shortvideo/qr.png',
    '/api/shortvideo/task',
    '/api/shortvideo/login',
    '/api/shortvideo/job',
    '/api/shortvideo/cancel',
    '/api/shortvideo/settings',
  ]

  const disposers = []
  paths.forEach((pathname, index) => {
    try {
      disposers.push(server.register({ kind: 'exact', path: pathname, handler: routes[index] }))
    } catch (error) {
      ctx.logger?.warn?.(`短视频导出：注册 ${pathname} 失败——${error?.message || error}`)
    }
  })
  // 任务中心用前缀路由：/api/shortvideo/tasks、/api/shortvideo/tasks/<id>、/…/<id>/transcribe
  try {
    disposers.push(
      server.register({ kind: 'prefix', path: '/api/shortvideo/tasks', handler: tasksHandler }),
    )
  } catch (error) {
    ctx.logger?.warn?.(`短视频导出：注册 /api/shortvideo/tasks 失败——${error?.message || error}`)
  }

  try {
    disposers.push(
      server.register({ kind: 'prefix', path: '/api/shortvideo/polish', handler: polishHandler }),
    )
  } catch (error) {
    ctx.logger?.warn?.(`短视频导出：注册 /api/shortvideo/polish 失败——${error?.message || error}`)
  }

  ctx.logger?.info?.(`短视频导出：已注册 ${disposers.length} 个 Web 接口`)

  return () => {
    for (const dispose of disposers) {
      try {
        dispose()
      } catch {
        /* ignore */
      }
    }
  }
}
