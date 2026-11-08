// 可选确认卡（DSH action-cards 服务的 Host 侧「kind」）。
//
// 用途：删除任务、清空库、覆盖导出这类不可逆操作，先弹一张卡让用户点「确认执行」，
// 而不是 Agent 直接动手。卡片由 Agent 调 `propose_action` 提出（见 skills/douyin-export/SKILL.md），
// 用户点了按钮后 DSH 把决定写进会话日志，并在下一次请求里以状态通知告诉 Agent。
//
// 契约见 @deepseek-ai/dsh-action-cards：
//   ctx.actionCards.register({ action, skill, invitation, details, verbs, act, ... }) -> disposer
//   ctx.actionCards.record(agent, decision)
//   details 只需提供 safeParse(value) -> { success, data } | { success:false, error:{ issues } }

const ACTION = 'douyin.confirm'
const SKILL = 'douyin-export'

const OPS = new Set(['delete-task', 'clear-library', 'overwrite-export', 'publish-video'])

/** 手写一个最小 schema：DSH 只要求 safeParse，不要求真 zod。 */
const details = {
  safeParse(value) {
    const issues = []
    const data = {}
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return { success: false, error: { issues: [{ path: [], message: '必须是对象' }] } }
    }
    if (typeof value.op !== 'string' || !OPS.has(value.op)) {
      issues.push({ path: ['op'], message: `op 必须是 ${[...OPS].join(' / ')} 之一` })
    } else {
      data.op = value.op
    }
    if (typeof value.target !== 'string' || value.target.trim() === '') {
      issues.push({ path: ['target'], message: 'target 不能为空（任务 id、作者 sec_uid 或文件路径）' })
    } else {
      data.target = value.target.trim()
    }
    if (issues.length > 0) return { success: false, error: { issues } }
    return { success: true, data }
  },
}

const OP_TEXT = {
  'delete-task': '删除任务',
  'clear-library': '清空资料库',
  'overwrite-export': '覆盖已有导出',
  'publish-video': '对外发布视频',
}

/**
 * 注册确认卡 kinds。
 * @param {object} ctx Cordis 上下文
 * @returns {(() => void) | undefined} 注销函数；服务不存在时返回 undefined
 */
export function registerActionKinds(ctx) {
  const cards = ctx.get('actionCards')
  if (cards === undefined || typeof cards.register !== 'function') return undefined

  const kind = {
    action: ACTION,
    skill: SKILL,
    invitation:
      '这件事不可逆，需要你点一下确认：卡片里选「确认执行」我就开始，选「不用了」就停在这里。',
    details,
    verbs: {
      confirm: { label: '确认执行', writes: true },
    },
    openPhases: [],
    statusGuidance: [
      '抖音导出确认卡的结果已经记录：accepted 表示用户同意，可以立刻执行并如实汇报；dismissed 表示用户不同意，不要再做，也不要再提这张卡。',
    ],
    statusFields: ['op', 'target'],
    act(agent, card, verb) {
      const decision = {
        callId: card.callId,
        action: ACTION,
        phase: 'accepted',
        data: { op: card.details.op, target: card.details.target, text: OP_TEXT[card.details.op] ?? '操作' },
      }
      cards.record(agent, decision)
      return Promise.resolve({ status: 'settled', decision })
    },
  }

  return cards.register(kind)
}
