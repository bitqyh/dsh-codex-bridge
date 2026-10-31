# dsh-codex-bridge

Codex ↔ DSH 跨 agent 接力：会话采集、自动存档、中断恢复提示。

**一个包，一条命令安装。**

## 安装

```bash
dsh plugin --profile desktop add dsh-codex-bridge
```

然后跑安装命令（**不会自动改文件**，见下方说明）：

```bash
npx dsh-codex-bridge doctor          # 先只读检查
npx dsh-codex-bridge setup           # 看计划（不写文件）
npx dsh-codex-bridge setup --yes     # 真正安装 DSH 侧
npx dsh-codex-bridge setup-codex --yes   # 安装 Codex 侧
```

最后：

1. 在 `~/.dsh/profiles/desktop/cordis.patch.yml` 加：

```yaml
- insert:
    - id: codex-dsh-bridge
      name: 'dsh-codex-bridge'
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
`~/Library/Application Support/ai-memory/hooks/dsh/` 写 12 个文件、覆盖
`hooks.json`——都在项目目录之外。装一个 npm 包不预期会改这些。

所以改成显式命令：**默认只打印计划，加 `--yes` 才执行**，且改任何文件前先备份。

## 命令

| 命令 | 作用 |
|---|---|
| `dsh-codex-bridge doctor` | 只读检查两侧状态 |
| `dsh-codex-bridge setup` | 安装 DSH 侧（先看计划） |
| `dsh-codex-bridge setup --yes` | 真正执行 |
| `dsh-codex-bridge setup-codex` | 安装 Codex 侧（合并，保留原有其他事件） |
| `dsh-codex-bridge paths` | 打印所有相关路径 |

选项：`--server=<url>` 覆盖 ai-memory 地址（默认 `http://127.0.0.1:49374`）。

## 它提供什么

| 能力 | 说明 |
|---|---|
| 会话采集 | prompt、工具调用、轮次边界，上报 ai-memory |
| 自动存档 | 每轮结束刷新可恢复快照（零 token） |
| 中断检测 | 限速扫描磁盘日志，发现额度中断/崩溃的会话并注入提示 |

## 前置条件

需要**已安装并运行 ai-memory 服务**（本包不包含）：

```bash
launchctl list | grep ai-memory
curl -s http://127.0.0.1:49374/healthz   # → {"status":"ok"}
```

没装见 https://github.com/akitaonrails/ai-memory

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
├── src/index.js      插件主体（中断扫描 + 注入）
├── bin/cli.mjs       安装/诊断命令
├── assets/hooks/     7 个 hook 脚本
├── assets/tools/     存档/扫描/验收工具
└── test-plugin.mjs   11 项离线测试
```

## 测试

```bash
node test-plugin.mjs
```

覆盖：注册事件、只注入可操作项、活跃会话不提示、去重、限速、脚本缺失静默、
开关、downstream 非 enter 时不注入。

## 边界

- **不包含 ai-memory 服务本身**，必须独立安装运行。
- **Codex 侧需要重新信任钩子**（信任哈希绑定命令），改完不信任会静默跳过。
- **插件只发现和提示中断**，不自动写恢复交接。
- 中断期间不会有任何自动动作（进程已死），只能等下次有 agent 活着时补。
