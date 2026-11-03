# Changelog

## 0.1.0 — 2026-11-01

首个对外版本。

### 新增

- **「交接」按钮按具体项目分组**：DSH 输入框下方的可接续聊天列表，现在每个项目
  各自一组（组名 = 项目名），未归属的合成「非项目会话」固定放最后。组顺序按组内
  最新会话时间倒序，组内仍按时间倒序。
- **项目归属五档判定**：`thread`（Codex 记录的会话所属项目，权威）> `dir`（登记
  目录 + cwd 路径边界匹配）> `unavailable`（元数据读不到，归属未知）/
  `ambiguous`（目录登记给多个项目，归属未知）> `none`。
  当前 Codex 版本的 `threads.project_id` 整列为空，实际走 `dir`；该列有值后自动接管。
- **元数据不可用单独提示**：会话仍完整列出并归入非项目组，但明确标为「归属未确认」
  而非「确认非项目」，且不计入「N 个日志无法确认归属，已跳过」的统计。
- `resume` 命令行：只读解析原生日志构建恢复上下文，可离线、无 DB 降级。
- 中断检测事件插件（可选，默认不插入）。

### 修复

- **`Config` 缺少 `~standard.validate` 导致插件启动崩溃**。cordis 的 `resolveConfig`
  调的是 `Config['~standard'].validate(config)`，之前 `Config` 是普通对象字面量，
  加载即抛 `TypeError: Cannot read properties of undefined (reading 'validate')`。
  现按 Standard Schema v1 补齐，且 `validate` 永不抛错——坏配置回退默认值，
  不让整个应用起不来。
- `bin/cli.mjs` 权限从 600 改为 644，git clone 后可直接执行。
- `files` 白名单改用 `assets/hooks/*` 与 `assets/tools/*.py` 显式列举，
  `__pycache__/*.pyc` 不再被打进 npm 包。

### 新增：目录歧义不再静默猜归属

- **同一目录登记给多个项目时标 `ambiguous`，不按注册表行序猜一个**。实测本机
  `.../Liner-transformer` 同时挂在「论文复现」和「PPT制作」下，两个根路径长度相同，
  此前 `project_of()` 取先出现的那个，等于让 SQLite 返回顺序静默决定归属。
  现在返回未知，会话归入非项目组并单独提示 `project-ambiguous`。
- 前端新增第三类提示块，与「元数据不可读」「日志缺失已跳过」三者语义互不混用。
- `tests/project-scope-tests.py` 增 11 项断言（含行序反转结果不变）。
- 新增 `COMPARISON.md`：与 `dsh-codex-history` / `dsh-assembly.resume` /
  `dsh-takeover` / `task-passport` / 导入类插件的逐条差异，依据各仓库源码核对。

### 修复：DB 侧查了不用的三个字段

`db_context()` 一直在查 `last_obs_us` / `autosave_us` / `ended_at_us`，但只用了前两项
的「会话是否登记」，三个时间戳从没进入判断——DB 对恢复正文的实际贡献只有一行
「缺 DB 校验」警告。现在它们真正参与判断：

- **存档落后检测**：日志末尾比存档新超过 90 秒（且会话已不活跃）→ 明确提示
  「存档停在 X，日志已到 Y，很可能该轮没有正常结束，按日志重建的才最新」。
  实测真实数据里 23 个存档有 3 个落后，最多 20 小时（都对应额度中断）。
- **观测落后检测**：日志有内容但钩子最后上报明显更早 → 提示可能在丢事件。
- **`ended_at` 明确不参与**进度判断：实测它与日志末尾可差 ±16 小时、方向不定，
  语义是「钩子捕获到结束的时刻」而非内容终止。加测试锁住这个边界。
- 阈值（90 秒 / 5 分钟窗口）与 `recover-interrupted.py` 的 `verdict()` 一致——
  同一份数据在两个入口给不同结论比给保守结论更糟。

新增 5 项测试（含「存档正常时不误报」「活跃会话不说成崩溃」两个反向用例）。

### 变更

- 包名改为 `@bitqyh/dsh-codex-bridge`（`dsh-codex-bridge` 已被他人注册，且用途不同）。
- `peerDependencies` 从精确 pin `0.2.1-rc.2` 改为带显式预发布分支的范围，
  避免 DSH 升级后用户遇到 `ERESOLVE`。
- 补 `engines.dsh`、`repository`、`homepage`、`bugs`、`keywords`、`publishConfig`。
- 自带 `cordis.patch.yml` 只插 UI 入口；事件插件改为需手动添加，patch id
  从 `codex-dsh-bridge` 统一为 `dsh-codex-bridge-events`。
- README 更正：setup 实际写 13 个文件（7 hook + 6 工具），此前误写 12；
  测试总数 177 → 188（project-scope 41 → 52）。
