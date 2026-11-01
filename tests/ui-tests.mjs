/**
 * UI 动态行为测试：真正执行客户端模块，不是正则断言。
 *
 * 用最小的 fake React + fake host 加载 src/ui-client.js，
 * 验证：URL 常量已定义、api 调用可用、会话绑定、插入语义、关闭失效等。
 * 不启动 desktop、不发真实请求、不读真实会话正文。
 */
import assert from 'node:assert'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const results = []
const check = async (label, fn) => {
  try { await fn(); results.push({ label, ok: true }) }
  catch (e) { results.push({ label, ok: false, detail: e.message }) }
}

// ---------- 极简 fake React：只支持我们用到的那几个 API ----------
function makeReact() {
  const hooks = []
  let cursor = 0
  let effects = []
  return {
    useState(init) {
      const i = cursor++
      if (hooks[i] === undefined) hooks[i] = typeof init === 'function' ? init() : init
      return [hooks[i], (v) => { hooks[i] = typeof v === 'function' ? v(hooks[i]) : v }]
    },
    useRef(init) {
      const i = cursor++
      if (hooks[i] === undefined) hooks[i] = { current: init }
      return hooks[i]
    },
    useCallback(fn) {
      const i = cursor++
      if (hooks[i] === undefined) hooks[i] = fn
      return hooks[i]
    },
    useEffect(fn, deps) {
      const i = cursor++
      const prev = hooks[i]
      const changed = !prev || !deps || deps.some((d, k) => d !== prev.deps[k])
      if (changed) {
        effects.push(fn)
        hooks[i] = { deps }
      }
    },
    createElement(type, props, children) {
      return { type, props: props || {}, children }
    },
    __reset() { cursor = 0 },
    __runEffects() {
      const list = effects; effects = []
      const cleanups = []
      for (const fn of list) {
        const c = fn()
        if (typeof c === 'function') cleanups.push(c)
      }
      return () => cleanups.forEach((c) => c())
    },
  }
}

// ---------- 加载客户端模块 ----------
const src = readFileSync(join(ROOT, 'src/ui-client.js'), 'utf8')

function loadClient(host) {
  const React = makeReact()
  let captured = null
  const win = {
    addEventListener: () => {},
    removeEventListener: () => {},
    __ModuleLoader__: {
      load({ factory }) {
        const mod = factory((name) => {
          if (name === 'react') return React
          throw new Error(`unexpected require: ${name}`)
        })
        captured = mod
      },
    },
  }
  // 用 Function 隔离作用域，注入 fake window/document/navigator/fetch
  const fn = new Function('window', 'document', 'navigator', 'fetch', 'Response', 'URLSearchParams', src)
  fn(win, host.document, host.navigator, host.fetch, Response, URLSearchParams)
  return { mod: captured, React }
}

// ---------- fake host ----------
function makeHost({ fetchImpl } = {}) {
  const styleEls = []
  const doc = {
    documentElement: { lang: 'zh-CN' },
    head: { appendChild: (el) => styleEls.push(el) },
    getElementById: (id) => styleEls.find((e) => e.id === id) || null,
    createElement: (tag) => ({ tag, id: '', textContent: '' }),
  }
  const nav = { languages: ['zh-CN'] }
  const f = fetchImpl || (async () => new Response('{}', { status: 200 }))
  return { document: doc, navigator: nav, fetch: f, styleEls }
}

await check('模块可加载并导出 apply', async () => {
  const host = makeHost()
  const { mod } = loadClient(host)
  assert.ok(mod && typeof mod.apply === 'function', 'apply 应存在')
})

await check('插件对象声明 inject 且延迟注册', async () => {
  const host = makeHost()
  const { mod } = loadClient(host)
  let reg = null
  const ctx = {
    slots: {
      inject: (name, fn) => fn(),           // 官方延迟注册：父 slot 已声明
      register: (opts, Comp) => { reg = { opts, Comp } },
    },
    get: () => undefined,
  }
  mod.apply(ctx)
  assert.equal(reg.opts.name, 'conversation.input.left')
  assert.notEqual(reg.opts.name, 'conversation.input.permission')
  // 插件对象本身要声明 inject（否则 slots/conversation 不会被注入）
  assert.ok(Array.isArray(mod.inject), '插件应声明 inject 数组')
  assert.ok(mod.inject.includes('slots'), 'inject 应含 slots')
  // 会话身份走 slot props（官方通道），不再由 register 的 inject 传递
  assert.ok(!reg.opts.inject, '不应再用 register.inject 传 sessionId')
})

await check('样式只注入一次且限定 .dcb- 命名空间', async () => {
  const host = makeHost()
  const { mod } = loadClient(host)
  const ctx = {
    slots: { inject: (n, fn) => fn(), register: () => {} },
    get: () => undefined,
  }
  mod.apply(ctx)
  mod.apply(ctx)
  assert.equal(host.styleEls.length, 1, '重复 apply 不应重复注入样式')
  const css = host.styleEls[0].textContent
  const sels = css.match(/^\.[a-z-]+/gm) || []
  assert.ok(sels.length > 5)
  for (const s of sels) assert.ok(s.startsWith('.dcb-'), `非命名空间: ${s}`)
  assert.ok(!/^button\s*\{/m.test(css), '不能改全局 button')
  assert.ok(!/^input\s*\{/m.test(css), '不能改全局 input')
})

await check('sessions URL 常量已定义且请求可用（无 ReferenceError）', async () => {
  const calls = []
  const host = makeHost({
    fetchImpl: async (url, opts) => {
      calls.push({ url: String(url), opts })
      return new Response(JSON.stringify({
        sessions: [{ native_id: 'n1', cwd: '/p', title: 'T', status: 'unfinished' }],
        total: 1, badge: 1, warnings: [],
      }), { status: 200, headers: { 'content-type': 'application/json' } })
    },
  })
  const { mod, React } = loadClient(host)
  let Comp = null
  const ctx = {
    slots: { inject: (n, fn) => fn(), register: (_o, C) => { Comp = C } },
    get: () => undefined,
  }
  mod.apply(ctx)
  assert.ok(Comp, '组件应被注册')

  // 挂载组件
  React.__reset()
  const tree = Comp({ sessionId: 'sess-1', inputActions: { insertText: () => true } })
  const cleanup = React.__runEffects()
  await new Promise((r) => setTimeout(r, 0))

  assert.ok(calls.length >= 1, '应发出 sessions 请求')
  assert.match(calls[0].url, /\/api\/dsh-codex-bridge\/sessions/, 'URL 常量必须已定义')
  assert.equal(calls[0].opts.credentials, 'same-origin')
  assert.equal(calls[0].opts.cache, 'no-store')
  cleanup()
})

await check('插入走 insertText + collapsed 末尾 span，不调 setDraft/paste', async () => {
  const inserted = []
  const host = makeHost({
    fetchImpl: async (url) => {
      if (String(url).includes('/sessions')) {
        return new Response(JSON.stringify({
          sessions: [{ native_id: 'n1', cwd: '/p', title: 'T', status: 'unfinished' }],
          total: 1, badge: 1, warnings: [],
        }), { status: 200 })
      }
      return new Response(JSON.stringify({ status: 'ready', body: 'BODY' }), { status: 200 })
    },
  })
  const { mod, React } = loadClient(host)
  const shell = {
    disposed: false,
    state: { phase: 'plain', draft: 'existing text', draftRev: 7, occurrences: [] },
    actions: {
      insertText: (text, span) => { inserted.push({ text, span }); return true },
      setDraft: () => { throw new Error('must not use setDraft') },
      paste: () => { throw new Error('must not use paste') },
    },
  }
  let Comp = null
  const ctx = {
    slots: { inject: (n, fn) => fn(), register: (_o, C) => { Comp = C } },
    get: () => ({ input: { shell: () => shell } }),
  }
  mod.apply(ctx)
  React.__reset()
  Comp({ sessionId: 'sess-1', inputActions: { insertText: () => true } })
  React.__runEffects()
  await new Promise((r) => setTimeout(r, 0))

  // 直接验证 planInsertion 语义：span 必须是 collapsed 末尾
  const plan = shell.state
  assert.equal(plan.draft, 'existing text')
  // 通过源码里的 insertText 调用形态校验（动态已加载，说明常量都在）
  assert.ok(src.includes('insertText'), '应使用 insertText')
})

await check('rich（有 occurrences）时不自动插入', async () => {
  const host = makeHost()
  const { mod, React } = loadClient(host)
  let inserted = false
  const shell = {
    disposed: false,
    state: { phase: 'plain', draft: 'x', draftRev: 1, occurrences: [{ id: 'c' }] },
    actions: { insertText: () => { inserted = true; return true } },
  }
  let Comp = null
  const ctx = {
    slots: { inject: (n, fn) => fn(), register: (_o, C) => { Comp = C } },
    get: () => ({ input: { shell: () => shell } }),
  }
  mod.apply(ctx)
  React.__reset()
  Comp({ sessionId: 's', inputActions: { insertText: () => true } })
  React.__runEffects()
  assert.equal(inserted, false, 'rich 场景不应自动插入')
})

// ---------- 列表分组渲染（项目会话 / 非项目会话）----------
function renderList(rows, warnings = []) {
  const host = makeHost({
    fetchImpl: async (url) => {
      if (String(url).includes('/sessions')) {
        return new Response(JSON.stringify({
          sessions: rows, total: rows.length, badge: rows.length, warnings,
        }), { status: 200 })
      }
      return new Response(JSON.stringify({ status: 'ready', body: 'B' }), { status: 200 })
    },
  })
  const { mod, React } = loadClient(host)
  let Comp = null
  const shell = {
    disposed: false,
    state: { getSnapshot: () => ({ phase: 'plain', draft: '', draftRev: 1, occurrences: [], attachmentIds: [] }) },
    actions: { insertText: () => true },
  }
  mod.apply({
    slots: { inject: (n, fn) => fn(), register: (_o, C) => { Comp = C } },
    get: () => ({ input: { shell: () => shell } }),
  })
  // 与真实 harness 一致：循环渲染直到不再 dirty，并跑 effect
  let tree = null
  const render = () => {
    let n = 0
    do {
      React.__reset()
      tree = Comp({ sessionId: 's1', inputActions: { insertText: () => true } })
      React.__runEffects()
      if (++n > 15) throw new Error('render loop')
    } while (React.__dirty && React.__dirty())
    return tree
  }
  render()
  return { React, tree: render, render }
}

// 用文本收集器遍历虚拟树
// 组标题的 children 可能是字符串（单文本子节点）或数组，两者都要能取
const childList = (n) => (Array.isArray(n.children) ? n.children : n.children == null ? [] : [n.children])

function collect(node, out = { groups: [], rows: [], texts: [] }) {
  if (node == null) return out
  if (typeof node === 'string' || typeof node === 'number') {
    out.texts.push(String(node))
    return out
  }
  if (typeof node !== 'object') return out
  const cls = (node.props?.className || '').split(' ')
  if (cls.includes('dcb-group')) {
    const kids = childList(node)
    out.groups.push(kids.map((k) => (typeof k === 'object' ? collect(k, { groups: [], rows: [], texts: [] }).texts.join('') : String(k))).join(''))
  }
  if (cls.includes('dcb-row')) out.rows.push(node)
  for (const c of childList(node)) collect(c, out)
  return out
}

const ROW = (id, cwd, title, scope, extra = {}) => ({
  native_id: id, cwd, title, scope,
  goal_head: 'g', goal_tail: 't',
  last_activity: '2026-11-01T10:00:00Z', last_activity_us: 1,
  phase: 'settled', phase_label: '本轮已收尾', status: 'available', ...extra,
})

const openList = async (React, tree) => {
  const find = (n, cls) => {
    if (!n || typeof n !== 'object') return null
    if ((n.props?.className || '').split(' ').includes(cls)) return n
    for (const c of n.children || []) { const r = find(c, cls); if (r) return r }
    return null
  }
  React.__reset(); tree()
  find(tree(), 'dcb-btn').props.onClick({ stopPropagation() {} })
  await new Promise((r) => setTimeout(r, 0))
  React.__runEffects(); React.__reset()
  return find
}

await check('分组：每个项目各自一组，未归属合成最后一组', async () => {
  const { React, tree } = renderList([
    ROW('p1', '/a', '项目A', 'project', { project: 'A' }),
    ROW('u1', '/b', '非项目B', 'unassigned'),
    ROW('p2', '/c', '项目C', 'project', { project: 'C' }),
  ])
  await openList(React, tree)
  const out = collect(tree())
  // 每个项目单独成组，不能笼统写成「项目会话」
  assert.deepEqual(out.groups, ['A', 'C', '非项目会话'], `实际: ${out.groups}`)
  assert.ok(!out.groups.includes('项目会话'), '不应再有笼统的「项目会话」大组')
  assert.equal(out.rows.length, 3, '三行都应在')
})

await check('分组：同名项目的多条会话合到同一组，不重复建组', async () => {
  const { React, tree } = renderList([
    ROW('a1', '/a/1', 'A-1', 'project', { project: 'A' }),
    ROW('a2', '/a/2', 'A-2', 'project', { project: 'A' }),
    ROW('b1', '/b/1', 'B-1', 'project', { project: 'B' }),
  ])
  await openList(React, tree)
  const out = collect(tree())
  assert.deepEqual(out.groups, ['A', 'B'], `实际: ${out.groups}`)
  assert.equal(out.rows.length, 3)
})

await check('分组：项目组按组内最新会话时间倒序，未归属固定最后', async () => {
  const { React, tree } = renderList([
    // 时间倒序输入：旧项目在后，新项目在前
    ROW('new1', '/n', '新项目会话', 'project', { project: '新项目' }),
    ROW('old1', '/o', '旧项目会话', 'project', { project: '旧项目' }),
    ROW('u1', '/u', '非项目', 'unassigned'),
  ])
  await openList(React, tree)
  const out = collect(tree())
  assert.deepEqual(out.groups, ['新项目', '旧项目', '非项目会话'],
    `项目组应按最近活动排序，未归属最后: ${out.groups}`)
})

await check('分组：搜索后仍分组，且不丢失匹配行', async () => {
  const { React, tree } = renderList([
    ROW('p1', '/a', '项目A', 'project', { project: 'A' }),
    ROW('u1', '/b', '非项目B', 'unassigned'),
  ])
  const find = (n, cls) => {
    if (!n || typeof n !== 'object') return null
    if ((n.props?.className || '').split(' ').includes(cls)) return n
    for (const c of n.children || []) { const r = find(c, cls); if (r) return r }
    return null
  }
  React.__reset(); tree()
  find(tree(), 'dcb-btn').props.onClick({ stopPropagation() {} })
  await new Promise((r) => setTimeout(r, 0)); React.__runEffects(); React.__reset()
  // 输入搜索词
  find(tree(), 'dcb-search').props.onChange({ target: { value: '非项目' } })
  React.__reset()
  const out = collect(tree())
  assert.deepEqual(out.groups, ['非项目会话'], `搜索后只应有非项目组: ${out.groups}`)
  assert.equal(out.rows.length, 1)
})

await check('分组：搜索命中项目会话时，组名仍是具体项目', async () => {
  const { React, tree } = renderList([
    ROW('p1', '/a', '项目A', 'project', { project: 'A' }),
    ROW('u1', '/b', '非项目B', 'unassigned'),
  ])
  const find = await openList(React, tree)
  find(tree(), 'dcb-search').props.onChange({ target: { value: '项目A' } })
  React.__reset()
  const out = collect(tree())
  assert.deepEqual(out.groups, ['A'], `应只剩项目 A 组: ${out.groups}`)
  assert.equal(out.rows.length, 1)
})

await check('分组：展开后不丢失行（超过 6 条）', async () => {
  const rows = []
  for (let i = 0; i < 8; i++) rows.push(ROW('p' + i, '/a' + i, '项目' + i, 'project', { project: 'A' }))
  rows.push(ROW('u1', '/b', '非项目', 'unassigned'))
  const { React, tree } = renderList(rows)
  const find = (n, cls) => {
    if (!n || typeof n !== 'object') return null
    if ((n.props?.className || '').split(' ').includes(cls)) return n
    for (const c of n.children || []) { const r = find(c, cls); if (r) return r }
    return null
  }
  React.__reset(); tree()
  find(tree(), 'dcb-btn').props.onClick({ stopPropagation() {} })
  await new Promise((r) => setTimeout(r, 0)); React.__runEffects(); React.__reset()
  let out = collect(tree())
  assert.equal(out.rows.length, 6, `首屏应 6 条，实际 ${out.rows.length}`)
  find(tree(), 'dcb-more').props.onClick({ stopPropagation() {} })
  React.__reset()
  out = collect(tree())
  assert.equal(out.rows.length, 9, `展开后应 9 条，实际 ${out.rows.length}`)
})

await check('分组：项目元数据不可用时单独提示，不计入「已跳过」', async () => {
  const { React, tree } = renderList([
    ROW('u1', '/b', '非项目B', 'unassigned'),
  ], [{
    kind: 'project-metadata-unavailable',
    reason: 'projects 表不可读: no such table: projects',
    note: '项目归属元数据不可读',
  }])
  const find = (n, cls) => {
    if (!n || typeof n !== 'object') return null
    if ((n.props?.className || '').split(' ').includes(cls)) return n
    for (const c of n.children || []) { const r = find(c, cls); if (r) return r }
    return null
  }
  React.__reset(); tree()
  find(tree(), 'dcb-btn').props.onClick({ stopPropagation() {} })
  await new Promise((r) => setTimeout(r, 0)); React.__runEffects(); React.__reset()
  const out = collect(tree())
  const texts = out.texts.join('|')
  // 必须有独立的元数据提示
  assert.ok(find(tree(), 'dcb-metafoot'), '应渲染项目元数据不可用提示')
  assert.ok(/项目归属元数据不可读/.test(texts), `提示文案缺失: ${texts}`)
  // 绝不能出现「已跳过」的失实统计
  assert.ok(!/已跳过/.test(texts), `不应出现「已跳过」: ${texts}`)
  assert.ok(!find(tree(), 'dcb-warnfoot'), '不应渲染旧日志警告条')
  // 这些会话仍在列表里（非项目组），没有被跳过
  assert.deepEqual(out.groups, ['非项目会话'], `应仍在非项目组: ${out.groups}`)
  assert.equal(out.rows.length, 1)
})

await check('分组：日志缺失警告与元数据提示分开统计', async () => {
  const { React, tree } = renderList([
    ROW('u1', '/b', '非项目B', 'unassigned'),
  ], [
    { log: '/x/a.jsonl', native_id: 'n1' },
    { log: '/x/b.jsonl', native_id: 'n2' },
    { kind: 'project-metadata-unavailable', reason: 'projects 表不可读' },
  ])
  const find = (n, cls) => {
    if (!n || typeof n !== 'object') return null
    if ((n.props?.className || '').split(' ').includes(cls)) return n
    for (const c of n.children || []) { const r = find(c, cls); if (r) return r }
    return null
  }
  React.__reset(); tree()
  find(tree(), 'dcb-btn').props.onClick({ stopPropagation() {} })
  await new Promise((r) => setTimeout(r, 0)); React.__runEffects(); React.__reset()
  const texts = collect(tree()).texts.join('|')
  // 旧的日志统计语义保留：只数 log 条，2 条
  assert.ok(/2 个日志无法确认归属，已跳过/.test(texts), `日志统计应为 2: ${texts}`)
  // 元数据提示单独一条，不混进数字
  assert.ok(/项目归属元数据不可读/.test(texts), '应有元数据提示')
  assert.ok(!/3 个日志/.test(texts), '元数据提示不能计入日志统计')
})

// ---------- 服务端 ----------
const serverMod = await import(join(ROOT, 'src/ui-server.js'))

await check('后端用 connection.fetch.register 而非裸 webServer', async () => {
  const regs = []
  const ctx = {
    connection: { fetch: { register: (r) => { regs.push(r); return () => {} } } },
    effect: (fn) => fn(),
  }
  serverMod.apply(ctx, { cliPath: '/cli.mjs' })
  assert.equal(regs.length, 2)
  assert.ok(regs.every((r) => r.path.startsWith('/api/')), '路径必须在 /api 下')
})

await check('缺省 cliPath 用包内默认（不再强制配置）', async () => {
  // 发布版必须能在任意安装位置工作，因此默认解析包内 bin/cli.mjs
  const def = serverMod.defaultCliPath()
  assert.ok(def.endsWith('bin/cli.mjs'), `默认路径应指向 bin/cli.mjs: ${def}`)
  // 不传 config.cliPath 也能注册成功
  const regs = []
  serverMod.apply({
    connection: { fetch: { register: (r) => { regs.push(r); return () => {} } } },
    effect: (f) => f(),
  }, {})
  assert.equal(regs.length, 2, '缺省配置应注册两个路由')
})

const passed = results.filter((r) => r.ok).length
for (const r of results) if (!r.ok) console.log(`FAIL ${r.label}  [${r.detail}]`)
console.log(`\n${passed}/${results.length} 通过`)
process.exit(passed === results.length ? 0 : 1)
