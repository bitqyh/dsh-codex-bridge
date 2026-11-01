# 与已有插件的差异

DSH 插件市场里已经有一批「把外部 agent 会话带进 DSH」的插件。这份文档说明
`@bitqyh/dsh-codex-bridge` 与它们的实际差异，依据是各仓库的源码与 README
（核对日期 2026-11-01），不是凭简介猜测。

## 一句话定位

**别人做「把会话搬进来」，我们做「把会话列清楚，并且诚实地告诉你哪些列不清」。**

具体说：不导入、不切换上下文、不写回 Codex；只在输入框下方按**具体项目**分组
列出可接续的 Codex 聊天，把恢复内容插入**草稿**，发不发由你决定。

## 逐条对比

### vs `dsh-codex-history`（已收录，2 stars）

| 维度 | dsh-codex-history | 本插件 |
|---|---|---|
| 列表形态 | **平铺列表**，cwd 放在副标题里（源码 `client/client.js` 无分组逻辑） | **按具体项目分组**，组名 = 项目名 |
| 数据源 | 轮询 `~/.codex/sessions/**/rollout-*.jsonl` | 会话日志 + `~/.codex/state_5.sqlite` 的 `projects`/`project_roots` 注册表 |
| 会话去向 | 整段**导入当前 DSH 对话**，或填进输入框 | 只把恢复内容插入**草稿**，不发送、不导入 |
| 后台行为 | 每 5 秒轮询索引；新消息**增量回灌**到进行中的对话 | 无后台轮询、无回灌；按钮点了才读 |
| 归属判定 | 不涉及（不分组） | 四级 `thread`/`dir`/`unavailable`/`none` + `ambiguous`，全部如实标注 |

差异实质：它解决「把 Codex 历史变成 DSH 的一等公民」；我们解决「在列表里一眼看出
哪条属于哪个项目，并且知道这个判断有多可靠」。**两者不冲突，可以同时装。**

### vs `dsh-assembly.resume`（已收录，2 stars）

这是最接近的一个——它也按项目分组。差异在**分组的依据**：

它的 `projectNameFromPath()`（源码 `src/client/project-groups.ts`）是：

```ts
return path.replace(/\/+$/u, '').split('/').at(-1) || path
```

也就是**取 cwd 路径的最后一段当项目名**，而 `codexProjectPath()` 直接把 cwd 原样
当作 `projectPath`。后果是本机会出现这些组名：

| cwd | 它的组名 | 本插件的组名（`projects.name`） |
|---|---|---|
| `~/Documents/GPToutput` | `GPToutput` | **文件处理** |
| `~/Projects/github` | `github` | **Github项目集** |
| `~/Projects/vscode/bitqyh.github.io` | `bitqyh.github.io` | **我的个人笔记** |
| `~/Documents/AI_use` | `AI_use` | **订阅转API** |
| `~/Documents/我的论文/材料热处理分析` | `材料热处理分析` | **热处理材料论文精读** |
| `~/Projects/github/ppt-master` | `ppt-master` | **PPT制作** |

实测本机 19 个注册根里有 **9 个**项目名 ≠ 路径末段。也就是说，对一半以上的项目，
路径末段给出的名字不是用户给项目起的名字。

另外它会把会话**导入 DSH**并绑定工作区，由 DSH Agent 继续；我们只插入草稿。

### vs `dsh-takeover`（未收录到 npm，走 GitHub Release）

它做的是完整的接管闭环：拉取八家 agent 会话 → 蒸馏成六段协议卡 → 写进
`~/.handoff/pending/` 收件箱 → 任何 agent 开局取件。工程完成度很高（有协议
SPEC、一致性套件、红队测试）。

差异是**产品形态**：

- 它：输入框里的「接力」按钮 → 浮层工作台 → 点「接管」**新建会话并自动切换过去**
- 我们：输入框下方「交接」按钮 → 按项目分组的列表 → 插入**当前**会话的草稿

它的设计目标是「别的 agent 干到一半，你随时接管接着掌控」，所以必然要新建会话；
我们的目标是「我在这个会话里，想把上次的现场捞过来继续」，所以只碰草稿。
它有自己的 `handoff: 1` 开放协议与文件总线，我们没有协议层。

### vs `task-passport`（已收录，10 stars）

它做**任务状态**搬运：机器可读检查点 + 乐观锁，跨 DSH/WorkBuddy/Claude Code/Codex
同步「任务进行到哪」。我们做**会话发现与呈现**：列出哪些 Codex 聊天可以接着做，
以及它们分别属于哪个项目。一个管状态序列化，一个管列表与归属。

### vs 导入类（`dsh-chat-import` 273 stars、`dsh-plugin-session-import` 8 stars）

它们把外部会话**导入成 DSH 原生会话**（`dsh-chat-import` 支持 13 家 agent 并支持
反向导出）。这是迁移工具。我们不做导入——恢复内容是给你当前会话看的文本证据，
不生成新会话、不改任何存储。

## 我们的取舍

**不做什么（有意为之）：**

- 不导入会话、不新建会话、不切换当前对话
- 不写回 Codex，不改 `~/.codex` 任何文件（全程只读）
- 不后台轮询、不自动回灌、不自动发送
- 不做跨机同步、不做协议收件箱、不做任务状态序列化

**做什么：**

1. 按**具体项目**分组，组名用 `projects.name`（用户给项目起的名字），不是路径末段
2. 归属依据**分四级如实标注**，并且区分「确实没有依据」与「有依据但判不了」
3. 只插入草稿，`insertText(text, {start, end, draftRev})`，不碰 `setDraft`/`paste`/`submit`

第 2 点是这个插件真正的设计重心。多数同类工具会在这里二选一：要么猜一个
（比如取路径末段），要么干脆不说。我们的做法是——**说不清就说不清**：

- `threads.project_id` 全空，不解读成「这些会话没有项目」，而是「这一版 schema 没写」
- 元数据读不到 → `unavailable`，绝不降级成「确认非项目」
- 同一目录登记给两个项目 → `ambiguous`，绝不按 SQLite 行序猜一个

## 一句话结论

如果你的需求是「把 Codex 历史搬进 DSH 继续聊」，`dsh-chat-import` 或
`dsh-codex-history` 更直接。如果你需要的是「在 DSH 里看清哪些 Codex 聊天能接着做、
分别属于哪个项目、这个判断有多可靠，然后自己决定要不要接」——那是本插件。
