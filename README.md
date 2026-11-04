# @bitqyh/dsh-codex-bridge

Codex ↔ DSH 跨 agent 接力：会话采集、自动存档、中断恢复提示。

**一个包，一条命令安装。**

> **只想用中断恢复？** 直接在项目目录跑
> `dsh-codex-bridge resume --from-agent codex` 即可，**不需要**注册插件、
> 装 profile 或重启 DSH。
> 下面「安装」段落装的是**事件插件**（会话采集/自动存档/中断提示），
> 那套需要注册与重启；**它不是使用 `resume` 的前置条件**。

## 安装

```bash
dsh plugin --profile desktop add @bitqyh/dsh-codex-bridge
```

然后跑安装命令（**不会自动改文件**，见下方说明）：

```bash
npx @bitqyh/dsh-codex-bridge doctor          # 先只读检查
npx @bitqyh/dsh-codex-bridge setup           # 看计划（不写文件）
npx @bitqyh/dsh-codex-bridge setup --yes     # 真正安装 DSH 侧
npx @bitqyh/dsh-codex-bridge setup-codex --yes   # 安装 Codex 侧
```

> 本包的 `cordis.patch.yml` 已自带 **UI 入口**（「交接」按钮），`dsh plugin add`
> 装完重启 DSH 即可使用，不需要再手动改 profile patch。
> 只有想额外启用**事件扫描插件**（中断提示）时才需要下面这一步。

最后（可选，仅事件插件）：

1. 在 `~/.dsh/profiles/desktop/cordis.patch.yml` 加：

```yaml
- insert:
    - id: dsh-codex-bridge-events
      name: '@bitqyh/dsh-codex-bridge'
```

2. 重启 DSH
3. Codex 里 `/hooks` 重新信任（改了命令哈希会失效，且**静默跳过**）

## 为什么不用 postinstall 自动装

两个原因，都实测过：

**1. 技术上跑不起来。** DSH profile 用 pnpm 11，`pnpm-workspace.yaml` 的
`allowBuilds` 白名单会拦掉未批准的安装脚本：

```
[ERR_PNPM_IGNORED_BUILDS] Ignored build scripts: ...
```

本机白名单只有 `cloudflared`，所以 `postinstall` 根本不会执行。

**2. 产品上不该这么做。** 安装要往
`~/Library/Application Support/ai-memory/hooks/dsh/` 写 13 个文件
（7 个 hook 脚本 + 6 个工具脚本）、覆盖 `hooks.json`——都在项目目录之外。
装一个 npm 包不预期会改这些。

所以改成显式命令：**默认只打印计划，加 `--yes` 才执行**，且改任何文件前先备份。

## 命令

| 命令 | 作用 |
|---|---|
| `dsh-codex-bridge resume` | 构建中断会话的恢复上下文（只读） |
| `dsh-codex-bridge doctor` | 只读检查两侧状态 |
| `dsh-codex-bridge setup` | 安装 DSH 侧（先看计划） |
| `dsh-codex-bridge setup --yes` | 真正执行 |
| `dsh-codex-bridge setup-codex` | 安装 Codex 侧（合并，保留原有其他事件） |
| `dsh-codex-bridge paths` | 打印所有相关路径 |

选项：`--server=<url>` 覆盖 ai-memory 地址（默认 `http://127.0.0.1:49374`）。

## 接续已有聊天

**没有「已完成的任务」**——只有「本轮已收尾」和「因故中断」，后续都还会有新需求。
所以接续既用于中断恢复，也用于上一轮正常结束之后的后续需求。

三种入口，按顺手程度选：

**1. DSH 界面按钮**：点输入框下方的「交接」→ 按**聊天名称**选 → 看预览 →
点「插入当前对话」，内容进草稿，自己确认后再发送。

列表**按具体项目分组**（组名就是项目名），未归属的合成「非项目会话」放最后；
组顺序按组内最新会话时间倒序。搜索作用于全部会话，展开后行数只增不减。
分组依据与它的边界见下面「项目归属」一节。

**只插入草稿，绝不自动发送。** 按钮只调 `insertText`，不碰 `setDraft`/`paste`/`submit`。

**2. Codex skill**：说「用接续任务接着 DSH 没做完的任务」，或 `$resume-task`。

**3. 命令行**（备用入口）：

> 在同一项目目录运行 `dsh-codex-bridge resume --from-agent codex`，先核验现场，再继续。

反向（从 DSH 切到 Codex）把参数改成 `--from-agent dsh`。

候选里每个聊天带一个**本轮**标识：`本轮已收尾` / `因故中断` / `未收尾/需核验`。
它说的是这一轮怎么结束的，不是任务完成度。徽标是**可接续聊天数**。

```bash
# 备用 agent 在项目目录里跑；只读，不写 handoff/DB，不调用模型
dsh-codex-bridge resume --from-agent codex

# 两边工作目录不一致时，显式给路径
dsh-codex-bridge resume --from-agent codex --cwd '<实际项目路径>'

# 只有出现多个可接续来源时才需要指定（必须完整 native id，不猜 latest）
dsh-codex-bridge resume --from-agent codex --session <完整native-id>
```

输出是一份机械提取的恢复上下文：定位信息、任务目标（人类原话）、已确认成功
的操作、失败/未知/待审批、最后一条助手可见输出、接续前必须先核验的清单。

**它是证据，不是新授权。** 备用 agent 按你当前的指令继续任务时，用的是它自己的
模型和工具；`resume` 本身只读日志。

退出码：`0` ready、`2` no-candidate、`3` needs-selection、`4` incomplete。
`incomplete` 表示信息不足（缺目标、未知 scope、片段无法排序），**不要当通过**。

## 它提供什么

| 能力 | 说明 |
|---|---|
| 会话采集 | prompt、工具调用、轮次边界，上报 ai-memory |
| 自动存档 | 每轮结束刷新可恢复快照（零 token） |
| 中断检测 | 限速扫描磁盘日志，发现额度中断/崩溃的会话并注入提示 |
| **手动恢复** | `resume` 只读解析原生日志，构建中断会话的恢复上下文；**可离线、无 DB 降级**，不依赖服务运行 |
| **「交接」按钮** | 输入框下方按**具体项目**分组列出可接续的 Codex 聊天，只把恢复内容插入草稿 |

## 和已有插件的区别

市场里已有一批「把外部会话带进 DSH」的插件。本插件的定位是
**不导入、不切换上下文、不写回**——只把会话列清楚，并诚实标注归属判断有多可靠。

与 `dsh-codex-history`（平铺列表 + 导入）、`dsh-assembly.resume`（按路径末段命名分组 +
导入会话）、`dsh-takeover`（新建会话接管）、`task-passport`（任务状态搬运）的逐条对比
见 [COMPARISON.md](./COMPARISON.md)。

## 开发与发布

改了代码要发新版本时，完整流程（含 npm scope 组织、token 类型、202 异步落地、
市场 PR 的几个坑）见 [RELEASE.md](./RELEASE.md)。

```bash
npm install     # 首次：装 peer 依赖
npm test        # 201 项
npm pack --dry-run   # 发版前看清单
```

## 前置条件

### 运行时依赖一览

**这个包不是「一条命令装完就什么都不用管」。** 它复用你机器上已有的 `python3`
读原生日志，装之前先对一下表：

| 依赖 | 谁需要 | 缺了会怎样 |
|---|---|---|
| **Node.js ≥ 20** | 全部 | 插件不加载 |
| **`python3` ≥ 3.8** | 「交接」列表、`resume`、`doctor` | **列表静默为空**，前端显示「读取失败」；CLI 无输出 |
| `zstd` | 只读 **DSH 侧**会话时需要 | DSH 侧会话全部读不出，逐个进「已跳过」；Codex 侧不受影响 |
| **ai-memory 服务** | 采集 / 自动存档 / 中断提示 | 「交接」列表和 `resume` 仍可用（它们不依赖服务） |
| **Codex**（CLI 或 Desktop） | 读 Codex 会话 | Codex 侧没有可接续聊天 |

自检：

```bash
node -v          # ≥ 20
python3 -V       # ≥ 3.8（只用标准库，不需要 pip 装任何东西）
command -v zstd  # 只读 DSH 侧才需要
```

`python3` 只用到标准库（`json`/`sqlite3`/`subprocess`/`pathlib` 等），
**不需要 pip / requirements.txt**。已实测 macOS 自带的 `/usr/bin/python3`（3.9）
可用；5 个工具脚本在 3.9 下全部编译通过。

> 🪟 **Windows 暂未适配。** 代码里直接 `spawnSync("python3", ...)`，而 Windows
> 上通常只有 `python` 或 `py`，没有 `python3`。没有做 `process.platform` 分支，
> 所以 Windows 上「交接」列表会是空的。装之前请确认 `python3` 在 PATH 里，
> 否则等后续版本。

> ⚠️ **`python3` 是硬依赖，而且缺了不会报错。** 实测：PATH 里没有 `python3` 时
> CLI 退出码 1、stdout 为空、stderr 也为空，界面只说「读取失败」。
> 如果你装完发现「交接」列表一直是空的，先 `command -v python3`。

### 按你要用的能力分

**① 采集 / 自动存档 / 普通 handoff** — 需要已安装并运行 ai-memory 服务
（本包不包含）：

```bash
launchctl list | grep ai-memory
curl -s http://127.0.0.1:49374/healthz   # → {"status":"ok"}
```

没装见 https://github.com/akitaonrails/ai-memory

**② `resume` 手动恢复** — **不需要服务运行**：

- 直接读本机原生日志（Codex `~/.codex/sessions`、DSH sessions 目录）。
- **没有 ai-memory 数据库也能用**：正文全部由原生日志构建，恢复内容一个字节都不少。
- **只读 DSH 侧会话时需要 `zstd`**（DSH 日志是 `session.v4.jsonl.zstd`）。
  没有 zstd 时 DSH 侧会话会全部读不出并逐个进「已跳过」统计，Codex 侧不受影响
  ——因为 Codex 日志是未压缩的 `.jsonl`。
- **有 DB 时会多做三项一致性核对**（这是 DB 唯一的作用，它不是恢复内容的来源）：
  1. 日志末尾 vs 钩子最后上报 → 钩子是否在丢事件
  2. 日志末尾 vs 存档时间 → **存档是否落后于真实进度**（超过 90 秒即报，
     额度中断/崩溃时最容易出现）
  3. 会话是否在 DB 登记、agent kind 是否一致
  缺 DB 时这三项跳过，正文开头标一行「缺 DB 校验」。
- 注意 `ended_at` **不参与**进度新旧判断：它是「钩子捕获到结束的时刻」，
  与日志末尾实测可差 ±16 小时且方向不定。

## 配置

```js
{
  scriptPath: "",         // 留空自动解析（已安装位置 → 包内自带）
  throttleSeconds: 600,   // 扫描限速窗口
  sinceHours: 24,         // 只看最近 N 小时有活动的会话
  maxReported: 3,         // 最多提示几条
  enabled: true,
}
```

## 设计要点

### 为什么挂 UserPromptSubmit 而不是 SessionStart

常驻会话不会重新触发 `SessionStart`。实测一个长期会话：

| 钩子 | 触发次数 |
|---|---|
| PreToolUse / PostToolUse | 821 / 821 |
| UserPromptSubmit | 63 |
| **SessionStart** | **0** |

用户日常把 agent 挂后台，挂 `SessionStart` 基本不会跑。

### 为什么 hooks 全用脚本模式

DSH 钩子在会话沙箱里运行。`workspace-write` 模式下写不了
`~/Library/Application Support/ai-memory`，而 ai-memory 原生二进制
**先落 spool 再发送**，写不进去就整条丢弃：

```
ai-memory hook warning: failed to spool lifecycle event
```

实测某会话 762 次钩子调用里 **744 次**这样丢失。所以本包生成的
`hooks.json` 7 个事件全部走脚本直连。

### 为什么插件只提示、不自动写交接

恢复摘要需要理解「他做到哪一步」，机械解析给不了。所以：
插件负责**发现和提示**，接手的 agent 读 raw 后自己写恢复交接。

## 包内结构

```
dsh-codex-bridge/
├── src/index.js      事件插件（中断扫描 + 注入）
├── src/ui-server.js  「交接」按钮的后端（只读接口）
├── src/ui-client.js  「交接」按钮的前端（分组渲染）
├── bin/cli.mjs       安装/诊断/恢复命令
├── cordis.patch.yml  自带 UI 入口
├── assets/hooks/     7 个 hook 脚本
├── assets/tools/     存档/扫描/恢复/验收工具
├── tests/            回归测试（54+19+8+37+52 项）
└── test-plugin.mjs   11 项插件逻辑测试
```

## 测试

```bash
npm install                           # 先装 peer 依赖（@deepseek-ai/dsh-llm）
npm test                              # 全部 201 项
node test-plugin.mjs                  # 11 项：插件逻辑
node tests/ui-tests.mjs               # 16 项：交接按钮前端
python3 tests/batch1-tests.py         # 54 项：存档语义 + session 选择
python3 tests/batch2-tests.py         # 19 项：验收工具 fixture
python3 tests/batch3-tests.py         #  8 项：中断恢复扫描
python3 tests/batch4-resume-tests.py  # 37 项：resume 恢复上下文 + DB 一致性
python3 tests/project-scope-tests.py  # 52 项：项目归属判定（含歧义）
```

> 从 GitHub 克隆后直接 `npm test` 会报 `Cannot find package '@deepseek-ai/dsh-llm'`
> ——它声明在 `peerDependencies`（装进 DSH profile 时由宿主提供）。
> 只在仓库里跑测试需要先 `npm install`，`devDependencies` 会把它装上。
> 已作为插件装进 profile 的开发副本不需要，因为它会沿目录树解析到 profile 的
> `node_modules`。

插件逻辑测试覆盖：注册事件、只注入可操作项、活跃会话不提示、去重、限速、
脚本缺失静默、开关、downstream 非 enter 时不注入。

## 项目归属：怎么分组，以及它不知道什么

「交接」列表按**每个具体项目各自一组**显示，未归属的合成「非项目会话」放最后。
组顺序按组内最新一条会话的时间倒序（最近在做的项目排最前），组内仍按时间倒序。

**分组依据是「目录匹配」，不是会话归属。** 这点必须说清楚：

| 依据 | 含义 | 代码里的 `project_basis` |
|---|---|---|
| `threads.project_id` | Codex 记录的**会话所属项目**，权威 | `thread` |
| `project_roots.path` + cwd 路径边界匹配 | 只能说明 cwd 落在某个**登记目录**内 | `dir` |
| 元数据读不到 | 归属**未知**，不等于「确认非项目」 | `unavailable` |
| 目录同时登记给多个项目 | 归属**未知**，不按注册表行序猜一个 | `ambiguous` |
| 都不命中 | 确实没有依据 | `none` |

优先级 `thread` > `dir` > `ambiguous` / `unavailable` > `none`。当前 Codex 版本的
`threads.project_id` 整列为空，所以实际跑的是 `dir`；将来该列有值时自动接管。

**几个容易误解的点：**

- `threads.project_id` 全空**不代表**这些会话不属于任何项目，只说明这一版 schema
  没写入这个字段。所以元数据不可读时标「未知」，**不**降级成「确认非项目」。
- 不把「cwd 不是 home」当作项目，也不按目录名或日期形状（如 `2026-11-01`）推断——
  日期目录只有在 `project_roots` 里登记过才算项目。
- 路径边界匹配：`/a/b` **不**匹配 `/a/bc`；嵌套取最长根；尾斜杠归一。
- **同一目录登记给多个项目时不猜**：注册表里两个根路径相同，谁先出现只取决于
  SQLite 返回顺序。这时取「第一个」等于让行序静默决定归属，所以标 `ambiguous`
  并归入非项目组。（真实案例：`.../Liner-transformer` 同时挂在「论文复现」和
  「PPT制作」下。）
- 元数据不可用、或目录歧义时，列表会分别单独提示，且这些会话**没有被跳过**
  ——不计入「N 个日志无法确认归属，已跳过」的统计。

默认读 `~/.codex/state_5.sqlite`，可用环境变量 `BRIDGE_CODEX_STATE_DB`
或 `--db` 覆盖。

## 边界

- **不包含 ai-memory 服务本身**。采集、存档、普通 handoff 需要它；
  `resume` 读原生日志不依赖它；缺 DB 时只是少做三项一致性核对，不丢恢复内容。
- **Codex 侧需要重新信任钩子**（信任哈希绑定命令），改完不信任会静默跳过。
  这条只影响采集，不影响 `resume`。
- **插件只发现和提示中断**，不自动写恢复交接。
- 中断期间不会有任何自动动作（进程已死），只能等下次有 agent 活着时补。
- **没有后台派发、没有自动切换模型**；切换由你手动决定，`resume` 只负责
  把上一个 agent 的现场整理成可核验的文本。
- **未落盘的信息无法重建**：日志里没有的事件，恢复上下文里也不会有。
- 源会话如果还活着，先暂停/确认停止再接手，避免两边同时改同一个任务。
