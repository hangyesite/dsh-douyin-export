// 技能提供者：把本插件包内的 skills/ 目录注册进 DSH 的技能目录（ctx.skills）。
// 这样 Agent 在会话里就能看到「抖音导出」这份 SKILL.md，知道什么时候调哪些工具。
//
// 契约来自 @deepseek-ai/dsh-skill-filesystem：
//   ctx.skills.registerProvider((control) => provider)
//   provider.list(options)         -> SkillCandidate[]
//   provider.get(candidate, opts)  -> SkillDefinition | undefined
// SkillCandidate: { name, description, whenToUse?, invocation, provider, source, rank, locator, resourceBase, path }
// SkillDefinition 在 candidate 基础上带 content（Markdown 正文，不含 frontmatter）。

import fs from 'node:fs';
import path from 'node:path';

const PROVIDER = 'douyin-export';
const RANK = 10;

/** 极简 frontmatter 解析：只认 `key: value` 单行，够 SKILL.md 用。 */
function parseSkill(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(text);
  if (match === null) return { meta: {}, content: text.trim() };
  const meta = {};
  for (const line of match[1].split(/\r?\n/)) {
    const hit = /^([A-Za-z][\w-]*)\s*:\s*(.*)$/.exec(line);
    if (hit === null) continue;
    let value = hit[2].trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    meta[hit[1]] = value;
  }
  return { meta, content: match[2].trim() };
}

/**
 * 注册插件自带的技能目录。
 * @param {object} ctx Cordis 上下文（需已 inject 'skills'）
 * @param {string} skillDir 形如 <包目录>/skills 的绝对路径
 * @returns {() => void} 注销函数
 */
export function registerSkills(ctx, skillDir) {
  const entries = () => {
    let names = [];
    try {
      names = fs
        .readdirSync(skillDir, { withFileTypes: true })
        .filter((item) => item.isDirectory())
        .filter((item) => fs.existsSync(path.join(skillDir, item.name, 'SKILL.md')))
        .map((item) => item.name)
        .sort();
    } catch {
      return [];
    }
    return names.map((name) => {
      const dir = path.join(skillDir, name);
      const file = path.join(dir, 'SKILL.md');
      const { meta } = parseSkill(safeRead(file));
      return {
        name: meta.name || name,
        description: meta.description || '',
        ...(meta.whenToUse ? { whenToUse: meta.whenToUse } : {}),
        invocation: {
          modelInvocable: meta.disableModelInvocation === 'true' ? false : true,
          userInvocable: meta.userInvocable === 'false' ? false : true,
        },
        provider: PROVIDER,
        source: 'bundled',
        rank: RANK,
        locator: { path: file, directory: dir },
        resourceBase: { kind: 'directory', path: dir },
        path: file,
      };
    });
  };

  const provider = {
    name: PROVIDER,
    async list() {
      return entries();
    },
    async get(candidate) {
      const lead = entries().find((item) => item.path === candidate.path);
      if (lead === undefined) return undefined;
      const { meta, content } = parseSkill(safeRead(lead.path));
      return {
        ...lead,
        name: meta.name || lead.name,
        description: meta.description || lead.description,
        ...(meta.whenToUse ? { whenToUse: meta.whenToUse } : {}),
        content,
      };
    },
    async dispose() {},
  };

  const off = ctx.skills.registerProvider(() => provider);
  return () => {
    if (typeof off === 'function') off();
  };
}

function safeRead(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch {
    return '';
  }
}
