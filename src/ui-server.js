/**
 * dsh-codex-bridge UI 后端（独立入口，仅服务交接按钮）。
 *
 * 与 `src/index.js`（事件扫描插件）**完全独立**：只注册交接按钮需要的只读接口，
 * 不启用任何事件钩子。
 *
 * 安全边界（全部复用 desktop 已有载体，不自建认证）：
 * - 用 `ctx.connection.fetch.register` 注册 `/api/...` 精确路由。
 *   `/api` 前缀路由已由 dsh-client-connection 统一做 Host/Origin fence +
 *   认证 Peer 准入；`fetch` 被调用时还会校验 `peer.principal.kind === 'owner'`。
 * - **不**向 `webServer.register` 注册 `/api` exact 路由——那会绕过前缀认证。
 * - 不读、不传、不在前端内联任何凭据；不改 trustedHosts/CORS。
 *
 * 来源校验：客户端可传 `native` + `cwd`，但 `cwd` **只用于索引匹配**；
 * 真正执行 resume 的 cwd 一律取自已索引行，客户端传的路径不参与执行。
 */

import { execFile as execFileCb } from 'node:child_process'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-codex-bridge-ui'
export const inject = ['connection']

const SCAN_TIMEOUT_MS = 20_000
const SCAN_MAX_BUFFER = 8 * 1024 * 1024
const CACHE_TTL_MS = 15_000

/**
 * 包内默认 CLI 路径。
 *
 * 用 `import.meta.url` 相对解析，**不硬编码本机绝对路径**——发布给他人安装时
 * 包会被放在任意位置。`config.cliPath` 仍可覆盖（例如指向自定义构建）。
 */
export function defaultCliPath() {
  return fileURLToPath(new URL('../bin/cli.mjs', import.meta.url))
}

export const SESSIONS_PATH = '/api/dsh-codex-bridge/sessions'
export const RESUME_PATH = '/api/dsh-codex-bridge/resume'

/** 退出码 → 允许的状态。只有这些组合算成功。 */
const EXIT_STATUS = new Map([
  [0, new Set(['ready'])],
  [2, new Set(['no-candidate'])],
  [3, new Set(['needs-selection'])],
  [4, new Set(['incomplete'])],
])

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    },
  })
}

/**
 * 安全 Node runner。
 *
 * Electron 里 `process.execPath` 是 DSH 自己的可执行文件。官方 host 启动子进程时
 * 已继承 `ELECTRON_RUN_AS_NODE=1`，但这里**显式确保** Node 模式，不依赖外部环境。
 */
export function nodeRunner(env) {
  const isElectron = Boolean(process.versions && process.versions.electron)
  const command = process.execPath
  const merged = { ...(env || process.env) }
  if (isElectron) merged.ELECTRON_RUN_AS_NODE = '1'
  return { command, env: merged }
}

function runCli({ cliPath, args, cwd, execFile }) {
  const runner = nodeRunner()
  return new Promise((resolve, reject) => {
    execFile(
      runner.command,
      [cliPath, ...args],
      {
        timeout: SCAN_TIMEOUT_MS,
        maxBuffer: SCAN_MAX_BUFFER,
        cwd,
        env: runner.env,
      },
      (error, out, stderr) => {
        resolve({
          error: error || null,
          stdout: String(out || ''),
          stderr: String(stderr || ''),
        })
      },
    )
  })
}

/**
 * 严格解析：只接受与退出码一致的已知状态。
 *
 * `expectStatus=false` 用于 `sessions`——它输出的是列表对象，没有 `status` 字段；
 * 此时只校验「没被杀/没超时/是合法 JSON」。
 */
export function validateCliResult({ error, stdout }, expectStatus = true) {
  const code = error && typeof error.code === 'number' ? error.code : (error ? -1 : 0)
  if (error) {
    // 被杀/超时/信号/超限：一律失败，不能拿 stdout 冒充成功
    if (error.killed || error.signal) return { ok: false, reason: 'killed' }
    if (code === 'ETIMEDOUT' || error.code === 'ETIMEDOUT') return { ok: false, reason: 'timeout' }
    if (code < 0) return { ok: false, reason: 'spawn' }
  }
  let parsed
  try {
    parsed = JSON.parse(stdout)
  } catch {
    return { ok: false, reason: 'bad-json' }
  }
  if (!parsed || typeof parsed !== 'object') return { ok: false, reason: 'bad-shape' }
  if (!expectStatus) {
    // sessions 列表**只接受 exit 0**：2/3/4 是 resume 的状态语义，
    // 若放行，incomplete 对象会被当成列表缓存，前端假显示「没有任务」。
    if (code !== 0) return { ok: false, reason: `unexpected-exit-${code}` }
    return { ok: true, value: parsed, code }
  }
  const allowed = EXIT_STATUS.get(code)
  if (!allowed) return { ok: false, reason: `unexpected-exit-${code}` }
  if (!allowed.has(parsed.status)) {
    // 退出码与状态不符：例如 exit 4 却说 ready
    return { ok: false, reason: 'status-mismatch' }
  }
  return { ok: true, value: parsed, code }
}

/**
 * 校验 sessions 列表的最小可用形状。
 *
 * `stdout=[]` 也是合法 JSON，但它不是列表对象；不校验就会把空数组
 * 当成「零个可接续聊天」显示给用户。
 */
export function checkSessionsShape(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { ok: false, reason: 'not-object' }
  }
  if (!Array.isArray(value.sessions)) return { ok: false, reason: 'no-sessions-array' }
  if (typeof value.total !== 'number') return { ok: false, reason: 'no-total' }
  if (typeof value.badge !== 'number') return { ok: false, reason: 'no-badge' }
  if (value.warnings !== undefined && !Array.isArray(value.warnings)) {
    return { ok: false, reason: 'bad-warnings' }
  }
  return { ok: true }
}

/**
 * 每个 handler 实例、每个 scope 各自持有 inflight。
 *
 * 不能用一个全局变量：local 与 all-cwds 是两次不同的扫描，
 * 共用会让一个请求拿到另一个 scope 的结果。
 */
function createScopeGate() {
  const inflight = new Map()
  return async (key, run) => {
    if (inflight.has(key)) return inflight.get(key)
    const p = (async () => run())()
    inflight.set(key, p)
    try {
      return await p
    } finally {
      inflight.delete(key)
    }
  }
}

export function createHandlers({ cliPath, execFile, cwdOf, env }) {
  const gate = createScopeGate()
  const cache = new Map()

  const cacheGet = (key) => {
    const hit = cache.get(key)
    if (!hit) return null
    if (Date.now() - hit.at > CACHE_TTL_MS) {
      cache.delete(key)
      return null
    }
    return hit.value
  }
  const cacheSet = (key, value) => {
    cache.set(key, { at: Date.now(), value })
    if (cache.size > 4) cache.delete(cache.keys().next().value)
  }

  const scan = async (allCwds) => {
    const cwd = cwdOf()
    const key = allCwds ? `all:${cwd}` : `local:${cwd}`
    const hit = cacheGet(key)
    if (hit) return hit
    return gate(key, async () => {
      const args = ['sessions', '--from-agent', 'codex', '--cwd', cwd, '--json']
      if (allCwds) args.push('--all-cwds')
      const r = await runCli({ cliPath, args, cwd, execFile, env })
      const checked = validateCliResult(r, false)
      if (!checked.ok) throw new Error(`sessions ${checked.reason}`)
      const shape = checkSessionsShape(checked.value)
      if (!shape.ok) throw new Error(`sessions ${shape.reason}`)
      cacheSet(key, checked.value)
      return checked.value
    })
  }

  return [
    {
      path: SESSIONS_PATH,
      methods: ['GET'],
      requestBody: 'buffered',
      async fetch(request) {
        try {
          const url = new URL(request.url)
          const allCwds = url.searchParams.get('all') === '1'
          return jsonResponse(200, await scan(allCwds))
        } catch (error) {
          return jsonResponse(500, {
            error: 'scan failed',
            detail: String(error && error.message ? error.message : error),
          })
        }
      },
    },
    {
      path: RESUME_PATH,
      methods: ['GET'],
      requestBody: 'buffered',
      async fetch(request) {
        try {
          const url = new URL(request.url)
          const native = url.searchParams.get('native') || ''
          // cwd 仅用于**索引匹配**，不参与执行
          const wantedCwd = url.searchParams.get('cwd') || ''
          if (!native) return jsonResponse(400, { error: 'missing native' })

          const list = await scan(true)
          const rows = ((list && list.sessions) || [])
            .filter((r) => r && r.native_id === native)
          if (rows.length === 0) return jsonResponse(404, { error: 'unknown source' })

          let row
          if (wantedCwd) {
            // 客户端指定了来源：必须精确命中已索引的 (native, cwd)
            row = rows.find((r) => r.cwd === wantedCwd)
            if (!row) {
              return jsonResponse(400, {
                error: 'cwd not indexed',
                note: '给定的 cwd 不在已索引来源中',
              })
            }
          } else if (rows.length === 1) {
            row = rows[0]
          } else {
            return jsonResponse(409, {
              error: 'ambiguous source',
              note: '同一 native 在多个 cwd 都有有效 header，请按 native_id + cwd 精确选择',
              candidates: rows.map((r) => ({ native_id: r.native_id, cwd: r.cwd })),
            })
          }

          // 执行用**索引里的 cwd**，不是客户端传的那个字符串
          const runCwd = row.cwd || cwdOf()
          const args = ['resume', '--from-agent', 'codex', '--session', native,
            '--cwd', runCwd, '--json']
          const r = await runCli({ cliPath, args, cwd: runCwd, execFile, env })
          const checked = validateCliResult(r)
          if (!checked.ok) {
            return jsonResponse(500, {
              error: 'resume failed',
              detail: checked.reason,
            })
          }
          return jsonResponse(200, checked.value)
        } catch (error) {
          return jsonResponse(500, {
            error: 'resume failed',
            detail: String(error && error.message ? error.message : error),
          })
        }
      },
    },
  ]
}

/** 仅测试用。 */
export function __clearCache() {
  // 缓存与 gate 都是 handler 实例级；这里只提供兼容入口
}

export function apply(ctx, config = {}) {
  // 默认用包内 CLI，配置可覆盖；不再要求 profile 里硬编码绝对路径
  const cliPath = config.cliPath || defaultCliPath()
  const handlers = createHandlers({
    cliPath,
    execFile: config.execFile || execFileCb,
    cwdOf: config.cwdOf || (() => process.cwd()),
  })
  for (const h of handlers) {
    ctx.effect(
      () => ctx.connection.fetch.register(h),
      `dsh-codex-bridge-ui: ${h.path}`,
    )
  }
}
