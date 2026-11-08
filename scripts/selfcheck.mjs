import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8')

let bad = 0
const fail = (msg) => { bad += 1; console.error('FAIL ' + msg) }
const ok = (msg) => console.log('ok   ' + msg)

const pkg = JSON.parse(read('package.json').replace(/^\uFEFF/, ''))

// 1) client bundle 的模块 id 必须等于包名，否则浏览器端整包不加载（面板入口会消失）
const client = read('client.js')
const m = client.match(/__ModuleLoader__\.load\(\{[^}]*?id:\s*'([^']+)'/)
if (!m) fail('client.js 里找不到 __ModuleLoader__.load({ id })')
else if (m[1] !== pkg.name) fail(`client.js 模块 id (${m[1]}) 与 package.json name (${pkg.name}) 不一致`)
else ok('client.js 模块 id 与包名一致：' + m[1])

// 2) cordis.patch.yml 的 name 必须等于包名
const nm = read('cordis.patch.yml').match(/name:\s*'([^']+)'/)
if (!nm) fail('cordis.patch.yml 里找不到 name')
else if (nm[1] !== pkg.name) fail(`cordis.patch.yml name (${nm[1]}) 与包名 (${pkg.name}) 不一致`)
else ok('cordis.patch.yml name 与包名一致')

// 3) 市场收录硬性门槛：必须声明 dsh.bundle
if (!pkg.dsh || !pkg.dsh.bundle || !pkg.dsh.bundle.patch) fail('package.json 缺少 dsh.bundle.patch')
else ok('dsh.bundle.patch = ' + pkg.dsh.bundle.patch)

// 4) 入口文件必须存在
for (const rel of ['index.js', 'client.js', 'server.js', 'tasks.js', 'skills.js', 'action-cards.js', 'cordis.patch.yml', 'icon.svg']) {
  if (!fs.existsSync(path.join(root, rel))) fail('缺少文件 ' + rel)
}
ok('入口文件齐全')

// 5) 显示元数据
for (const lang of ['zh', 'en']) {
  const dict = JSON.parse(read('locale/' + lang + '.json'))
  if (!dict.meta || !dict.meta.title || !dict.meta.description) fail('locale/' + lang + '.json 缺 meta.title/description')
}
ok('locale 元数据齐全')

// 6) 对外发布不能出现预置厂商名
for (const word of ['fanfan', 'aifan', '爱饭']) {
  if (pkg.description.toLowerCase().includes(word) || read('skills/douyin-export/SKILL.md').includes(word)) fail('出现了厂商名：' + word)
}
ok('未出现预置厂商名')

// 7) MCP 变体的 skill 必须存在，并且用 MCP 工具名（douyin_*），不要混进 DSH 的 dyx_*
const mcpSkillPath = path.join(root, 'mcp', 'SKILL.md')
if (!fs.existsSync(mcpSkillPath)) fail('缺少 mcp/SKILL.md（给非 DSH 的 MCP 客户端用）')
else {
  const text = fs.readFileSync(mcpSkillPath, 'utf8')
  if (!text.includes('douyin_run')) fail('mcp/SKILL.md 没提到 douyin_run')
  else if (/\bdyx_run\b/.test(text)) fail('mcp/SKILL.md 出现了 dyx_run')
  else ok('mcp/SKILL.md 使用 douyin_* 工具名')
}

console.log(bad ? `\n${bad} 项检查未通过` : '\n全部检查通过')
process.exit(bad ? 1 : 0)
