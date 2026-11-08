// 用 DSH 自己的 LLM 服务跑 AI 校对：不需要任何 Key，默认跟随 DSH 当前模型（如 deepseek-v4.1-flash），
// 也可以在设置里选别的官方模型，或切到自定义 HTTP 端点（引擎侧 http 模式）。
//
// 注意：这里的 `config` 是 Cordis 条目配置（{home, python, engine, formats}），
// 校对相关的配置在引擎侧 <home>/config.json 里，必须通过 `settings get` 读。

function providerId(entry) {
  if (typeof entry === 'string') return entry
  if (!entry) return ''
  return entry.id || entry.provider || entry.name || ''
}

function modelId(entry) {
  if (typeof entry === 'string') return entry
  if (!entry) return ''
  return entry.id || entry.model || entry.name || ''
}

export function createPolishHost({ ctx, deps, config }) {
  const llm = () => (typeof ctx.get === 'function' ? ctx.get('llm') : null)

  // 引擎侧设置（带 5 秒缓存，避免每次请求都起子进程）
  let cache = { at: 0, value: null };
  async function engineSettings(force = false) {
    const now = Date.now()
    if (!force && cache.value && now - cache.at < 5000) return cache.value
    try {
      const data = await deps.runEngine(config, ['settings', 'get'], { timeoutMs: 25000 })
      const value = (data && data.config) || {}
      cache = { at: now, value }
      return value
    } catch {
      return cache.value || {}
    }
  }

  async function mode() {
    const cfg = await engineSettings()
    const m = String((cfg.polish && cfg.polish.mode) || 'host')
    return m === 'http' ? 'http' : 'host'
  }

  async function listRoutes() {
    const service = llm()
    if (!service || typeof service.listProviders !== 'function') return []
    let providers = []
    try {
      providers = (await service.listProviders()) || []
    } catch {
      return []
    }
    const out = []
    for (const raw of providers) {
      const provider = providerId(raw)
      if (!provider) continue
      let models = []
      try {
        models = (await service.listModels(provider)) || []
      } catch {
        models = []
      }
      for (const m of models) {
        const model = modelId(m)
        if (model) out.push({ provider, model, label: provider + ' / ' + model })
      }
    }
    return out
  }

  async function resolveRoute() {
    const routes = await listRoutes()
    if (!routes.length) return null
    const cfg = await engineSettings()
    const polish = cfg.polish || {}
    const wantProvider = String(polish.provider || '').trim()
    const wantModel = String(polish.model || '').trim()
    if (wantProvider || wantModel) {
      const hit = routes.find(
        (r) => (!wantProvider || r.provider === wantProvider) && (!wantModel || r.model === wantModel),
      )
      if (hit) return hit
    }
    // 默认优先 DSH 自带的官方 provider（用户不填任何东西就能用）
    const official = routes.find(
      (r) => /official/i.test(r.provider) && /deepseek/i.test(r.model) && /flash/i.test(r.model),
    )
    if (official) return official
    const v41 = routes.find((r) => /deepseek-v4\.1-flash/i.test(r.model))
    if (v41) return v41
    const flash = routes.find((r) => /deepseek/i.test(r.model) && /flash/i.test(r.model))
    if (flash) return flash
    const deepseek = routes.find((r) => /deepseek/i.test(r.provider) || /deepseek/i.test(r.model))
    return deepseek || routes[0]
  }

  async function chat(route, system, user) {
    const service = llm()
    if (!service || typeof service.stream !== 'function') {
      throw new Error('DSH 的模型服务不可用（ctx.llm 缺失）')
    }
    const messages = []
    if (system) messages.push({ role: 'system', content: [{ type: 'text', text: system }] })
    messages.push({ role: 'user', content: [{ type: 'text', text: user }] })
    let text = ''
    for await (const chunk of service.stream({
      provider: route.provider,
      model: route.model,
      messages,
    })) {
      if (!chunk) continue
      if (chunk.type === 'text-delta') text += chunk.text || chunk.delta || ''
      if (chunk.type === 'finish') {
        const reason = chunk.reason || {}
        if (reason.kind === 'error' || reason.kind === 'aborted') {
          const failure = reason.failure || {}
          throw new Error('校对模型调用失败：' + (failure.code || failure.message || reason.kind))
        }
      }
    }
    return text.trim()
  }

  async function mine(args = {}) {
    const route = await resolveRoute()
    if (!route) throw new Error('DSH 里没有可用的模型（ctx.llm.listProviders 为空）')
    const cfg = await engineSettings()
    const polish = cfg.polish || {}
    const sample = Number(args.sample || polish.sampleSize || 20)
    const argv = ['polish', '--dump-samples', '--sample', String(sample)]
    if (args.author) argv.push('--author', String(args.author))
    if (Array.isArray(args.ids) && args.ids.length) argv.push('--ids', ...args.ids.map(String))
    if (args.task) argv.push('--task', String(args.task))
    const dumped = await deps.runEngine(config, argv, { timeoutMs: 120000 })
    const samples = (dumped && dumped.samples) || []
    if (!samples.length) {
      return { ok: true, mine: { sampled: 0, calls: 0, terms: {}, failed: [] }, route, terms: [] }
    }
    const system = dumped.system
    const template = dumped.userTemplate
    const concurrency = Math.max(1, Math.min(Number(polish.concurrency || 2), 6))
    const replies = []
    const failed = []
    let cursor = 0
    async function worker() {
      while (cursor < samples.length) {
        const item = samples[cursor]
        cursor += 1
        const prompt = String(template || '').replace('{text}', item.text)
        try {
          const reply = await chat(route, system, prompt)
          replies.push({ aweme_id: item.aweme_id, reply })
        } catch (err) {
          failed.push({ aweme_id: item.aweme_id, error: String((err && err.message) || err) })
        }
      }
    }
    await Promise.all(Array.from({ length: concurrency }, worker))
    const tmp = deps.path.join(deps.home(), 'polish-replies.json')
    deps.fs.writeFileSync(tmp, JSON.stringify({ replies }), 'utf8')
    const argv2 = ['polish', '--ingest', tmp, '--limit', '300']
    if (args.author) argv2.push('--author', String(args.author))
    const ingested = await deps.runEngine(config, argv2, { timeoutMs: 180000 })
    try {
      deps.fs.unlinkSync(tmp)
    } catch {
      /* ignore */
    }
    const mine = (ingested && ingested.mine) || {}
    return {
      ok: true,
      mine: { ...mine, sampled: samples.length, calls: replies.length, failed },
      route,
      terms: (ingested && ingested.terms) || [],
      patches: (ingested && ingested.patches) || [],
    }
  }

  function invalidate() {
    cache = { at: 0, value: null };
  }

  return { mode, engineSettings, listRoutes, resolveRoute, chat, mine, invalidate }
}
