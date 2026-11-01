/**
 * ai-memory 中断恢复提示插件。
 *
 * 解决的问题
 * ----------
 * 额度耗尽 / 崩溃时 agent 进程直接死掉，不会触发 SessionEnd，所以：
 *   - 没有自动 handoff
 *   - 那一轮没有 Stop，阶段存档停在上一轮
 *   - 数据库里的 ended_at 可能早就被写成「已结束」，但它实际还在产出内容
 *
 * 因此「有没有新工作」不能看 ended_at，也不能只看 handoff。
 *
 * 本插件在每次用户提交提示时（UserPromptSubmit 时机）**限速地**跑一次
 * 磁盘扫描，发现中断会话就把现场摘要注入上下文，让接手方知道该先读 raw。
 *
 * 为什么挂 UserPromptSubmit 而不是 SessionStart
 * --------------------------------------------
 * 常驻会话不会重新触发 SessionStart。实测某个长期会话：SessionStart 触发 0 次，
 * UserPromptSubmit 63 次。所以只有 UserPromptSubmit 在「两边都常驻」的用法下
 * 才真的会跑。
 *
 * 设计约束
 * --------
 * - 不启动后台定时器（无 watcher / launchd）
 * - 不调用模型（纯磁盘 + sqlite 只读）
 * - 限速：默认 10 分钟内最多扫一次，避免每条消息都跑
 * - 只读：不写 ai-memory 库、不领 handoff、不改配置
 * - 失败静默：扫描出错不能影响正常对话
 *
 * @module dsh-plugin-ai-memory-recovery
 */

import { execFile } from "node:child_process";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { createUserMessage } from "@deepseek-ai/dsh-llm";

const execFileAsync = promisify(execFile);

export const name = "ai-memory-recovery";

/** 只需要 agent 事件，不需要 shell / session 投影。 */
export const inject = [];

const PKG_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** ai-memory 的 hooks/dsh 目录（安装器会把脚本放这里）。 */
function defaultHooksDir() {
  const base = process.env.AI_MEMORY_DATA_DIR
    || join(homedir(), "Library", "Application Support", "ai-memory");
  return join(base, "hooks", "dsh");
}

/** 优先用已安装位置，其次用包内自带（未跑安装器时）。 */
function defaultScriptPath() {
  const installed = join(defaultHooksDir(), "recover-interrupted.py");
  if (existsSync(installed)) return installed;
  return join(PKG_ROOT, "assets", "tools", "recover-interrupted.py");
}

/**
 * 配置默认值。
 *
 * `resolveConfig` 会调 `Config['~standard'].validate(config)`——**不是**读这些键本身。
 * 所以默认值必须由 `validate` 合并进去，只写在这里不会生效：cordis 传进来的原始
 * config 是 patch 里那一小块，缺的键不会自动补。
 */
const CONFIG_DEFAULTS = Object.freeze({
  /** 扫描脚本路径。留空则自动解析（已安装位置 → 包内自带）。 */
  scriptPath: "",
  /** 限速窗口（秒）。同一窗口内最多扫一次。 */
  throttleSeconds: 600,
  /** 只看最近多少小时有日志活动的会话。 */
  sinceHours: 24,
  /** 最多注入多少条中断会话，避免刷屏。 */
  maxReported: 3,
  /** 总开关。 */
  enabled: true,
});

/** 非负有限数，否则用默认值。手改配置写成字符串/NaN 不该让启动失败。 */
function nonNegative(value, fallback) {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

/**
 * 插件配置 schema。
 *
 * 暴露给 cordis 的是 `~standard.validate`（Standard Schema v1）：
 * `resolveConfig` 直接调它，**没有 `~standard` 就抛 TypeError**，
 * 插件在启动阶段就挂掉（曾经的真实故障：`Cannot read properties of undefined
 * (reading 'validate')`）。validate 永不抛错——一个坏配置不该让整个应用起不来，
 * 读不懂的值一律回退默认值。
 */
export const Config = {
  ...CONFIG_DEFAULTS,
  type: "object",
  meta: { default: {} },
  toJSON() {
    return { ...CONFIG_DEFAULTS };
  },
  "~standard": {
    version: 1,
    vendor: "dsh-codex-bridge",
    validate(value) {
      const src = value !== null && typeof value === "object" ? value : {};
      const scriptPath =
        typeof src.scriptPath === "string" ? src.scriptPath : CONFIG_DEFAULTS.scriptPath;
      const sinceHours = nonNegative(src.sinceHours, CONFIG_DEFAULTS.sinceHours);
      return {
        value: {
          scriptPath,
          throttleSeconds: nonNegative(src.throttleSeconds, CONFIG_DEFAULTS.throttleSeconds),
          // 0 小时等于「扫全部」，没有意义，退回默认窗口
          sinceHours: sinceHours > 0 ? sinceHours : CONFIG_DEFAULTS.sinceHours,
          maxReported: Math.max(1, Math.floor(nonNegative(src.maxReported, CONFIG_DEFAULTS.maxReported))),
          enabled: typeof src.enabled === "boolean" ? src.enabled : CONFIG_DEFAULTS.enabled,
        },
      };
    },
  },
};

const STATE_FILE = () => join(tmpdir(), "dsh-ai-memory-recovery-last-scan.json");

function readState() {
  try {
    const f = STATE_FILE();
    if (!existsSync(f)) return {};
    return JSON.parse(readFileSync(f, "utf8")) || {};
  } catch {
    return {};
  }
}

function writeState(obj) {
  try {
    writeFileSync(STATE_FILE(), JSON.stringify(obj), { mode: 0o600 });
  } catch {
    /* 状态文件写不了不影响主流程 */
  }
}

/** 只在「确实需要处理」时才值得打扰用户。 */
const ACTIONABLE = new Set(["stale_autosave", "handoff_outdated", "no_db_session"]);

function formatAge(us) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - us / 1_000_000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function renderNotice(rows) {
  const lines = [
    "⚠ ai-memory 检测到可能有未完成的中断会话。",
    "",
    "这些会话没有自动交接，存档也可能落后于真实进度。"
      + "不要只看 ended_at 或已有 handoff，请先读原始日志确认。",
    "",
  ];
  for (const r of rows) {
    lines.push(`- ${r.agent} ${r.native_id}`);
    lines.push(`  最后活动：${formatAge(r.last_log_activity_us)}（${r.cwd || "cwd 未知"}）`);
    lines.push(`  原因：${r.verdict_detail}`);
    if (r.log_path) lines.push(`  日志：${r.log_path}`);
  }
  lines.push("");
  lines.push("需要接手时，先用 recover-interrupted.py 看完整清单，再按 raw 恢复。");
  return lines.join("\n");
}

/**
 * 跑一次扫描。返回需要提示的行（已按可操作性过滤）。
 * 任何失败都返回空数组——诊断不能破坏正常对话。
 */
async function scan(config) {
  const script = config.scriptPath || defaultScriptPath();
  if (!script || !existsSync(script)) return [];

  try {
    const { stdout } = await execFileAsync(
      "python3",
      [script, "--since-hours", String(config.sinceHours), "--json"],
      { timeout: 20_000, maxBuffer: 8 * 1024 * 1024 },
    );
    const rows = JSON.parse(stdout);
    if (!Array.isArray(rows)) return [];
    return rows
      .filter((r) => ACTIONABLE.has(r.verdict))
      .slice(0, Math.max(1, config.maxReported));
  } catch {
    return [];
  }
}

export function apply(ctx, config) {
  if (config.enabled === false) return;

  ctx.on("agent/pre-step", async ({ agent, messages, turn, signal }, next) => {
    const downstream = await next();
    // 只在该轮真的要进入时注入
    if (downstream.kind !== "enter") return downstream;
    if (!messages || messages.length === 0) return downstream;

    // 限速：同一窗口内只扫一次
    const now = Date.now();
    const state = readState();
    const windowMs = Math.max(0, config.throttleSeconds || 0) * 1000;
    if (windowMs > 0 && state.lastScanAt && now - state.lastScanAt < windowMs) {
      return downstream;
    }
    writeState({ ...state, lastScanAt: now });

    let rows = [];
    try {
      rows = await scan(config);
    } catch {
      return downstream;
    }
    if (rows.length === 0) return downstream;

    // 同一批中断会话不重复提示（按 native_id 记忆）
    const seen = new Set(state.reported || []);
    const fresh = rows.filter((r) => !seen.has(`${r.agent}:${r.native_id}`));
    if (fresh.length === 0) return downstream;

    writeState({
      lastScanAt: now,
      reported: [...seen, ...fresh.map((r) => `${r.agent}:${r.native_id}`)].slice(-50),
    });

    const notice = createUserMessage({
      content: [{ type: "text", text: renderNotice(fresh) }],
      source: { kind: "ai-memory-recovery" },
    });

    return { ...downstream, messages: [...downstream.messages, notice] };
  });
}
