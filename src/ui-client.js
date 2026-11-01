window.__ModuleLoader__.load({
  id: 'dsh-codex-bridge',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

/**
 * dsh-codex-bridge UI 客户端：输入框下方「交接」按钮。
 *
 * 挂到 `conversation.input.left`（list 位），**不占用**
 * `conversation.input.permission`（single 位，已有权限控件）。
 *
 * 官方接线要点（见 work/ui-sdk-review-20261031.md）：
 * - React 来自 factory 的 require，不是 Cordis 服务。
 * - 会话身份与输入动作来自 **slot props**（sessionId / inputActions / useInput），
 *   不从根 conversation 单例上猜。
 * - shell.state 是 SnapshotStore，实际状态在 shell.snapshot 或 state.getSnapshot()。
 * - 用 slots.inject(name, () => slots.register(...)) 延迟注册，等父 slot 声明。
 * - 插入用 insertText(text, {start:n,end:n,draftRev})，绝不 setDraft/paste/submit。
 */

const SESSIONS_URL = '/api/dsh-codex-bridge/sessions'
const RESUME_URL = '/api/dsh-codex-bridge/resume'
const RECENT_LIMIT = 6

const COPY = {
  zh: {
    label: '交接', title: '从 Codex 聊天接续',
    search: '搜索聊天名称、目标或路径',
    more: '显示其余', less: '收起', pick: '选择',
    insert: '插入当前对话', copy: '复制恢复内容', copied: '已复制',
    empty: '当前范围没有可接续聊天',
    emptyHint: '空白或仅系统注入的会话不会列出。',
    incomplete: '来源需要核验', loading: '读取中…',
    interrupted: '因故中断', settled: '本轮已收尾', unsettled: '未收尾/需核验',
    noMatch: '没有匹配聊天',
    groupUnassigned: '非项目会话',
    skippedLogs: (n) => n + ' 个日志无法确认归属，已跳过',
    metadataUnavailable: '项目归属元数据不可读',
    metadataUnavailableHint: '暂时无法确认项目归属，相关聊天暂列在非项目会话中；'
      + '这不代表它们确实不属于任何项目。',
    metadataAmbiguous: '目录同时登记给多个项目',
    metadataAmbiguousHint: '有聊天所在的目录同时登记给了多个项目，无法判定归属；'
      + '这些聊天暂列在非项目会话中，不代表它们确实不属于任何项目。',
    failed: '读取失败',
    failedHint: '稍后再试；如果持续失败，用命令行 dsh-codex-bridge sessions 检查。',
    ambiguous: '同一会话出现在多个目录，请选择来源',
    richHint: '输入框里有引用或命令，已改为复制，避免破坏现有内容。',
    stale: '输入框或会话已变化，请重新打开',
    inserted: '已插入输入框，确认后自行发送',
    noTarget: '找不到当前对话的输入框',
  },
  en: {
    label: 'Handoff', title: 'Continue a Codex conversation',
    search: 'Search name, goal, or path',
    more: 'Show rest', less: 'Collapse', pick: 'Select',
    insert: 'Insert into this conversation', copy: 'Copy recovery content', copied: 'Copied',
    empty: 'No continuable chats in this scope',
    emptyHint: 'Blank or system-only sessions are not listed.',
    incomplete: 'Source needs verification', loading: 'Loading…',
    interrupted: 'Interrupted', settled: 'Round finished', unsettled: 'Unsettled / verify',
    noMatch: 'No matching chats',
    groupUnassigned: 'Unassigned chats',
    skippedLogs: (n) => n + ' log(s) could not be attributed; skipped',
    metadataUnavailable: 'Project metadata unreadable',
    metadataUnavailableHint: 'Project ownership cannot be confirmed right now, so these chats '
      + 'are listed under unassigned; that does not mean they belong to no project.',
    metadataAmbiguous: 'Folder registered under several projects',
    metadataAmbiguousHint: 'Some chats live in a folder registered under more than one project, '
      + 'so ownership cannot be decided; they are listed under unassigned, which does not mean '
      + 'they belong to no project.',
    failed: 'Could not read',
    failedHint: 'Try again; if it keeps failing, run dsh-codex-bridge sessions.',
    ambiguous: 'This session appears under several folders; pick a source',
    richHint: 'The input has references or a command, so copying is offered instead.',
    stale: 'The input or conversation changed; reopen',
    inserted: 'Inserted into the input; send it yourself',
    noTarget: 'No input box found for this conversation',
  },
}

function detectZh() {
  const tag = typeof document !== 'undefined' ? document?.documentElement?.lang : ''
  if (/^zh(-|$)/i.test(tag || '')) return true
  if (tag) return false
  const nav = typeof navigator !== 'undefined' ? navigator.languages || navigator.language : ''
  const first = Array.isArray(nav) ? nav[0] : nav
  return typeof first === 'string' && /^zh(-|$)/i.test(first)
}

function createApi(fetchImpl) {
  const doFetch = fetchImpl || ((...a) => fetch(...a))
  return {
    async sessions() {
      const r = await doFetch(SESSIONS_URL + '?all=1', {
        cache: 'no-store', credentials: 'same-origin',
      })
      if (!r.ok) throw new Error('sessions ' + r.status)
      return r.json()
    },
    async resume(nativeId, cwd) {
      const q = new URLSearchParams({ native: nativeId })
      if (cwd) q.set('cwd', cwd)
      const r = await doFetch(RESUME_URL + '?' + q.toString(), {
        cache: 'no-store', credentials: 'same-origin',
      })
      if (r.status === 409) return r.json()
      if (!r.ok) throw new Error('resume ' + r.status)
      return r.json()
    },
  }
}

/**
 * 从 shell 取**同一份** InputState 快照。
 *
 * `shell.state` 是 SnapshotStore，不是平铺对象：用 `state.getSnapshot()`；
 * `shell.snapshot` 是同一快照的直读入口。
 */
function readSnapshot(shell) {
  if (!shell || shell.disposed) return null
  const state = shell.state
  if (state && typeof state.getSnapshot === 'function') {
    const snap = state.getSnapshot()
    if (snap && typeof snap === 'object') return snap
  }
  const snap = shell.snapshot
  if (snap && typeof snap === 'object') return snap
  if (state && typeof state === 'object' && typeof state.phase === 'string') return state
  return null
}

/** 同快照取 collapsed 末尾 span 与 draftRev，中间不 await。 */
function planInsertion(shell) {
  const snap = readSnapshot(shell)
  if (!snap) return { ok: false, reason: shell && shell.disposed ? 'disposed' : 'no-state' }
  if (snap.phase !== 'plain') return { ok: false, reason: 'phase' }
  const occ = snap.occurrences
  if (Array.isArray(occ) && occ.length > 0) return { ok: false, reason: 'rich' }
  const draft = typeof snap.draft === 'string' ? snap.draft : ''
  const rev = snap.draftRev
  if (typeof rev !== 'number') return { ok: false, reason: 'no-rev' }
  const n = draft.length
  return { ok: true, span: { start: n, end: n, draftRev: rev }, rev }
}

/**
 * @param deps.api      只读接口
 * @param deps.getShell (sessionId) => shell | null
 */
function createHandoffButton(React, deps) {
  const h = React.createElement
  const c = detectZh() ? COPY.zh : COPY.en
  const api = deps.api
  const getShell = deps.getShell

  return function HandoffButton(props) {
    // 会话身份与输入动作来自 slot props（官方通道），不猜根单例
    const sessionId = props && props.sessionId
    const inputActions = props && props.inputActions
    const useInput = props && props.useInput

    const [open, setOpen] = React.useState(false)
    const [badge, setBadge] = React.useState(null)
    const [rows, setRows] = React.useState([])
    const [warnings, setWarnings] = React.useState([])
    const [query, setQuery] = React.useState('')
    const [expanded, setExpanded] = React.useState(false)
    const [notice, setNotice] = React.useState(null)
    const [picked, setPicked] = React.useState(null)
    const [preview, setPreview] = React.useState(null)
    const [busy, setBusy] = React.useState(false)

    const seq = React.useRef(0)
    const alive = React.useRef(true)
    const target = React.useRef(null)
    // 弹层容器：用于判断点击是否落在框外
    const rootRef = React.useRef(null)
    // **同步锁**：state 更新是异步的，双击时 picked 还没变，
    // 所以必须用 ref 在发起请求前就占位，防止同一来源发两次 resume。
    const pickingRef = React.useRef(null)

    React.useEffect(() => {
      alive.current = true
      return () => { alive.current = false; seq.current += 1 }
    }, [])

    // 接收会话变化：作废在途请求、清空预览、关闭
    React.useEffect(() => {
      seq.current += 1
      pickingRef.current = null
      setOpen(false); setPicked(null); setPreview(null); setNotice(null)
      target.current = null
    }, [sessionId])

    const load = React.useCallback(async () => {
      const my = ++seq.current
      setBusy(true)
      try {
        const data = await api.sessions()
        if (!alive.current || my !== seq.current) return
        setRows(data.sessions || [])
        setBadge(data.badge || 0)
        setWarnings(data.warnings || [])
      } catch (e) {
        if (!alive.current || my !== seq.current) return
        setNotice({ kind: 'error', text: c.failed, hint: c.failedHint })
      } finally {
        if (alive.current && my === seq.current) setBusy(false)
      }
    }, [])

    React.useEffect(() => {
      load()
      const onFocus = () => { if (!open) load() }
      window.addEventListener('focus', onFocus)
      return () => window.removeEventListener('focus', onFocus)
    }, [load, open])

    const openList = () => { target.current = sessionId; setOpen(true); load() }

    const close = () => {
      seq.current += 1
      pickingRef.current = null
      setOpen(false); setPicked(null); setPreview(null); setNotice(null)
      target.current = null
    }

    // 点开之后点击框外自动关闭。
    // 用 pointerdown 捕获阶段：比 click 更早，且不受内部元素 stopPropagation 影响。
    React.useEffect(() => {
      if (!open) return undefined
      // 宿主可能只提供 window 或 document 的事件接口，两者择一即可
      const host = (typeof document !== 'undefined' && document
        && typeof document.addEventListener === 'function') ? document
        : (typeof window !== 'undefined' ? window : null)
      if (!host) return undefined
      const onDown = (ev) => {
        const el = rootRef.current
        const t = ev && ev.target
        // 没有容器或没有 target：无法判断，保守不关
        if (!el || !t) return
        // 目标就是容器本身，或容器包含目标（按钮/弹层/行）→ 框内，不关
        if (t === el) return
        if (typeof el.contains === 'function') {
          if (el.contains(t)) return
          close()
          return
        }
        // 拿不到 contains（非常规宿主）：保守不关，避免误关
        return
      }
      host.addEventListener('pointerdown', onDown, true)
      return () => host.removeEventListener('pointerdown', onDown, true)
    }, [open])

    const pick = async (row, ev) => {
      if (ev && ev.stopPropagation) ev.stopPropagation()
      const key = row.native_id + '\u0000' + row.cwd
      // 同步锁：同一来源正在请求中就直接返回（双击不会发两次）
      if (pickingRef.current === key) return
      pickingRef.current = key
      const my = ++seq.current
      setPicked(row); setPreview(null); setNotice(null); setBusy(true)
      try {
        const data = await api.resume(row.native_id, row.cwd)
        if (!alive.current || my !== seq.current) return
        if (data && data.error === 'ambiguous source') {
          setNotice({ kind: 'warn', text: c.ambiguous }); return
        }
        setPreview(data)
      } catch (e) {
        if (!alive.current || my !== seq.current) return
        setNotice({ kind: 'error', text: c.failed, hint: c.failedHint })
      } finally {
        // 释放锁：**失败后也释放**，允许重试同一来源
        if (pickingRef.current === key) pickingRef.current = null
        if (alive.current && my === seq.current) setBusy(false)
      }
    }

    const insert = (ev) => {
      if (ev && ev.stopPropagation) ev.stopPropagation()
      if (!preview) return
      // 确认时核对接收会话没变
      if (target.current !== sessionId) { setNotice({ kind: 'warn', text: c.stale }); return }
      const shell = getShell(target.current)
      if (!shell) { setNotice({ kind: 'warn', text: c.noTarget }); return }
      const plan = planInsertion(shell)
      if (!plan.ok) {
        setNotice({ kind: 'warn', text: plan.reason === 'rich' ? c.richHint : c.stale })
        return
      }
      // 优先用 slot 提供的 inputActions（官方通道），回退 shell.actions
      const actions = inputActions && typeof inputActions.insertText === 'function'
        ? inputActions : shell.actions
      const ok = actions.insertText(preview.body || '', plan.span)
      if (!ok) { setNotice({ kind: 'warn', text: c.stale }); return }
      setNotice({ kind: 'ok', text: c.inserted })
      setOpen(false); setPicked(null); setPreview(null); target.current = null
    }

    const copy = async (ev) => {
      if (ev && ev.stopPropagation) ev.stopPropagation()
      if (!preview) return
      try {
        await navigator.clipboard.writeText(preview.body || '')
        setNotice({ kind: 'ok', text: c.copied })
      } catch { setNotice({ kind: 'warn', text: c.failed }) }
    }

    const filtered = query.trim()
      ? rows.filter((r) => {
          const q = query.trim().toLowerCase()
          return [r.title, r.goal_head, r.goal_tail, r.cwd]
            .some((v) => typeof v === 'string' && v.toLowerCase().includes(q))
        })
      : rows
    const shown = query.trim() || expanded ? filtered : filtered.slice(0, RECENT_LIMIT)
    // 分两组展示：项目会话 / 非项目会话。
    // 归属来自 Codex 注册的项目根（后端 scope 字段），不按目录名猜。
    // 组内保持原顺序（时间倒序），不改变可达性。
    // 分三档展示：每个**具体项目**各自一组，未归属的合成最后一组。
    // 只写「项目会话」一个笼统标题的话，用户看不出这条到底属于哪个项目，
    // 还得逐条看路径才能分辨——那就等于没分。
    // 项目顺序按该组内最新一条会话的时间倒序（shown 已按时间倒序），
    // 不按字母/条数排，保证「最近在做的项目」排在最前。
    const groups = []
    const byProject = new Map()
    for (const r of shown) {
      const key = r.scope === 'project' && r.project ? r.project : null
      const gk = key === null ? '\u0000unassigned' : 'p:' + key
      let g = byProject.get(gk)
      if (!g) {
        g = { key: gk, label: key === null ? c.groupUnassigned : key, rows: [] }
        byProject.set(gk, g)
        groups.push(g)
      }
      g.rows.push(r)
    }
    // 未归属组固定放最后，不按时间抢位
    groups.sort((a, b) => (a.key === '\u0000unassigned' ? 1 : 0)
      - (b.key === '\u0000unassigned' ? 1 : 0))
    // 三类 warning 语义完全不同，必须分开呈现：
    // - 日志缺失（有 log 字段）：这些会话确实**被跳过**，统计条数。
    // - 项目元数据不可用：会话**没有**被跳过，只是归属未确认，不能计入「已跳过」。
    // - 目录歧义：cwd 所在目录同时登记给多个项目，同样只是未确认，不是被跳过。
    const skippedWarnings = warnings.filter((w) => w && typeof w.log === 'string')
    const metaWarnings = warnings.filter(
      (w) => w && (w.kind === 'project-metadata-unavailable' || w.kind === 'project-ambiguous'))
    const label = badge && badge > 0 ? c.label + ' · ' + badge : c.label

    return h('span', { className: 'dcb-root', ref: rootRef }, [
      h('button', {
        key: 'btn', type: 'button', className: 'dcb-btn', title: c.title,
        'aria-haspopup': 'dialog', 'aria-expanded': open,
        disabled: !sessionId,
        onClick: openList,
      }, label),
      open ? h('div', {
        key: 'dlg', className: 'dcb-dialog', role: 'dialog', 'aria-label': c.title,
        onKeyDown: (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); close() } },
      }, [
        h('div', { key: 'hd', className: 'dcb-hd' }, [
          h('span', { key: 't' }, c.title),
          h('button', {
            key: 'x', type: 'button', className: 'dcb-x', 'aria-label': 'Close',
            onClick: (ev) => { ev.stopPropagation(); close() },
          }, '×'),
        ]),
        h('input', {
          key: 'q', className: 'dcb-search', type: 'search', placeholder: c.search,
          value: query, onChange: (ev) => setQuery(ev.target.value),
        }),
        notice ? h('div', {
          key: 'n', className: 'dcb-notice dcb-' + notice.kind,
          role: notice.kind === 'error' ? 'alert' : 'status',
        }, [notice.text, notice.hint
          ? h('div', { key: 'h', className: 'dcb-hint' }, notice.hint) : null]) : null,
        busy && !shown.length ? h('div', { key: 'l', className: 'dcb-muted' }, c.loading) : null,
        !busy && !shown.length ? h('div', { key: 'e', className: 'dcb-muted' }, [
          h('div', { key: 'a' }, query.trim() ? c.noMatch : c.empty),
          query.trim() ? null
            : h('div', { key: 'b', className: 'dcb-hint' }, c.emptyHint),
        ]) : null,
        h('div', { key: 'list', className: 'dcb-list' }, groups.flatMap((g) => [
          // 组标题：项目会话 / 非项目会话
          h('div', { key: 'gh-' + g.key, className: 'dcb-group' }, g.label),
          ...g.rows.map((r) => {
          const isPicked = picked && picked.native_id === r.native_id && picked.cwd === r.cwd
          return h('div', {
            key: r.native_id + ':' + r.cwd,
            className: 'dcb-row' + (isPicked ? ' dcb-sel' : ''),
          }, [
            h('div', { key: 'top', className: 'dcb-top' }, [
              h('div', { key: 'a', className: 'dcb-title' }, [
                h('span', { key: 'n' }, r.title || ''),
                // 阶段标识：描述**本轮**怎么结束的。三档都可接续，只是提示不同。
                r.phase === 'interrupted'
                  ? h('span', { key: 'ph', className: 'dcb-badge-int' }, c.interrupted)
                  : r.phase === 'settled'
                    ? h('span', { key: 'ph', className: 'dcb-badge-set' }, c.settled)
                    : r.phase === 'unsettled'
                      ? h('span', { key: 'ph', className: 'dcb-badge-uns' }, c.unsettled)
                      : null,
                r.status === 'incomplete'
                  ? h('span', { key: 'w', className: 'dcb-badge-warn', title: r.note || '' },
                      c.incomplete) : null,
              ]),
              h('button', {
                key: 'p', type: 'button', className: 'dcb-pick',
                onClick: (ev) => pick(r, ev),
              }, c.pick),
            ]),
            isPicked ? h('div', { key: 'd', className: 'dcb-detail' }, [
              h('div', { key: 'meta', className: 'dcb-sub' }, [
                h('span', { key: 'p' }, r.cwd || ''),
                h('span', { key: 't' }, r.last_activity || ''),
              ]),
              h('div', { key: 'h', className: 'dcb-goal' }, r.goal_head || ''),
              r.goal_tail && r.goal_tail !== r.goal_head
                ? h('div', { key: 'l', className: 'dcb-muted' }, r.goal_tail) : null,
              preview && preview.body
                ? h('pre', { key: 'pv', className: 'dcb-preview' }, preview.body) : null,
              preview ? h('div', { key: 'act', className: 'dcb-actions' }, [
                h('button', {
                  key: 'i', type: 'button', className: 'dcb-primary', onClick: insert,
                }, c.insert),
                h('button', {
                  key: 'c', type: 'button', className: 'dcb-ghost', onClick: copy,
                }, c.copy),
              ]) : null,
            ]) : null,
          ])
          }),
        ])),
        filtered.length > RECENT_LIMIT && !query.trim()
          ? h('button', {
              key: 'm', type: 'button', className: 'dcb-ghost dcb-more',
              onClick: (ev) => { ev.stopPropagation(); setExpanded((v) => !v) },
            }, expanded ? c.less : c.more + ' (' + (filtered.length - RECENT_LIMIT) + ')')
          : null,
        skippedWarnings.length
          ? h('div', { key: 'w', className: 'dcb-hint dcb-warnfoot' },
              c.skippedLogs(skippedWarnings.length)) : null,
        ...metaWarnings.map((w, i) => h('div', {
          key: 'mw' + i, className: 'dcb-hint dcb-metafoot', role: 'status',
          title: w.reason || '',
        }, [
          h('div', { key: 'a', className: 'dcb-metatitle' },
            w.kind === 'project-ambiguous' ? c.metadataAmbiguous : c.metadataUnavailable),
          h('div', { key: 'b' },
            w.kind === 'project-ambiguous' ? c.metadataAmbiguousHint : c.metadataUnavailableHint),
        ])),
      ]) : null,
    ])
  }
}

const STYLE = `
.dcb-root{position:relative;display:inline-flex;align-items:center}
.dcb-btn{font:inherit;color:inherit;background:transparent;border:1px solid transparent;
  border-radius:6px;padding:2px 8px;cursor:pointer;opacity:.75;white-space:nowrap}
.dcb-btn:disabled{opacity:.4;cursor:default}
.dcb-btn:hover,.dcb-btn:focus-visible{opacity:1;background:rgba(127,127,127,.12);outline:none}
.dcb-dialog{position:absolute;bottom:calc(100% + 6px);left:0;z-index:40;width:min(520px,80vw);
  max-height:60vh;overflow:auto;background:Canvas;color:CanvasText;border:1px solid rgba(127,127,127,.35);
  border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.18);padding:10px;font:inherit;font-size:13px}
.dcb-hd{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.dcb-x{background:transparent;border:0;color:inherit;cursor:pointer;font-size:16px;line-height:1;opacity:.6}
.dcb-x:hover{opacity:1}
.dcb-search{width:100%;box-sizing:border-box;font:inherit;color:inherit;background:rgba(127,127,127,.08);
  border:1px solid rgba(127,127,127,.25);border-radius:6px;padding:4px 8px;margin-bottom:8px}
.dcb-list{margin:0;padding:0}
.dcb-group{font-size:11px;opacity:.55;padding:6px 6px 2px;letter-spacing:.02em}
.dcb-row{padding:4px 6px;border-radius:6px}
.dcb-row:hover{background:rgba(127,127,127,.08)}
.dcb-sel{background:rgba(127,127,127,.12)}
.dcb-top{display:flex;align-items:center;gap:8px;justify-content:space-between}
.dcb-title{display:flex;gap:6px;align-items:center;font-weight:500;min-width:0}
.dcb-pick{font:inherit;font-size:11px;cursor:pointer;border-radius:5px;padding:1px 8px;
  border:1px solid rgba(127,127,127,.35);background:transparent;color:inherit;opacity:.75;flex:none}
.dcb-pick:hover{opacity:1;background:rgba(127,127,127,.12)}
.dcb-sub{display:flex;gap:8px;font-size:11px;opacity:.6;margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dcb-goal{margin-top:4px}
.dcb-badge-warn{font-size:10px;padding:0 5px;border-radius:4px;
  background:rgba(200,140,0,.18);color:inherit;opacity:.9;flex:none}
.dcb-badge-int{font-size:10px;padding:0 5px;border-radius:4px;flex:none;
  background:rgba(210,90,60,.20);color:inherit;opacity:.95}
.dcb-badge-set{font-size:10px;padding:0 5px;border-radius:4px;flex:none;
  background:rgba(120,120,120,.16);color:inherit;opacity:.75}
.dcb-badge-uns{font-size:10px;padding:0 5px;border-radius:4px;flex:none;
  background:rgba(200,140,0,.18);color:inherit;opacity:.9}
.dcb-detail{margin-top:6px;padding-top:6px;border-top:1px solid rgba(127,127,127,.2)}
.dcb-preview{max-height:180px;overflow:auto;margin:6px 0;padding:6px;font-size:11px;
  background:rgba(127,127,127,.07);border-radius:6px;white-space:pre-wrap;word-break:break-word}
.dcb-actions{display:flex;gap:8px;margin-top:6px}
.dcb-primary{font:inherit;cursor:pointer;border-radius:6px;padding:3px 10px;
  border:1px solid rgba(127,127,127,.4);background:rgba(127,127,127,.16);color:inherit}
.dcb-ghost{font:inherit;cursor:pointer;border-radius:6px;padding:3px 10px;
  border:1px solid transparent;background:transparent;color:inherit;opacity:.8}
.dcb-ghost:hover{opacity:1;background:rgba(127,127,127,.10)}
.dcb-muted{opacity:.6;padding:4px 2px;font-size:11px}
.dcb-hint{font-size:11px;opacity:.65;margin-top:2px}
.dcb-notice{margin:6px 0;padding:6px 8px;border-radius:6px;font-size:12px}
.dcb-error{background:rgba(200,60,60,.14)}
.dcb-warn{background:rgba(200,140,0,.14)}
.dcb-ok{background:rgba(60,160,90,.14)}
.dcb-more{margin-top:6px;width:100%}
.dcb-warnfoot{margin-top:6px}
.dcb-metafoot{margin-top:6px;padding:6px 8px;border-radius:6px;
  background:rgba(200,140,0,.12);color:inherit;opacity:.95}
.dcb-metatitle{font-weight:600;margin-bottom:2px}
`

function apply(ctx) {
  if (typeof document !== 'undefined' && !document.getElementById('dcb-style')) {
    const el = document.createElement('style')
    el.id = 'dcb-style'
    el.textContent = STYLE
    document.head.appendChild(el)
  }

  const getShell = (sessionId) => {
    if (!sessionId) return null
    try {
      const conversation = ctx.get('conversation')
      const shells = conversation && conversation.input
      if (!shells) return null
      // 按 id 取 shell；input.for 要的是 Session ctx，不能传 id
      const byShell = typeof shells.shell === 'function' ? shells.shell(sessionId) : null
      return byShell || null
    } catch { return null }
  }

  const Button = createHandoffButton(React, { api: createApi(), getShell })

  // 延迟注册：等父级声明 slot 后再注册（官方 SlotCore 对未声明 slot 会抛错）
  ctx.slots.inject('conversation.input.left', () => ctx.slots.register({
    name: 'conversation.input.left',
    id: 'dsh-codex-bridge',
    order: 30,
    label: () => '交接',
  }, Button))
}

    return {
      name: 'dsh-codex-bridge',
      inject: ['slots', 'conversation'],
      apply,
    };
  },
});
