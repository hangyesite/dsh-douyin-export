/**
 * 抖音导出 —— Web 界面。
 *
 * 兑现：不靠弹浏览器窗口，所有操作都在 DSH 自己的页面里完成。
 *
 *   · 会话标题栏右侧一个「抖音导出」按钮 → 打开面板
 *   · 面板四页：新建任务 / 任务 / 资料库 / 设置&登录
 *   · 新建任务支持混粘：视频链接、分享口令、抖音号、作者主页链接，一行一个
 *   · 登录：点一下就把二维码画在面板里，手机扫完即完成（登录态存在 Chrome profile，之后不用再扫）
 *
 * 所有请求都是同源相对路径 /api/douyin/*，由 Host 侧 server.js 提供。
 */
window.__ModuleLoader__.load({
  id: 'dsh-douyin-export',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useState, useEffect, useRef, useCallback, useMemo } = React

    // ---------------------------------------------------------------- 共享状态
    const store = {
      open: false,
      listeners: new Set(),
    }
    function setOpen(value) {
      store.open = value
      for (const fn of store.listeners) {
        try {
          fn(value)
        } catch {
          /* ignore */
        }
      }
    }
    function useOpen() {
      const [value, setValue] = useState(store.open)
      useEffect(() => {
        const fn = (next) => setValue(next)
        store.listeners.add(fn)
        return () => store.listeners.delete(fn)
      }, [])
      return value
    }

    // ------------------------------------------------------------------ 小工具
    const SURFACE = 'var(--dsw-alias-bg-layer-2)'
    const SURFACE_TOP = 'var(--dsw-alias-bg-layer-3)'
    const BORDER = 'var(--dsw-alias-border-l2)'
    const ACCENT = 'var(--dsw-alias-brand-primary)'

    const LIB_STATUS = [
      ['', '全部状态'],
      ['downloaded', '已下载'],
      ['listed', '未下载'],
      ['failed', '失败'],
      ['skipped', '已跳过'],
    ]
    const SEL_STYLE = {
      background: 'transparent',
      color: 'inherit',
      border: `1px solid ${BORDER}`,
      borderRadius: 6,
      padding: '4px 6px',
      fontSize: 12,
      outline: 'none',
    }

    const STATUS_TEXT = { running: '运行中', done: '已完成', failed: '失败', cancelled: '已取消',
      pending: '未开始', paused: '已暂停' }

/** 阶段进度文案：760/773（失败 13）· 剩 18 分钟 */
function stageText(stage) {
  if (!stage) return ''
  if (stage.status === 'pending') return '未开始'
  const parts = []
  if (typeof stage.total === 'number' && stage.total > 0) {
    parts.push(`${stage.done ?? 0}/${stage.total}`)
    if (stage.percent != null) parts.push(`${Math.round(stage.percent)}%`)
  } else if (typeof stage.done === 'number' && stage.done > 0) {
    parts.push(`${stage.done} 条`)
  }
  if (stage.failed) parts.push(`失败 ${stage.failed}`)
  if (stage.etaSec > 1) parts.push(`剩 ${formatCount(stage.etaSec)}`)
  if (!parts.length) return stage.status === 'done' ? '已完成' : '进行中'
  return parts.join(' · ')
}

/** 失败/跳过说明：把下载失败分类、图集与无人声跳过、历史欠费讲清楚 */
function FailureCard({ failures, onRetry, busy }) {
  if (!failures) return null
  if (failures.error) {
    return h('div', { style: { fontSize: 12, opacity: 0.6, marginTop: 10 } }, `失败原因读不到：${failures.error}`)
  }
  const notes = Array.isArray(failures.notes) ? failures.notes : []
  const cats = (failures.download && failures.download.categories) || []
  const images = (failures.download && failures.download.skippedImages) || []
  const noSpeech = (failures.transcribe && failures.transcribe.noSpeech) || []
  const missing = (failures.transcribe && failures.transcribe.missing) || []
  if (!notes.length) return null

  const row = (text, style) => h('div', { style: { fontSize: 12, lineHeight: '1.7', ...(style || {}) } }, text)

  return h(
    'div',
    {
      style: {
        marginTop: 16,
        padding: '12px 14px',
        border: `1px solid ${BORDER}`,
        borderRadius: 10,
        background: 'rgba(127,127,127,0.04)',
      },
    },
    h('div', { style: { fontSize: 13, fontWeight: 600, marginBottom: 8 } }, '失败与跳过说明'),
    (failures.download && failures.download.recoverable && failures.download.recoverable.count > 0 && onRetry)
      ? h(
          'div',
          { style: { marginBottom: 8 } },
          h(
            'button',
            {
              type: 'button',
              onClick: onRetry,
              disabled: busy,
              style: { ...btnStyle('primary'), opacity: busy ? 0.6 : 1 },
            },
            busy
              ? '提交中…'
              : `重试可抢救的失败（${failures.download.recoverable.count} 条）`,
          ),
          h(
            'div',
            { style: { fontSize: 12, opacity: 0.62, marginTop: 4 } },
            '只重跑风控/超时/磁盘这类值得再试的；404（视频已删）、403（内容受限）不会动。',
          ),
        )
      : null,
    ...notes.map((note, index) => row(note, { key: `n${index}`, opacity: 0.85 })),
    cats.length
      ? h(
          'div',
          { style: { marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 } },
          ...cats.map((cat) =>
            h(
              'div',
              { key: cat.key },
              row(`${cat.label} · ${cat.count} 条`, { fontWeight: 600 }),
              row(cat.hint, { opacity: 0.66 }),
              h(
                'div',
                { style: { fontSize: 11, opacity: 0.55, marginTop: 2 } },
                cat.items
                  .slice(0, 5)
                  .map((it) => `${it.aweme_id} ${it.desc}`)
                  .join('；') + (cat.items.length > 5 ? ` …共 ${cat.items.length} 条` : ''),
              ),
            ),
          ),
        )
      : null,
    images.length
      ? h(
          'div',
          { style: { marginTop: 10 } },
          row(`图集作品 ${images.length} 条（跳过）`, { fontWeight: 600 }),
          h(
            'div',
            { style: { fontSize: 11, opacity: 0.55, marginTop: 2 } },
            images.map((it) => `${it.aweme_id}（${it.images} 张图）`).join('；'),
          ),
        )
      : null,
    noSpeech.length
      ? h(
          'div',
          { style: { marginTop: 10 } },
          row(`无人声作品 ${noSpeech.length} 条（跳过识别）`, { fontWeight: 600 }),
          h(
            'div',
            { style: { fontSize: 11, opacity: 0.55, marginTop: 2 } },
            noSpeech.map((it) => `${it.aweme_id} ${it.desc}`).join('；'),
          ),
        )
      : null,
    missing.length
      ? h('div', { style: { marginTop: 10, fontSize: 12, color: '#d9534f' } },
          `还有 ${missing.length} 条已下载但没转写，点上面的转写按钮补跑。`)
      : null,
  )
}

const STATS_PICK = [
  { key: 'digg', value: 'digg_count', label: '点赞数' },
  { key: 'comment', value: 'comment_count', label: '评论数' },
  { key: 'share', value: 'share_count', label: '转发数' },
  { key: 'collect', value: 'collect_count', label: '收藏数' },
]

function pickedStats(fields) {
  if (!fields) return []
  return STATS_PICK.filter((row) => fields[row.key]).map((row) => row.value)
}

function advSummary(fields, stamps) {
  const n = pickedStats(fields).length
  const parts = []
  if (stamps) parts.push('时间戳')
  if (n) parts.push(`互动数据 ${n} 项`)
  return parts.length ? parts.join(' · ') : '未设置（纯文字）'
}

function engineText(id) {
  if (!id) return '按配置'
  const hit = PICK_ENGINES.find((row) => row.id === id)
  return hit ? hit.text : id
}

async function api(path, options) {
      const response = await fetch(path, {
        headers: { 'content-type': 'application/json' },
        ...options,
      })
      const text = await response.text()
      try {
        return JSON.parse(text)
      } catch {
        return { ok: false, error: text.slice(0, 400) || `HTTP ${response.status}` }
      }
    }

    const FORMATS = ['md', 'txt', 'srt', 'vtt', 'json', 'html', 'docx']

    function label(text, node) {
      return h('div', { style: { fontSize: 12, opacity: 0.62, margin: '14px 0 6px' } }, text, node)
    }

    function btnStyle(kind) {
      const base = {
        cursor: 'pointer',
        border: `1px solid ${BORDER}`,
        borderRadius: 8,
        padding: '7px 14px',
        fontSize: 13,
        background: 'transparent',
        color: 'inherit',
        transition: 'opacity .15s',
      }
      if (kind === 'primary') {
        return { ...base, background: ACCENT, borderColor: ACCENT, color: '#fff', fontWeight: 600 }
      }
      return base
    }

    function Field({ children }) {
      return h(
        'input',
        {
          style: {
            width: '100%',
            boxSizing: 'border-box',
            padding: '8px 10px',
            borderRadius: 8,
            border: `1px solid ${BORDER}`,
            background: 'transparent',
            color: 'inherit',
            fontSize: 13,
            outline: 'none',
          },
          value: children.value,
          onChange: (event) => children.onChange(event.target.value),
          placeholder: children.placeholder,
          type: children.type || 'text',
        },
        null,
      )
    }

    // ------------------------------------------------------------ 标题栏按钮
    function TriggerButton() {
      const open = useOpen()
      return h(
        'button',
        {
          type: 'button',
          onClick: () => setOpen(!open),
          title: '抖音导出：下载无水印视频、转文字、排版导出',
          style: {
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            cursor: 'pointer',
            border: `1px solid ${open ? ACCENT : BORDER}`,
            background: 'transparent',
            color: 'inherit',
            borderRadius: 999,
            padding: '4px 11px',
            fontSize: 12,
            lineHeight: '18px',
          },
        },
        h('span', { style: { fontSize: 13 } }, '🎬'),
        h('span', null, '抖音导出'),
      )
    }

    // ---------------------------------------------------------------- 主面板
    /** 左侧栏的图标：一个线条播放框，跟随主题前景色。 */
    function SidebarGlyph({ size = 18, active }) {
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 24 24',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.7,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': 'true',
          style: { opacity: active ? 1 : 0.72, display: 'block' },
        },
        h('rect', { x: 2.75, y: 5, width: 18.5, height: 14, rx: 3.5 }),
        h('path', { d: 'M10.2 9.2v5.6l4.6-2.8z', fill: 'currentColor', stroke: 'none' }),
      )
    }

    function formatCount(seconds) {
      if (seconds === null || seconds === undefined || !isFinite(seconds)) return ''
      const total = Math.max(0, Math.round(seconds))
      if (total < 60) return `${total} 秒`
      const minutes = Math.floor(total / 60)
      const rest = total % 60
      if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
      return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
    }

    /** 一条任务的进度：细进度条 + 一行数字（第几个/总共、失败、速度、预计剩余）。 */
    function PercentBar({ snapshot, alive }) {
      if (!snapshot) {
        return alive
          ? h('div', { style: { fontSize: 12, opacity: 0.5, marginTop: 4 } }, '准备中…')
          : null
      }
      const total = Number(snapshot.total || 0)
      const processed = Number(snapshot.processed || 0)
      const percent = total ? Math.min(100, Number(snapshot.percent || 0)) : processed ? 100 : 0
      const bits = []
      if (total) bits.push(`${processed}/${total} ${snapshot.unit || ''}`)
      else if (processed) bits.push(`已处理 ${processed} ${snapshot.unit || ''}`)
      if (snapshot.done) bits.push(`成功 ${snapshot.done}`)
      if (snapshot.failed) bits.push(`失败 ${snapshot.failed}`)
      if (snapshot.skipped) bits.push(`跳过 ${snapshot.skipped}`)
      if (snapshot.perSec) bits.push(`${snapshot.perSec}/秒`)
      if (alive && snapshot.etaSec !== null && snapshot.etaSec !== undefined) {
        bits.push(`约剩 ${formatCount(snapshot.etaSec)}`)
      }
      const done = !alive && percent >= 100
      return h(
        'div',
        { style: { marginTop: 6 } },
        h(
          'div',
          {
            style: {
              position: 'relative',
              height: 6,
              borderRadius: 999,
              background: 'var(--dsw-alias-bg-layer-1, rgba(127,127,127,.18))',
              overflow: 'hidden',
            },
          },
          h('div', {
            style: {
              position: 'absolute',
              inset: 0,
              width: `${total ? percent : alive ? 35 : 100}%`,
              background: !alive && snapshot.failed ? '#d9534f' : done ? '#2fa84f' : ACCENT,
              transition: 'width .4s ease',
            },
          }),
        ),
        h(
          'div',
          {
            style: {
              display: 'flex',
              gap: 10,
              flexWrap: 'wrap',
              marginTop: 4,
              fontSize: 12,
              opacity: 0.72,
            },
          },
          snapshot.phase ? h('span', { style: { opacity: 0.9 } }, snapshot.phase) : null,
          ...bits.map((text, index) => h('span', { key: `b${index}` }, text)),
          snapshot.note ? h('span', null, snapshot.note) : null,
        ),
      )
    }

    // ------------------------------------------------------- 四家引擎（平等暴露）
    const PICK_ENGINES = [
      { id: 'sensevoice', text: '本地 SenseVoice（本机运行）' },
      { id: 'openai', text: 'OpenAI 兼容接口' },
      { id: 'dashscope', text: '阿里云百炼' },
      { id: 'volc', text: '豆包（火山引擎）' },
    ]

    const ENGINE_KEYS = [
      {
        id: 'sensevoice',
        title: '本地 SenseVoice',
        hint: '本机 CPU 上跑 int8 模型，不联网，自带标点。首次使用要下载约 228 MB 模型。',
        fields: [
          { key: 'asr.sensevoice.modelDir', label: '模型目录', placeholder: '留空 = 用默认位置' },
          { key: 'asr.sensevoice.threads', label: '线程数', placeholder: '4' },
        ],
      },
      {
        id: 'openai',
        title: 'OpenAI 兼容接口',
        hint: '任何兼容 /audio/transcriptions 的服务都能接：OpenAI、Groq、硅基流动、自建网关…',
        fields: [
          { key: 'asr.openai.baseUrl', label: 'Base URL', placeholder: 'https://api.openai.com/v1' },
          { key: 'asr.openai.model', label: '模型', placeholder: 'whisper-1' },
          { key: 'asr.openai.apiKey', label: 'API Key', placeholder: 'sk-…', secret: true },
        ],
      },
      {
        id: 'dashscope',
        title: '阿里云百炼',
        hint: '词级时间戳，可重建句级；计费按你账号在阿里云百炼的规则走。',
        fields: [
          { key: 'asr.dashscope.apiKey', label: 'API Key', placeholder: 'sk-…', secret: true },
          { key: 'asr.dashscope.model', label: '模型', placeholder: 'qwen-audio-3.1-asr-flash' },
        ],
      },
      {
        id: 'volc',
        title: '豆包（火山引擎）',
        hint: '录音文件识别；它的接口只收公网音频地址——要转本地文件必须填 TOS 桶。',
        fields: [
          { key: 'asr.volc.apiKey', label: 'API Key', placeholder: '…', secret: true },
          { key: 'asr.volc.tier', label: '档位', placeholder: 'express / standard / offpeak' },
          { key: 'asr.volc.tosBucket', label: 'TOS 桶名（可选）', placeholder: 'my-bucket' },
          { key: 'asr.volc.tosRegion', label: 'TOS 区域（可选）', placeholder: 'cn-beijing' },
          { key: 'asr.volc.accessKeyId', label: 'TOS AccessKey ID（可选）', placeholder: '' },
          { key: 'asr.volc.secretAccessKey', label: 'TOS Secret Key（可选）', placeholder: '', secret: true },
        ],
      },
      {
        id: 'polish',
        title: 'AI 校对（可选）',
        hint:
          '转写完让一个大模型挑同音错字，只回「错→对」术语表，再由本地做确定性替换；不会重写正文。'
          + '和语音识别是两套配置，这里填 chat 模型即可。留空 apiKey = 不启用。',
        fields: [
          { key: 'polish.baseUrl', label: 'Base URL', placeholder: 'https://api.openai.com/v1' },
          { key: 'polish.model', label: '模型', placeholder: 'gpt-4o-mini' },
          { key: 'polish.apiKey', label: 'API Key', placeholder: 'sk-…', secret: true },
        ],
      },
    ]

    function pick(object, dotted) {
      let node = object
      for (const part of dotted.split('.')) {
        if (!node || typeof node !== 'object') return undefined
        node = node[part]
      }
      return node
    }

    function EngineKeys(props) {
      const config = props.config || {}
      const [draft, setDraft] = useState({})
      const [busy, setBusy] = useState('')
      const [note, setNote] = useState(null)

      const save = async (field) => {
        const value = draft[field.key]
        if (value === undefined) return
        setBusy(field.key)
        setNote(null)
        const result = await api('/api/douyin/settings', {
          method: 'POST',
          body: JSON.stringify({ key: field.key, value }),
        })
        setBusy('')
        if (result && result.ok) {
          setNote({ kind: 'ok', text: field.label + ' 已保存' })
          setDraft((current) => {
            const next = { ...current }
            delete next[field.key]
            return next
          })
          try {
            if (typeof store.refresh === 'function') store.refresh()
          } catch {
            /* ignore */
          }
        } else {
          setNote({ kind: 'error', text: (result && result.error) || '保存失败' })
        }
      }

      return h(
        'div',
        { style: { display: 'grid', gap: 12 } },
        note
          ? h(
              'div',
              { style: { fontSize: 12, color: note.kind === 'ok' ? '#2fa84f' : '#d9534f' } },
              note.text,
            )
          : null,
        ...ENGINE_KEYS.map((group) =>
          h(
            'div',
            { key: group.id, style: { border: '1px solid ' + BORDER, borderRadius: 10, padding: 12 } },
            h('div', { style: { fontSize: 13, fontWeight: 600 } }, group.title),
            h(
              'div',
              { style: { fontSize: 12, opacity: 0.62, marginTop: 4, lineHeight: 1.5 } },
              group.hint,
            ),
            ...group.fields.map((field) => {
              const current = pick(config, field.key)
              const shown = current === undefined || current === null ? '' : String(current)
              const typed = draft[field.key]
              return h(
                'div',
                { key: field.key, style: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 } },
                h('div', { style: { width: 156, fontSize: 12, opacity: 0.82 } }, field.label),
                h('input', {
                  value: typed === undefined ? '' : typed,
                  placeholder: shown || field.placeholder || '',
                  onChange: (event) =>
                    setDraft((current2) => {
                      const next = { ...current2 }
                      next[field.key] = event.target.value
                      return next
                    }),
                  onKeyDown: (event) => {
                    if (event.key === 'Enter') save(field)
                  },
                  style: {
                    flex: 1,
                    minWidth: 120,
                    padding: '6px 8px',
                    borderRadius: 7,
                    border: '1px solid ' + BORDER,
                    background: 'transparent',
                    color: 'inherit',
                    fontSize: 12,
                    outline: 'none',
                  },
                }),
                h(
                  'button',
                  {
                    type: 'button',
                    onClick: () => save(field),
                    disabled: busy === field.key || typed === undefined,
                    style: {
                      ...btnStyle('plain'),
                      fontSize: 12,
                      padding: '5px 10px',
                      opacity: typed === undefined ? 0.45 : 1,
                    },
                  },
                  busy === field.key ? '保存中' : '保存',
                ),
              )
            }),
          ),
        ),
        h(
          'div',
          { style: { fontSize: 12, opacity: 0.6, lineHeight: 1.6 } },
          '留空 = 保持原值。密钥只存在你自己的配置目录，插件包里不含任何密钥。',
        ),
      )
    }

    // --------------------------------------------------- 顶部实时进度 / 缺 Key 提示
    const ENGINE_KEY_PATH = {
      openai: 'asr.openai.apiKey',
      dashscope: 'asr.dashscope.apiKey',
      volc: 'asr.volc.apiKey',
    }
    const ENGINE_NAMES = { openai: 'OpenAI 兼容接口', dashscope: '阿里云百炼', volc: '豆包' }

    function engineHint(engineId, config) {
      const path = ENGINE_KEY_PATH[engineId || '']
      if (!path) return ''
      const value = pick(config || {}, path)
      if (value) return ''
      return '还没填 ' + ENGINE_NAMES[engineId] + ' 的 API Key —— 到「设置 & 登录」页填一下就能用。'
    }

    function jobPercent(job) {
      const snap = job && job.snapshot
      if (!snap || !snap.total) return null
      const value = Number(snap.percent)
      return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null
    }

    function ActiveJobPill(props) {
      const jobs = Array.isArray(props.jobs) ? props.jobs.filter((item) => item && item.alive) : []
      const job = jobs.find((item) => jobPercent(item) !== null) || jobs[0]
      if (!job) return null
      const pct = jobPercent(job)
      const snap = job.snapshot || {}
      const detail = snap.total
        ? `${snap.processed || 0}/${snap.total}${snap.unit || ''}`
        : '启动中'
      return h(
        'div',
        {
          style: {
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 12,
            minWidth: 200,
            maxWidth: 380,
            opacity: 0.92,
          },
          title: job.label || '',
        },
        h('span', { style: { whiteSpace: 'nowrap', opacity: 0.75 } }, job.label || '任务'),
        h(
          'div',
          {
            style: {
              flex: 1,
              minWidth: 60,
              height: 6,
              borderRadius: 3,
              background: 'rgba(128,128,128,.22)',
              overflow: 'hidden',
            },
          },
          h('div', {
            style: {
              width: (pct === null ? 30 : pct) + '%',
              height: '100%',
              background: ACCENT,
              transition: 'width .4s',
            },
          }),
        ),
        h(
          'span',
          { style: { whiteSpace: 'nowrap', opacity: 0.85 } },
          pct === null ? detail : Math.round(pct) + '%',
        ),
      )
    }

    function PolishCard({ state }) {
      const [terms, setTerms] = useState([])
      const [patches, setPatches] = useState([])
      const [busy, setBusy] = useState('')
      const [msg, setMsg] = useState('')
      const [sample, setSample] = useState(15)
      const [scope, setScope] = useState('author')
      const [author, setAuthor] = useState('')
      const [task, setTask] = useState('')
      const [ids, setIds] = useState('')
      const [models, setModels] = useState([])
      const [modelKey, setModelKey] = useState('')
      const [modelMsg, setModelMsg] = useState('')

      const authors = ((state && state.stats && state.stats.authors) || [])
      const tasks = ((state && state.tasks) || []).filter((item) => item.status !== 'cancelled')

      const body = useCallback(() => {
        if (scope === 'task') return { task }
        if (scope === 'ids') return { ids: ids.split(/[\s,;，；]+/).filter(Boolean) }
        return { author }
      }, [scope, author, task, ids])

      const scopeText = useMemo(() => {
        if (scope === 'task') {
          const hit = tasks.find((item) => item.id === task)
          return hit ? hit.title : '（还没选任务）'
        }
        if (scope === 'ids') {
          const n = ids.split(/[\s,;，；]+/).filter(Boolean).length
          return n ? `${n} 篇指定作品` : '（还没填作品）'
        }
        const hit = authors.find((item) => item.sec_uid === author)
        return hit ? hit.nickname || hit.sec_uid : '全部作者'
      }, [scope, author, task, ids, authors, tasks])

      const load = useCallback(async () => {
        const result = await api('/api/douyin/polish')
        if (result && result.ok) {
          setTerms(result.terms || [])
          setPatches(result.patches || [])
        }
      }, [])

      const loadModels = useCallback(async () => {
        try {
          const result = await api('/api/douyin/llm-models')
          if (!result || !result.ok) return
          setModels(result.routes || [])
          const current = result.current
          const cfg = (state && state.config && state.config.polish) || {}
          const wanted = cfg.provider && cfg.model ? `${cfg.provider}|${cfg.model}` : ''
          setModelKey(
            wanted
            || (current ? `${current.provider}|${current.model}` : ''),
          )
        } catch {
          /* 无模型服务时静默 */
        }
      }, [state && state.config])

      const pickModel = useCallback(
        async (value) => {
          setModelKey(value)
          setModelMsg('')
          const [provider, model] = value.includes('|') ? value.split('|') : ['', '']
          try {
            await api('/api/douyin/settings', {
              method: 'POST',
              body: JSON.stringify({ key: 'polish.provider', value: provider }),
            })
            await api('/api/douyin/settings', {
              method: 'POST',
              body: JSON.stringify({ key: 'polish.model', value: model }),
            })
            setModelMsg(provider ? `已切到 ${value.replace('|', ' / ')}` : '已恢复默认模型')
          } catch (error) {
            setModelMsg('切换失败：' + ((error && error.message) || error))
          }
        },
        [],
      )

      useEffect(() => {
        load()
        loadModels()
      }, [load, loadModels])

      const call = useCallback(
        async (action, extra) => {
          setBusy(action)
          setMsg('')
          try {
            const result = await api('/api/douyin/polish/' + action, {
              method: 'POST',
              body: JSON.stringify(extra || body()),
            })
            if (!result || (result.ok === false && result.error)) {
              setMsg('失败：' + String(result.error || '未知错误'))
            }
            if (result && result.ok) {
              if (result.terms) setTerms(result.terms)
              const mine = result.mine
              if (action === 'mine') {
                setMsg(
                  `抽样 ${mine && mine.sampled ? mine.sampled : 0} 条，新候选 ${(mine && mine.terms ? mine.terms : []).length} 个`
                    + (mine && mine.filtered ? `；自动跳过 ${mine.filtered} 个重复/有害条目` : ''),
                )
              } else if (action === 'apply') {
                setMsg(`已在「${scopeText}」应用：${(result.apply && result.apply.changed) || 0} 条有改动`)
              } else if (action === 'reset') {
                setMsg(`已撤销：${result.reset || 0} 条回到原始转写`)
              } else if (action === 'accept-local') {
                setMsg(
                  `只改这处：影响 ${result.videos || 0} 条，改写 ${result.changed || 0} 处`
                    + (result.videos ? '' : '（没匹配到原句，可能已被改过）'),
                )
              } else {
                setMsg('已更新术语表')
              }
              await load()
            } else {
              setMsg(String((result && result.error) || '操作失败'))
            }
          } catch (err) {
            setMsg('请求失败：' + String((err && err.message) || err))
          } finally {
            setBusy('')
          }
        },
        [body, load, scopeText],
      )

      const manualTerms = terms.filter((t) => t.source === 'manual')
      const candidates = terms.filter((t) => t.source !== 'manual' && !t.covered)
      const covered = terms.filter((t) => t.covered)
      const strong = candidates.filter((t) => (t.videos || 1) >= 2)
      const acceptMany = (list) =>
        call('accept', { author: scope === 'author' ? author : '', pairs: list.map((t) => `${t.wrong}=>${t.right}`) })

      const row = (term, action) =>
        h(
          'div',
          {
            key: term.wrong + term.right,
            style: {
              display: 'flex',
              gap: 10,
              alignItems: 'flex-start',
              fontSize: 12,
              padding: '4px 0',
              borderTop: `1px solid ${BORDER}`,
            },
          },
          h(
            'div',
            { style: { flex: 1, minWidth: 0 } },
            h(
              'div',
              { style: { display: 'flex', gap: 10, alignItems: 'center' } },
              h('span', { style: { minWidth: 160 } }, `${term.wrong} → ${term.right}`),
              h('span', { style: { opacity: 0.55 } }, `${term.videos || 1} 条 / ${term.hits || 1} 次`),
            ),
            term.context
              ? h(
                  'div',
                  {
                    title: term.context,
                    style: {
                      opacity: 0.6,
                      fontSize: 11,
                      marginTop: 2,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    },
                  },
                  `原句：${term.context}`,
                )
              : null,
          ),
          action,
        )

      return h(
        'div',
        null,
        h('div', { style: { fontSize: 13, fontWeight: 600, marginBottom: 4 } }, 'AI 校对（可选，不自动跑）'),
        h(
          'div',
          { style: { fontSize: 12, opacity: 0.7, lineHeight: 1.7, marginBottom: 10 } },
          '下载 / 转写 / 导出都不会自动触发校对。这里先选范围，再挖候选、逐条或整批采纳，'
            + '最后点「应用」才把术语替换进导出文本（原始转写和时间戳都不动）。随时可撤销。',
        ),

        // 校对模型：默认跟随 DSH 自带官方模型，不需要任何 Key
        h(
          'div',
          { style: { display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginBottom: 8 } },
          h('span', { style: { fontSize: 12, opacity: 0.7 } }, '校对模型'),
          h(
            'select',
            { value: modelKey, onChange: (event) => pickModel(event.target.value), style: SEL_STYLE },
            h('option', { value: '' }, '默认（DSH 自带模型）'),
            ...models.map((item) =>
              h(
                'option',
                { key: `${item.provider}|${item.model}`, value: `${item.provider}|${item.model}` },
                item.label,
              ),
            ),
          ),
          h('span', { style: { fontSize: 11, opacity: 0.55 } }, '默认用 DSH 自己的模型，不用填 Key'),
          modelMsg ? h('span', { style: { fontSize: 11, opacity: 0.7 } }, modelMsg) : null,
        ),

        // 范围
        h(
          'div',
          { style: { display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginBottom: 8 } },
          h('span', { style: { fontSize: 12, opacity: 0.7 } }, '范围'),
          h(
            'select',
            { value: scope, onChange: (event) => setScope(event.target.value), style: SEL_STYLE },
            h('option', { value: 'author' }, '按作者'),
            h('option', { value: 'task' }, '按任务'),
            h('option', { value: 'ids' }, '单篇 / 多篇'),
          ),
          scope === 'author'
            ? h(
                'select',
                { value: author, onChange: (event) => setAuthor(event.target.value), style: SEL_STYLE },
                h('option', { value: '' }, '全部作者'),
                ...authors.map((item) =>
                  h('option', { key: item.sec_uid, value: item.sec_uid }, item.nickname || item.sec_uid),
                ),
              )
            : null,
          scope === 'task'
            ? h(
                'select',
                { value: task, onChange: (event) => setTask(event.target.value), style: SEL_STYLE },
                h('option', { value: '' }, '选择任务…'),
                ...tasks.map((item) =>
                  h('option', { key: item.id, value: item.id }, `${item.title || item.id}`),
                ),
              )
            : null,
          scope === 'ids'
            ? h('input', {
                value: ids,
                placeholder: '粘作品链接或 ID，一行/逗号分隔',
                onChange: (event) => setIds(event.target.value),
                style: { ...SEL_STYLE, minWidth: 320 },
              })
            : null,
        ),

        // 操作
        h(
          'div',
          { style: { display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginBottom: 10 } },
          h('input', {
            type: 'number',
            min: 3,
            max: 60,
            value: sample,
            onChange: (event) => setSample(Number(event.target.value) || 15),
            style: { ...SEL_STYLE, width: 72 },
          }),
          h(
            'button',
            { style: btnStyle('ghost'), disabled: !!busy, onClick: () => call('mine', { ...body(), sample }) },
            busy === 'mine' ? '挖取中…' : '挖一轮候选',
          ),
          h(
            'button',
            { style: btnStyle('primary'), disabled: !!busy, onClick: () => call('apply') },
            busy === 'apply' ? '应用中…' : `应用（${manualTerms.length} 条术语 → ${scopeText}）`,
          ),
          h(
            'button',
            { style: btnStyle('ghost'), disabled: !!busy, onClick: () => call('reset') },
            busy === 'reset' ? '撤销中…' : '撤销校对（全库）',
          ),
          msg ? h('span', { style: { fontSize: 12, opacity: 0.7 } }, msg) : null,
        ),

        h('div', { style: { fontSize: 12, fontWeight: 600, margin: '6px 0' } }, `已采纳（${manualTerms.length}）`),
        manualTerms.length
          ? h(
              'div',
              { style: { maxHeight: 160, overflow: 'auto', marginBottom: 12 } },
              ...manualTerms.map((term) => row(term, h('span', { style: { color: '#2fa84f' } }, '生效中'))),
            )
          : h('div', { style: { fontSize: 12, opacity: 0.5, marginBottom: 12 } }, '还没有采纳的术语'),

        h(
          'div',
          { style: { display: 'flex', gap: 8, alignItems: 'center', margin: '6px 0' } },
          h('div', { style: { fontSize: 12, fontWeight: 600 } }, `候选（${candidates.length}）`),
          strong.length
            ? h(
                'button',
                { style: btnStyle('ghost'), disabled: !!busy, onClick: () => acceptMany(strong) },
                `一键采纳高置信 ${strong.length} 条`,
              )
            : null,
        ),
        candidates.length
          ? h(
              'div',
              { style: { maxHeight: 240, overflow: 'auto' } },
              ...candidates.map((term) =>
                row(
                  term,
                  h(
                    'span',
                    { style: { display: 'flex', gap: 6 } },
                    h(
                      'button',
                      { style: btnStyle('ghost'), disabled: !!busy, onClick: () => acceptMany([term]) },
                      '全局采纳',
                    ),
                    term.context
                      ? h(
                          'button',
                          {
                            style: btnStyle('ghost'),
                            disabled: !!busy,
                            title: '只改原句里的这一处，不会影响别的视频',
                            onClick: () =>
                              call('accept-local', {
                                items: [{ wrong: term.wrong, right: term.right, context: term.context }],
                              }),
                          },
                          '只改这处',
                        )
                      : null,
                    h(
                      'button',
                      {
                        style: btnStyle('ghost'),
                        disabled: !!busy,
                        onClick: () =>
                          call('drop', { author: scope === 'author' ? author : '',
                                         pairs: [`${term.wrong}=>${term.right}`] }),
                      },
                      '丢掉',
                    ),
                  ),
                ),
              ),
            )
          : h('div', { style: { fontSize: 12, opacity: 0.5 } }, '没有待定候选'),
        covered.length
          ? h(
              'div',
              { style: { fontSize: 11, opacity: 0.5, marginTop: 8 } },
              `另有 ${covered.length} 个候选与已采纳条目重复，已隐藏`,
            )
          : null,
        h(
          'div',
          { style: { fontSize: 12, fontWeight: 600, margin: '14px 0 6px' } },
          `逐处修正（${patches.length}）`,
        ),
        patches.length
          ? h(
              'div',
              { style: { maxHeight: 180, overflow: 'auto' } },
              ...patches.map((item, index) =>
                h(
                  'div',
                  {
                    key: `${item.aweme_id}-${item.wrong}-${index}`,
                    style: { fontSize: 11, padding: '3px 0', opacity: 0.8 },
                  },
                  `${item.wrong} → ${item.right}　`,
                  h('span', { style: { opacity: 0.6 } }, `（${item.aweme_id}）${item.context || ''}`),
                ),
              ),
            )
          : h(
              'div',
              { style: { fontSize: 11, opacity: 0.5 } },
              '还没有逐处修正。候选里点「只改这处」，只在那一句里替换。',
            ),
      )
    }
    function Panel() {
      const open = true
      const [tab, setTab] = useState('new')
      const [text, setText] = useState('')
      const [mode, setMode] = useState('download')
      const [engine, setEngine] = useState('')
      const [formats, setFormats] = useState(['md'])
      const [merge, setMerge] = useState(true)
      const [advOpen, setAdvOpen] = useState(false)
      const [advFields, setAdvFields] = useState({ digg: false, comment: false, share: false, collect: false })
      const [advStamps, setAdvStamps] = useState(false)
      const [advLink, setAdvLink] = useState(false)
      const [advPublished, setAdvPublished] = useState(false)
      const [advOrder, setAdvOrder] = useState('desc')
      const [submitting, setSubmitting] = useState(false)
      const [notice, setNotice] = useState(null)
      const [state, setState] = useState(null)
      const [engines, setEngines] = useState([])
      const [job, setJob] = useState(null)
      const [videos, setVideos] = useState([])
      const [libFilter, setLibFilter] = useState({ author: '', task: '', status: '', q: '' })
      const [libTotal, setLibTotal] = useState(null)
      const [libSel, setLibSel] = useState([])
      const [polishBusy, setPolishBusy] = useState('')

      const polishIds = useCallback(
        async (ids) => {
          const list = (ids || []).filter(Boolean)
          if (!list.length) return
          setPolishBusy(list.length > 1 ? `批量 ${list.length} 篇` : list[0])
          try {
            const result = await api('/api/douyin/polish/mine', {
              method: 'POST',
              body: JSON.stringify({
                ids: list,
                sample: Math.max(6, Math.min(30, list.length * 2)),
              }),
            })
            if (result && result.ok) {
              const mine = result.mine || {}
              setNotice(
                `已发起校对：${mine.sampled || list.length} 篇，新候选 ${(mine.terms || []).length} 个`
                  + (mine.filtered ? `（自动过滤 ${mine.filtered} 个）` : '')
                  + ' · 到「校对」页签逐条采纳后点应用',
              )
              setTab('polish')
            } else {
              setNotice(String((result && result.error) || '发起校对失败'))
            }
          } finally {
            setPolishBusy('')
          }
        },
        [setTab],
      )
      const [tasks, setTasks] = useState([])
      const [taskView, setTaskView] = useState(null)
      const [taskBusy, setTaskBusy] = useState(false)
      const [login, setLogin] = useState(null)
      const [qrTick, setQrTick] = useState(Date.now())
      const timer = useRef(null)

      const refresh = useCallback(async () => {
        const [stateResult, enginesResult] = await Promise.all([
          api('/api/douyin/state'),
          engines.length ? Promise.resolve(null) : api('/api/douyin/engines'),
        ])
        if (stateResult && stateResult.ok) {
          setState(stateResult)
          setLogin(stateResult.login || null)
        }
        if (enginesResult && enginesResult.ok && Array.isArray(enginesResult.engines)) {
          setEngines(enginesResult.engines)
        }
        api('/api/douyin/tasks').then((result) => {
          if (result && result.ok) setTasks(result.tasks || [])
        })
      }, [engines.length])

      useEffect(() => {
        if (!open) return undefined
        refresh()
        timer.current = setInterval(refresh, 4000)
        store.refresh = refresh
        return () => clearInterval(timer.current)
      }, [open, refresh])

      useEffect(() => {
        if (!open || tab !== 'library') return undefined
        const params = new URLSearchParams({ limit: '60' })
        if (libFilter.author) params.set('author', libFilter.author)
        if (libFilter.task) params.set('task', libFilter.task)
        if (libFilter.status) params.set('status', libFilter.status)
        if (libFilter.q) params.set('q', libFilter.q)
        api('/api/douyin/videos?' + params.toString()).then((result) => {
          if (result && result.ok) {
            setVideos(result.videos || result.items || [])
            setLibTotal(typeof result.total === 'number' ? result.total : null)
          }
        })
        return undefined
      }, [open, tab, libFilter, state && state.stats && state.stats.videos])

      useEffect(() => {
        if (!open || tab !== 'jobs' || !taskView) return undefined
        const id = taskView.task.id
        const tick = setInterval(() => {
          api(`/api/douyin/tasks/${encodeURIComponent(id)}`).then((result) => {
            if (result && result.ok) setTaskView(result)
          })
        }, 3000)
        return () => clearInterval(tick)
      }, [open, tab, taskView && taskView.task && taskView.task.id])

      const submit = async () => {
        if (!text.trim()) {
          setNotice({ kind: 'error', text: '先粘一个链接或抖音号吧' })
          return
        }
        setSubmitting(true)
        setNotice(null)
        const result = await api('/api/douyin/task', {
          method: 'POST',
          body: JSON.stringify({ text, mode, engine, formats, merge, statsFields: pickedStats(advFields), timestamps: advStamps,
            withLink: advLink, published: advPublished, mergeOrder: advOrder, full: mode === 'full' }),
        })
        setSubmitting(false)
        if (result.ok) {
          setJob(result.job)
          setNotice({ kind: 'ok', text: `任务已启动：${result.task ? result.task.id : result.job.id}（后台运行，可以关掉面板）` })
          setTab('jobs')
          if (result.task) openTask(result.task.id)
          refresh()
        } else {
          setNotice({ kind: 'error', text: result.error || '启动失败' })
        }
      }

      const startLogin = async () => {
        const result = await api('/api/douyin/login', {
          method: 'POST',
          body: JSON.stringify({ waitSeconds: 300 }),
        })
        if (result.ok) {
          setLogin({ state: 'waiting' })
          setQrTick(Date.now())
          setNotice({ kind: 'ok', text: '已打开登录，请用抖音 App 扫码（二维码过期会自动刷新）' })
        } else {
          setNotice({ kind: 'error', text: result.error || '登录启动失败' })
        }
      }

      const openJob = async (id) => {
        const result = await api(`/api/douyin/job?id=${encodeURIComponent(id)}`)
        if (result.ok) setJob(result.job)
      }

      const openTask = async (id) => {
        const result = await api(`/api/douyin/tasks/${encodeURIComponent(id)}`)
        if (result && result.ok) setTaskView(result)
        else setNotice({ kind: 'error', text: (result && result.error) || '读不到这个任务' })
      }

      const taskAction = async (action, extra) => {
        if (!taskView) return
        setTaskBusy(true)
        const payload = {
          ...(action === 'transcribe' ? { engine, formats } : { formats, statsFields: pickedStats(advFields), timestamps: advStamps, withLink: advLink, published: advPublished, mergeOrder: advOrder }),
          ...(extra || {}),
        }
        const result = await api(
          `/api/douyin/tasks/${encodeURIComponent(taskView.task.id)}/${action}`,
          { method: 'POST', body: JSON.stringify(payload) },
        )
        setTaskBusy(false)
        if (result && result.ok) {
          setNotice({
            kind: 'ok',
            text: `${action === 'transcribe' ? '转写' : '导出'}已提交：${result.job.id}`,
          })
          openTask(taskView.task.id)
          refresh()
        } else {
          setNotice({ kind: 'error', text: (result && result.error) || '提交失败' })
        }
      }

      const cancelJobById = async (id) => {
        const result = await api('/api/douyin/cancel', {
          method: 'POST',
          body: JSON.stringify({ id }),
        })
        if (result && result.ok) setNotice({ kind: 'ok', text: '已请求取消' })
        else setNotice({ kind: 'error', text: (result && result.error) || '取消失败' })
      }

      if (!open) return null

      const stats = (state && state.stats && (state.stats.stats || state.stats)) || null
      const jobs = (state && state.jobs) || []
      const loggedIn = !!(login && login.state === 'loggedIn')

      const tabs = [
        ['new', '新建任务'],
        ['jobs', '任务'],
        ['library', '资料库'],
        ['polish', '校对'],
        ['account', '设置 / 登录'],
      ]

      return h(
        'div',
        {
          'aria-label': '抖音导出',
          style: {
            height: '100%',
            minHeight: 420,
            display: 'flex',
            flexDirection: 'column',
            background: SURFACE,
            overflow: 'hidden',
          },
        },
        h(
          'div',
          {
            style: {
              flex: 1,
              minHeight: 0,
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
            },
          },
          // 头部
          h(
            'div',
            {
              style: {
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                padding: '12px 16px',
                borderBottom: `1px solid ${BORDER}`,
                background: SURFACE_TOP,
              },
            },
            h('span', { style: { fontSize: 16 } }, '🎬'),
            h('strong', { style: { fontSize: 14 } }, '抖音导出'),
            h(
              'span',
              { style: { fontSize: 12, opacity: 0.6 } },
              loggedIn ? '已登录' : '未登录（登录后可同步作者全部作品）',
            ),
            h(ActiveJobPill, { jobs: state && state.jobs }),
            h('div', { style: { flex: 1 } }),
            stats
              ? h(
                  'span',
                  { style: { fontSize: 12, opacity: 0.7, whiteSpace: 'nowrap' } },
                  `作品 ${stats.videos ?? '-'} · 已下载 ${stats.downloaded ?? '-'} · 已转写 ${stats.transcribed ?? '-'}`,
                )
              : null,
          ),
          // 标签栏
          h(
            'div',
            {
              style: {
                display: 'flex',
                gap: 4,
                padding: '8px 12px 0',
                borderBottom: `1px solid ${BORDER}`,
              },
            },
            ...tabs.map(([id, name]) =>
              h(
                'button',
                {
                  key: id,
                  type: 'button',
                  onClick: () => setTab(id),
                  style: {
                    cursor: 'pointer',
                    border: 'none',
                    borderBottom: `2px solid ${tab === id ? ACCENT : 'transparent'}`,
                    background: 'transparent',
                    color: 'inherit',
                    padding: '8px 12px',
                    fontSize: 13,
                    fontWeight: tab === id ? 600 : 400,
                    opacity: tab === id ? 1 : 0.7,
                  },
                },
                name,
              ),
            ),
          ),
          // 内容
          h(
            'div',
            {
              style: {
                flex: 1,
                overflow: 'auto',
                padding: '20px 24px 32px',
                width: '100%',
                maxWidth: 1020,
                boxSizing: 'border-box',
              },
            },
            notice
              ? h(
                  'div',
                  {
                    style: {
                      marginBottom: 12,
                      padding: '8px 12px',
                      borderRadius: 8,
                      fontSize: 12,
                      border: `1px solid ${notice.kind === 'error' ? '#d9534f' : BORDER}`,
                      background: notice.kind === 'error' ? 'rgba(217,83,79,.12)' : 'transparent',
                    },
                  },
                  notice.text,
                )
              : null,

            // ---- 新建任务
            tab === 'new'
              ? h(
                  'div',
                  null,
                  label('粘贴链接：视频链接 / 分享口令 / 抖音号 / 作者主页，一行一个'),
                  h('textarea', {
                    value: text,
                    onChange: (event) => setText(event.target.value),
                    placeholder:
                      'https://v.douyin.com/xxxxxxx/\nhttps://www.douyin.com/user/MS4wLjABAAAA……\n抖音号：xxxxxx\n7.58 复制打开抖音…',
                    rows: 7,
                    style: {
                      width: '100%',
                      boxSizing: 'border-box',
                      padding: 10,
                      borderRadius: 8,
                      border: `1px solid ${BORDER}`,
                      background: 'transparent',
                      color: 'inherit',
                      fontSize: 13,
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                      resize: 'vertical',
                      outline: 'none',
                    },
                  }),
                  label('做什么'),
                  h(
                    'div',
                    { style: { display: 'flex', gap: 16, fontSize: 13 } },
                    ...['download', 'full'].map((id) =>
                      h(
                        'label',
                        { key: id, style: { display: 'flex', gap: 6, alignItems: 'center', cursor: 'pointer' } },
                        h('input', {
                          type: 'radio',
                          checked: mode === id,
                          onChange: () => setMode(id),
                        }),
                        id === 'download' ? '只下载无水印视频' : '下载 + 转写 + 排版导出',
                      ),
                    ),
                  ),
                  mode === 'full'
                    ? h(
                        'div',
                        null,
                        label('转写引擎'),
                        h(
                          'select',
                          {
                            value: engine,
                            onChange: (event) => setEngine(event.target.value),
                            style: {
                              width: '100%',
                              padding: '8px 10px',
                              borderRadius: 8,
                              border: `1px solid ${BORDER}`,
                              background: 'transparent',
                              color: 'inherit',
                              fontSize: 13,
                            },
                          },
                          h('option', { value: '' }, '跟随配置（默认）'),
                          ...PICK_ENGINES.map((row) =>
                            h('option', { key: row.id, value: row.id }, row.text),
                          ),
                        ),
                        engineHint(engine, state && state.config)
                          ? h(
                              'div',
                              { style: { fontSize: 12, color: '#d9534f', marginTop: 6 } },
                              engineHint(engine, state && state.config),
                            )
                          : null,
                        label('导出格式'),
                        h(
                          'div',
                          { style: { display: 'flex', flexWrap: 'wrap', gap: 12, fontSize: 13 } },
                          ...FORMATS.map((id) =>
                            h(
                              'label',
                              { key: id, style: { display: 'flex', gap: 5, alignItems: 'center', cursor: 'pointer' } },
                              h('input', {
                                type: 'checkbox',
                                checked: formats.includes(id),
                                onChange: () => {
                                  setFormats((current) =>
                                    current.includes(id)
                                      ? current.filter((item) => item !== id)
                                      : [...current, id],
                                  )
                                },
                              }),
                              id.toUpperCase(),
                            ),
                          ),
                        ),
                        h(
                          'label',
                          { style: { display: 'flex', gap: 7, alignItems: 'center', cursor: 'pointer', fontSize: 13, marginTop: 10 } },
                          h('input', {
                            type: 'checkbox',
                            checked: merge,
                            onChange: () => setMerge((v) => !v),
                          }),
                          '同时输出合集',
                          h('span', { style: { fontSize: 12, opacity: 0.6 } }, '（多条合并成一个文件，含目录与总时长：MD/TXT/HTML/DOCX）'),
                        ),
                        h(
                          'div',
                          { style: { display: 'flex', gap: 10, alignItems: 'center', marginTop: 2 } },
                          h(
                            'button',
                            { type: 'button', onClick: () => setAdvOpen(true), style: btnStyle('ghost') },
                            '导出高级设置',
                          ),
                          h(
                            'span',
                            { style: { fontSize: 12, opacity: 0.65 } },
                            advSummary(advFields, advStamps),
                          ),
                        ),
                        advOpen
                          ? h(
                            'div',
                            {
                              style: {
                                position: 'fixed', inset: 0, zIndex: 70, background: 'rgba(0,0,0,0.45)',
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                              },
                              onClick: () => setAdvOpen(false),
                            },
                            h(
                              'div',
                              {
                                style: {
                                  width: 380, maxWidth: '92vw', borderRadius: 12, padding: '18px 20px 16px',
                                  background: 'var(--dsw-alias-bg-elevated, #ffffff)',
                                  boxShadow: '0 18px 48px rgba(0,0,0,0.32)',
                                  border: '1px solid var(--dsw-alias-line-subtle, rgba(0,0,0,0.08))',
                                },
                                onClick: (ev) => ev.stopPropagation(),
                              },
                              h('strong', { style: { fontSize: 14 } }, '导出高级设置'),
                              h(
                                'div',
                                { style: { fontSize: 12, opacity: 0.62, marginTop: 6, lineHeight: 1.6 } },
                                '默认都不勾，导出就是干净的纯文字。勾选只是把数字写进正文，不会导出评论内容。',
                              ),
                              h('div', { style: { fontSize: 13, fontWeight: 600, marginTop: 16 } }, '时间戳'),
                              h(
                                'label',
                                { style: { display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer', fontSize: 13, marginTop: 8 } },
                                h('input', { type: 'checkbox', checked: advStamps, onChange: () => setAdvStamps((v) => !v) }),
                                '段落前加 [mm:ss]',
                                h('span', { style: { fontSize: 12, opacity: 0.6 } }, '（JSON 永远保留完整时间戳）'),
                              ),
                              h(
                                'label',
                                { style: { display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer', fontSize: 13, marginTop: 8 } },
                                h('input', { type: 'checkbox', checked: advLink, onChange: () => setAdvLink((v) => !v) }),
                                '正文里写原文链接',
                                h('span', { style: { fontSize: 12, opacity: 0.6 } }, '（抖音视频地址；front-matter 里一直有 url）'),
                              ),
                              h(
                                'label',
                                { style: { display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer', fontSize: 13, marginTop: 8 } },
                                h('input', { type: 'checkbox', checked: advPublished, onChange: () => setAdvPublished((v) => !v) }),
                                '正文里写发布日期',
                                h('span', { style: { fontSize: 12, opacity: 0.6 } }, '（front-matter 里一直有，这里管正文引用行）'),
                              ),
                              h('div', { style: { fontSize: 13, fontWeight: 600, marginTop: 16 } }, '合集排序'),
                              h(
                                'div',
                                { style: { display: 'flex', gap: 8, marginTop: 8 } },
                                ...[['desc', '最新在前'], ['asc', '最老在前']].map(([value, text]) => h(
                                  'button',
                                  {
                                    key: value,
                                    type: 'button',
                                    onClick: () => setAdvOrder(value),
                                    style: {
                                      ...btnStyle(advOrder === value ? 'primary' : 'ghost'),
                                      fontSize: 12.5,
                                    },
                                  },
                                  text,
                                )),
                              ),
                              h(
                                'div',
                                { style: { fontSize: 12, opacity: 0.6, marginTop: 6 } },
                                '只影响合并合集里的条目顺序。',
                              ),
                              h('div', { style: { fontSize: 13, fontWeight: 600, marginTop: 16 } }, '互动数据'),
                              h(
                                'div',
                                { style: { display: 'flex', flexWrap: 'wrap', gap: '8px 20px', marginTop: 8 } },
                                ...STATS_PICK.map((row) => h(
                                  'label',
                                  {
                                    key: row.key,
                                    style: { display: 'flex', gap: 7, alignItems: 'center', cursor: 'pointer', fontSize: 13 },
                                  },
                                  h('input', {
                                    type: 'checkbox',
                                    checked: Boolean(advFields[row.key]),
                                    onChange: () => setAdvFields((v) => ({ ...v, [row.key]: !v[row.key] })),
                                  }),
                                  row.label,
                                )),
                              ),
                              h(
                                'div',
                                { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 18 } },
                                h(
                                  'button',
                                  {
                                    type: 'button',
                                    onClick: () => {
                                      setAdvFields({ digg: false, comment: false, share: false, collect: false })
                                      setAdvStamps(false)
                                    },
                                    style: btnStyle('ghost'),
                                  },
                                  '全部不勾',
                                ),
                                h(
                                  'button',
                                  { type: 'button', onClick: () => setAdvOpen(false), style: btnStyle('primary') },
                                  '完成',
                                ),
                              ),
                            ),
                          )
                          : null,
                      )
                    : null,
                  h(
                    'div',
                    { style: { marginTop: 18, display: 'flex', gap: 10, alignItems: 'center' } },
                    h(
                      'button',
                      {
                        type: 'button',
                        onClick: submit,
                        disabled: submitting,
                        style: { ...btnStyle('primary'), opacity: submitting ? 0.6 : 1 },
                      },
                      submitting ? '启动中…' : '开始',
                    ),
                    h(
                      'span',
                      { style: { fontSize: 12, opacity: 0.6 } },
                      '长任务会挂到后台，关掉这个面板也不影响；进度看「任务」页。',
                    ),
                  ),
                )
              : null,

            // ---- 任务：每个任务一个目录，列表 + 详情（可追加转写 / 导出）
            tab === 'jobs'
              ? h(
                  'div',
                  null,
                  taskView
                    ? h(
                        'div',
                        null,
                        h(
                          'div',
                          {
                            style: {
                              display: 'flex',
                              gap: 10,
                              alignItems: 'center',
                              marginBottom: 10,
                              flexWrap: 'wrap',
                            },
                          },
                          h(
                            'button',
                            {
                              type: 'button',
                              onClick: () => setTaskView(null),
                              style: btnStyle('ghost'),
                            },
                            '← 任务列表',
                          ),
                          h('div', { style: { fontSize: 15, fontWeight: 600 } }, taskView.task.title),
                          h(
                            'span',
                            {
                              style: {
                                fontSize: 12,
                                padding: '2px 8px',
                                borderRadius: 999,
                                border: `1px solid ${BORDER}`,
                                opacity: 0.8,
                              },
                            },
                            STATUS_TEXT[taskView.task.status] || taskView.task.status,
                          ),
                        ),
                        h(PercentBar, {
                          snapshot: taskView.snapshot,
                          alive: !!(taskView.task.job && taskView.task.job.alive),
                        }),
                        h(
                          'div',
                          { style: { fontSize: 12, opacity: 0.65, marginTop: 8, lineHeight: 1.8 } },
                          `任务目录：${taskView.task.dir}`,
                          h('br'),
                          `输入：作品 ${taskView.task.input.videos.length} 个 · 作者 ${taskView.task.input.authors.length} 个 · 待解析 ${taskView.task.input.unresolved.length} 行`,
                          h('br'),
                          `创建 ${String(taskView.task.createdAt).replace('T', ' ').slice(0, 19)} · 运行 ${taskView.task.runs} 次`,
                        ),
                        (taskView.task.stages || []).length
                          ? h(
                              'div',
                              { style: { marginTop: 12 } },
                              label('阶段'),
                              h(
                                'div',
                                { style: { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 12 } },
                                ...(taskView.task.stages || []).map((stage) =>
                                  h(
                                    'div',
                                    {
                                      key: stage.key,
                                      style: { display: 'flex', alignItems: 'center', gap: 8 },
                                    },
                                    h('span', { style: { width: 40, opacity: 0.8 } }, stage.label),
                                    h('span', { style: { flex: 1 } }, stageText(stage)),
                                    h(
                                      'span',
                                      {
                                        style: {
                                          fontSize: 11,
                                          opacity: 0.6,
                                          color: stage.status === 'failed' ? '#d9534f' : undefined,
                                        },
                                      },
                                      STATUS_TEXT[stage.status] || stage.status,
                                    ),
                                  ),
                                ),
                              ),
                            )
                          : null,
                        h(FailureCard, {
                          failures: taskView.failures,
                          busy: taskBusy,
                          onRetry: () => taskAction('retry'),
                        }),
                        h(
                          'div',
                          { style: { display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' } },
                          (taskView.task.stages || []).some((stage) => stage.key === 'transcribe')
                            ? null
                            : h(
                                'button',
                                {
                                  type: 'button',
                                  onClick: () => taskAction('transcribe'),
                                  disabled: taskBusy,
                                  style: { ...btnStyle('primary'), opacity: taskBusy ? 0.6 : 1 },
                                },
                                taskBusy ? '提交中…' : `发起转写（${engineText(engine)}）`,
                              ),
                          h(
                            'button',
                            {
                              type: 'button',
                              onClick: () => taskAction('export'),
                              disabled: taskBusy,
                              style: { ...btnStyle('ghost'), opacity: taskBusy ? 0.6 : 1 },
                            },
                            `导出 ${formats.length ? formats.join('/').toUpperCase() : 'MD'}`,
                          ),
                          h(
                            'button',
                            {
                              type: 'button',
                              onClick: () => taskAction('export', { merge: true }),
                              disabled: taskBusy,
                              style: { ...btnStyle('ghost'), opacity: taskBusy ? 0.6 : 1 },
                            },
                            '导出合集',
                          ),
                          h(
                            'button',
                            {
                              type: 'button',
                              onClick: () => taskAction('open'),
                              style: btnStyle('ghost'),
                            },
                            '打开项目目录',
                          ),
                          taskView.task.job && taskView.task.job.alive
                            ? h(
                                'button',
                                {
                                  type: 'button',
                                  onClick: () => cancelJobById(taskView.task.job.id),
                                  style: btnStyle('ghost'),
                                },
                                '取消运行',
                              )
                            : null,
                        ),
                        h(
                          'div',
                          { style: { fontSize: 11.5, opacity: 0.55, marginTop: 8 } },
                          '转写引擎和导出格式跟随「新建任务」页当前的选择。',
                        ),
                        h(
                          'div',
                          { style: { marginTop: 16, display: 'flex', alignItems: 'baseline', gap: 8 } },
                          label(`产出文件（${(taskView.fileStats && taskView.fileStats.total) || (taskView.files || []).length}）`),
                          (taskView.fileStats && taskView.fileStats.total > (taskView.files || []).length)
                            ? h('span', { style: { fontSize: 12, opacity: 0.6 } }, `列出前 ${(taskView.files || []).length} 个`)
                            : null,
                        ),
                        (taskView.files || []).length === 0
                          ? h(
                              'div',
                              { style: { fontSize: 12, opacity: 0.6 } },
                              '还没有产出文件；转写 / 导出完成后会出现在任务目录里。',
                            )
                          : h(
                              'div',
                              { style: { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12.5 } },
                              ...taskView.files.map((file) =>
                                h(
                                  'div',
                                  { key: file.rel, style: { display: 'flex', gap: 8, alignItems: 'center' } },
                                  h(
                                    'span',
                                    {
                                      style: {
                                        flex: 1,
                                        minWidth: 0,
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                      },
                                    },
                                    (file.rel || '').includes('/合集/') ? `📚 ${file.rel}` : file.rel,
                                  ),
                                  h('span', { style: { opacity: 0.5 } }, `${Math.max(1, Math.round(file.size / 1024))} KB`),
                                ),
                              ),
                            ),
                        h('div', { style: { marginTop: 16 } }, label('运行日志')),
                        h(
                          'pre',
                          {
                            style: {
                              margin: 0,
                              maxHeight: 280,
                              overflow: 'auto',
                              fontSize: 12,
                              lineHeight: 1.55,
                              padding: 10,
                              borderRadius: 8,
                              border: `1px solid ${BORDER}`,
                              whiteSpace: 'pre-wrap',
                              wordBreak: 'break-all',
                            },
                          },
                          taskView.log || '（暂无日志）',
                        ),
                      )
                    : h(
                        'div',
                        null,
                        h(
                          'div',
                          { style: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 } },
                          h(
                            'div',
                            { style: { fontSize: 13, fontWeight: 600, opacity: 0.85 } },
                            `共 ${tasks.length} 个任务`,
                          ),
                          h('div', { style: { flex: 1 } }),
                          h(
                            'button',
                            { type: 'button', onClick: () => refresh(), style: btnStyle('ghost') },
                            '刷新',
                          ),
                        ),
                        tasks.length === 0
                          ? h('div', { style: { fontSize: 13, opacity: 0.7 } }, '还没有任务。到「新建任务」发起一个。')
                          : h(
                          'div',
                          { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
                          ...tasks.map((item) =>
                            h(
                              'div',
                              {
                                key: item.id,
                                onClick: () => openTask(item.id),
                                style: {
                                  cursor: 'pointer',
                                  border: `1px solid ${BORDER}`,
                                  borderRadius: 8,
                                  padding: '9px 12px',
                                  display: 'flex',
                                  gap: 10,
                                  alignItems: 'center',
                                  fontSize: 13,
                                },
                              },
                              h(
                                'span',
                                null,
                                item.status === 'running' ? '⏳' : item.status === 'failed' ? '⚠️' : '✅',
                              ),
                              h(
                                'div',
                                { style: { flex: 1, minWidth: 0 } },
                                h(
                                  'div',
                                  { style: { display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' } },
                                  h('span', null, item.title),
                                  h(
                                    'span',
                                    { style: { fontSize: 11, opacity: 0.55 } },
                                    String(item.createdAt).replace('T', ' ').slice(5, 16),
                                  ),
                                  (item.stages || []).length
                                    ? h(
                                        'span',
                                        { style: { fontSize: 11, opacity: 0.75, display: 'flex', gap: 10 } },
                                        ...(item.stages || []).map((stage) =>
                                          h('span', { key: stage.key }, `${stage.label} ${stageText(stage)}`),
                                        ),
                                      )
                                    : h(
                                        'span',
                                        { style: { fontSize: 11, opacity: 0.55 } },
                                        `作品 ${item.input.videos.length} · 作者 ${item.input.authors.length}`,
                                      ),
                                ),
                                h(PercentBar, {
                                  snapshot: item.job && item.job.snapshot,
                                  alive: !!(item.job && item.job.alive),
                                }),
                              ),
                              h('span', { style: { fontSize: 11, opacity: 0.5 } }, item.id),
                            ),
                          ),
                        ),
                      ),
                )
              : null,

            // ---- 校对（独立步骤：不参与下载/转写/导出的自动流程）
            tab === 'polish'
              ? h('div', null, h(PolishCard, { state }))
              : null,

            // ---- 资料库
            tab === 'library'
              ? h(
                  'div',
                  null,
                  stats
                    ? h(
                        'div',
                        { style: { fontSize: 13, marginBottom: 12, opacity: 0.8 } },
                        `作品 ${stats.videos ?? '-'} 个 · 已下载 ${stats.downloaded ?? '-'} · 已转写 ${stats.transcribed ?? '-'} · 导出 ${stats.exports ?? '-'} 次 · 占用 ${stats.bytesHuman || '-'}`,
                      )
                    : null,
                  h(
                    'div',
                    {
                      style: {
                        display: 'flex',
                        flexWrap: 'wrap',
                        gap: 8,
                        alignItems: 'center',
                        marginBottom: 10,
                      },
                    },
                    h(
                      'select',
                      {
                        value: libFilter.author,
                        onChange: (event) => setLibFilter({ ...libFilter, author: event.target.value }),
                        style: SEL_STYLE,
                      },
                      h('option', { value: '' }, '全部作者'),
                      ...(((state && state.stats && state.stats.authors) || []).map((author) =>
                        h(
                          'option',
                          { key: author.sec_uid, value: author.sec_uid },
                          `${author.nickname || author.sec_uid}（${author.aweme_count ?? '?'}）`,
                        ),
                      )),
                    ),
                    h(
                      'select',
                      {
                        value: libFilter.task,
                        onChange: (event) => setLibFilter({ ...libFilter, task: event.target.value }),
                        style: SEL_STYLE,
                      },
                      h('option', { value: '' }, '全部任务'),
                      ...tasks.map((task) =>
                        h('option', { key: task.id, value: task.id }, task.title || task.id),
                      ),
                    ),
                    h(
                      'select',
                      {
                        value: libFilter.status,
                        onChange: (event) => setLibFilter({ ...libFilter, status: event.target.value }),
                        style: SEL_STYLE,
                      },
                      ...LIB_STATUS.map(([value, label]) => h('option', { key: value || 'all', value }, label)),
                    ),
                    h('input', {
                      value: libFilter.q,
                      placeholder: '搜标题 / 作品 ID',
                      onChange: (event) => setLibFilter({ ...libFilter, q: event.target.value }),
                      style: { ...SEL_STYLE, minWidth: 180 },
                    }),
                    libTotal !== null
                      ? h(
                          'span',
                          { style: { fontSize: 11, opacity: 0.6 } },
                          `共 ${libTotal} 条 · 显示 ${videos.length} 条`,
                        )
                      : null,
                    h(
                      'label',
                      {
                        style: {
                          fontSize: 11,
                          opacity: 0.75,
                          display: 'flex',
                          gap: 6,
                          alignItems: 'center',
                        },
                      },
                      h('input', {
                        type: 'checkbox',
                        checked: videos.length > 0 && libSel.length === videos.length,
                        onChange: (event) =>
                          setLibSel(event.target.checked ? videos.map((item) => item.aweme_id) : []),
                      }),
                      '全选本页',
                    ),
                    libSel.length
                      ? h(
                          'button',
                          {
                            style: btnStyle('primary'),
                            disabled: !!polishBusy,
                            onClick: () => polishIds(libSel),
                          },
                          polishBusy ? '校对中…' : `发起校对（已选 ${libSel.length} 篇）`,
                        )
                      : null,
                  ),
                  h(
                    'div',
                    { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
                    ...videos.map((item) =>
                      h(
                        'div',
                        {
                          key: item.aweme_id,
                          style: {
                            border: `1px solid ${BORDER}`,
                            borderRadius: 8,
                            padding: '7px 10px',
                            fontSize: 12,
                            display: 'flex',
                            gap: 10,
                          },
                        },
                        h('input', {
                          type: 'checkbox',
                          checked: libSel.includes(item.aweme_id),
                          onChange: (event) =>
                            setLibSel((prev) =>
                              event.target.checked
                                ? [...prev, item.aweme_id]
                                : prev.filter((id) => id !== item.aweme_id),
                            ),
                          style: { marginTop: 2 },
                        }),
                        h('span', { style: { opacity: 0.55, minWidth: 150 } }, item.aweme_id),
                        h('span', { style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' } }, item.desc || ''),
                        h('span', { style: { opacity: 0.6 } }, item.status || ''),
                        h(
                          'button',
                          {
                            style: { ...btnStyle('ghost'), padding: '2px 8px', fontSize: 11 },
                            disabled: !!polishBusy,
                            onClick: () => polishIds([item.aweme_id]),
                          },
                          '校对',
                        ),
                      ),
                    ),
                  ),
                )
              : null,

            // ---- 设置 / 登录
            tab === 'account'
              ? h(
                  'div',
                  null,
                  h('div', { style: { display: 'flex', gap: 24, flexWrap: 'wrap' } }, 
                    h(
                      'div',
                      { style: { minWidth: 260 } },
                      label('登录（扫码一次，之后一直有效）'),
                      loggedIn
                        ? h('div', { style: { fontSize: 13, color: '#5cb85c' } }, '✅ 已登录，可以同步作者的全部作品了')
                        : h(
                            'div',
                            null,
                            h(
                              'button',
                              { type: 'button', onClick: startLogin, style: btnStyle('primary') },
                              '显示登录二维码',
                            ),
                            login && login.state === 'waiting'
                              ? h(
                                  'div',
                                  { style: { marginTop: 12 } },
                                  h('img', {
                                    src: `/api/douyin/qr.png?t=${qrTick}`,
                                    alt: '抖音登录二维码',
                                    width: 190,
                                    height: 190,
                                    style: {
                                      borderRadius: 8,
                                      background: '#fff',
                                      padding: 6,
                                      display: 'block',
                                    },
                                  }),
                                  h(
                                    'div',
                                    { style: { fontSize: 12, opacity: 0.65, marginTop: 8 } },
                                    '用抖音 App 扫码确认。二维码会自动刷新；扫完这一步就永久生效（存在 Chrome profile 里）。',
                                  ),
                                )
                              : null,
                          ),
                    ),
                    h(
                      'div',
                      { style: { flex: 1, minWidth: 260 } },
                      label('转写引擎（四家平等，用哪家填哪家）'),
                      h(EngineKeys, { config: state && state.config }),
                      h(
                        'div',
                        { style: { fontSize: 12, opacity: 0.6, marginTop: 8 } },
                        `库位置：${(state && state.home) || ''}`,
                      ),
                    ),
                  ),
                )
              : null,
          ),
        ),
      )
    }


    // ------------------------------------------------------- 确认卡（动作卡）
    // 用户在卡片上点「确认执行」/「不用了」，通过 act(verb) 走 DSH 的 Remote 调用，
    // 结果写进会话日志，Agent 下一轮会收到状态通知。
    function ConfirmBody(props) {
      const { card, decision, act } = props
      const [busy, setBusy] = useState(false)
      const details = (card && card.details) || {}
      const phase = decision && decision.phase
      const done = phase === 'accepted' || phase === 'dismissed' || phase === 'replaced'

      const run = async (verb) => {
        setBusy(true)
        try {
          if (typeof act === 'function') await act(verb)
        } finally {
          setBusy(false)
        }
      }

      const chip = (text, color) =>
        h(
          'span',
          {
            style: {
              fontSize: 11,
              padding: '1px 7px',
              borderRadius: 999,
              background: color + '22',
              color,
            },
          },
          text,
        )

      return h(
        'div',
        { style: { display: 'flex', flexDirection: 'column', gap: 10, padding: '12px 14px' } },
        h(
          'div',
          { style: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' } },
          h('span', { style: { fontSize: 13, fontWeight: 600 } }, (card && card.title) || '需要确认'),
          details.op ? chip(String(details.op), '#8a6d3b') : null,
          phase === 'accepted' ? chip('已确认', '#2fa84f') : null,
          phase === 'dismissed' ? chip('已取消', '#8a8a8a') : null,
        ),
        details.target
          ? h(
              'div',
              { style: { fontSize: 12, opacity: 0.72, wordBreak: 'break-all' } },
              '对象：' + String(details.target),
            )
          : null,
        done
          ? null
          : h(
              'div',
              { style: { display: 'flex', gap: 8 } },
              h(
                'button',
                {
                  disabled: busy,
                  onClick: () => run('confirm'),
                  style: {
                    ...btnStyle('primary'),
                    opacity: busy ? 0.6 : 1,
                    cursor: busy ? 'default' : 'pointer',
                  },
                },
                '确认执行',
              ),
              h(
                'button',
                {
                  disabled: busy,
                  onClick: () => run('dismiss'),
                  style: { ...btnStyle('ghost'), opacity: busy ? 0.6 : 1 },
                },
                '不用了',
              ),
            ),
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        const PANEL_ID = 'douyin-export'
        const title = () => '抖音导出'

        // 确认卡的浏览器侧 body（Host 侧 kind 在 action-cards.js）
        // 注意：actionCards 不在 inject 列表里，直接 ctx.get 会抛错并中断整个 apply，
        // 所以必须 try/catch —— 拿不到就只是不注册卡片，绝不能影响左栏入口。
        let cards
        try {
          cards = (typeof ctx.get === 'function' ? ctx.get('actionCards') : undefined) || ctx.actionCards
        } catch {
          cards = undefined
        }
        if (cards && typeof cards.register === 'function') {
          ctx.effect(() =>
            cards.register({ action: 'douyin.confirm', locale: 'douyin-export', Body: ConfirmBody }),
          )
        }

        // 左侧栏一个独立入口：点图标 → 主区域整页打开这个面板。
        ctx.slots.inject('main', () =>
          ctx.slots.register({ name: 'main', key: PANEL_ID }, Panel),
        )
        ctx.slots.inject('sidebar.panellist', () =>
          ctx.slots.register(
            { name: 'sidebar.panellist', id: PANEL_ID, order: 60, label: title },
            SidebarGlyph,
          ),
        )

        // 设置里再放一个同名分区，两处指向同一个组件。
        ctx.slots.inject('settings.section', () =>
          ctx.slots.register(
            { name: 'settings.section', id: PANEL_ID, order: 64, label: title },
            Panel,
          ),
        )
      },
    }
  },
})
