#!/usr/bin/env node
/**
 * dsh-codex-bridge — Codex ↔ DSH 接力的一站式安装/诊断命令。
 *
 * 子命令
 * ------
 *   dsh-codex-bridge setup          安装 DSH 侧（hooks 脚本 + hooks.json）
 *   dsh-codex-bridge setup-codex    安装 Codex 侧（~/.codex/hooks.json）
 *   dsh-codex-bridge doctor         只读检查两侧状态
 *   dsh-codex-bridge paths          打印所有相关路径（排查用）
 *
 * 为什么不用 postinstall
 * ---------------------
 * DSH 的 profile 用 pnpm 11，`pnpm-workspace.yaml` 的 `allowBuilds` 白名单会
 * 拦掉未批准的安装脚本（实测 `ERR_PNPM_IGNORED_BUILDS`）。而且往
 * `~/Library/Application Support` 写文件属于项目目录之外，不该在装包时偷偷做。
 * 所以安装一律由用户显式执行本命令。
 *
 * 安全
 * ----
 * - 默认先 `--dry-run` 语义：不带 --yes 只打印计划
 * - 改任何文件前先备份
 * - token 只读不打印
 * - 失败不破坏既有配置
 */

import { execFileSync } from "node:child_process";
import {
  copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync, chmodSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = resolve(HERE, "..");
const DEFAULT_SERVER = "http://127.0.0.1:49374";

const argv = process.argv.slice(2);
const cmd = argv[0] || "help";
const yes = argv.includes("--yes") || argv.includes("-y");
const server = (argv.find((a) => a.startsWith("--server=")) || "").split("=")[1]
  || process.env.AI_MEMORY_HOOK_URL || DEFAULT_SERVER;

function dataDir() {
  return process.env.AI_MEMORY_DATA_DIR
    || join(homedir(), "Library", "Application Support", "ai-memory");
}
const dshHooksDir = () => join(dataDir(), "hooks", "dsh");
const codexHooksFile = () => join(homedir(), ".codex", "hooks.json");

/** 从 auth-header 读 token，绝不打印。 */
function readToken() {
  if (process.env.AI_MEMORY_AUTH_TOKEN) return process.env.AI_MEMORY_AUTH_TOKEN;
  for (const name of ["auth-header", "auth-token"]) {
    const f = join(dataDir(), name);
    if (!existsSync(f)) continue;
    let line = readFileSync(f, "utf8").trim();
    if (line.toLowerCase().startsWith("authorization:")) {
      line = line.slice(line.indexOf(":") + 1).trim();
    }
    if (line.toLowerCase().startsWith("bearer ")) line = line.slice(7).trim();
    if (line) return line;
  }
  return null;
}

async function probe(s = server) {
  try {
    const res = await fetch(`${s}/healthz`, { signal: AbortSignal.timeout(3000) });
    if (!res.ok) return null;
    const body = await res.json();
    return body?.status === "ok" ? body : null;
  } catch {
    return null;
  }
}

function backupThenWrite(file, content, log) {
  if (existsSync(file)) {
    const b = `${file}.bak-${Date.now()}`;
    copyFileSync(file, b);
    chmodSync(b, 0o600);
    log(`    已备份 → ${b.split("/").pop()}`);
  }
  writeFileSync(file, content, { mode: 0o600 });
  chmodSync(file, 0o600);
}

// ---------------------------------------------------------------- 计划/执行
function planDsh() {
  const hd = dshHooksDir();
  const src = { hooks: join(PKG_ROOT, "assets", "hooks"), tools: join(PKG_ROOT, "assets", "tools") };
  const files = [];
  for (const [kind, dir] of Object.entries(src)) {
    if (!existsSync(dir)) continue;
    for (const n of readdirSync(dir)) files.push({ kind, from: join(dir, n), to: join(hd, n), name: n });
  }
  return { hd, files, hooksJson: join(hd, "hooks.json") };
}

async function cmdSetup(log = console.log) {
  log("dsh-codex-bridge setup — 安装 DSH 侧");
  log("");

  log(`[1/4] 检查服务 ${server}`);
  if (!(await probe())) {
    log("  ✗ 服务没有响应。请先安装并启动 ai-memory：");
    log("      https://github.com/akitaonrails/ai-memory");
    log("      launchctl list | grep ai-memory");
    return 1;
  }
  log("  ✓ status=ok");

  log("[2/4] 读取 token");
  const token = readToken();
  if (!token) {
    log(`  ✗ 找不到 token（${join(dataDir(), "auth-header")}）`);
    log("    可设 AI_MEMORY_AUTH_TOKEN，或 ai-memory generate-auth-token");
    return 1;
  }
  log(`  ✓ 已读取（长度 ${token.length}，不显示）`);

  const { hd, files, hooksJson } = planDsh();
  log(`[3/4] 复制 ${files.length} 个文件 → ${hd}`);
  for (const f of files) log(`    ${f.name}`);

  log(`[4/4] 生成 hooks.json`);
  log(`    ${hooksJson}`);

  if (!yes) {
    log("");
    log("以上为计划。确认执行请加 --yes");
    return 0;
  }

  mkdirSync(hd, { recursive: true });
  for (const f of files) {
    copyFileSync(f.from, f.to);
    if (f.name.endsWith(".sh")) chmodSync(f.to, 0o755);
  }
  log(`  ✓ 已复制 ${files.length} 个文件`);

  const script = (n) => `sh '${join(hd, n)}'`;
  const withEnv = (n) => `AI_MEMORY_HOOK_URL=${server} AI_MEMORY_AUTH_TOKEN=${token} sh '${join(hd, n)}'`;
  const payload = {
    hooks: {
      SessionStart: [{ matcher: "", hooks: [{ type: "command", command: script("session-start.sh") }] }],
      UserPromptSubmit: [{ matcher: "", hooks: [{ type: "command", command: withEnv("user-prompt-submit.sh") }] }],
      PreToolUse: [{ matcher: "", hooks: [{ type: "command", command: script("pre-tool-use.sh") }] }],
      PostToolUse: [{ matcher: "", hooks: [{ type: "command", command: script("post-tool-use.sh") }] }],
      Stop: [{ matcher: "", hooks: [{ type: "command", command: script("stop.sh") }] }],
      SubagentStart: [{ matcher: "", hooks: [{ type: "command", command: script("subagent-start.sh") }] }],
      SubagentStop: [{ matcher: "", hooks: [{ type: "command", command: script("subagent-stop.sh") }] }],
    },
  };
  backupThenWrite(hooksJson, JSON.stringify(payload, null, 2), log);
  log("  ✓ hooks.json 已写入（7 事件全脚本模式）");

  log("");
  log("DSH 侧完成。还需要：");
  log("  1) 在 ~/.dsh/profiles/desktop/cordis.patch.yml 加：");
  log("       - insert:");
  log("           - id: codex-dsh-bridge");
  log("             name: 'dsh-codex-bridge'");
  log("  2) 重启 DSH");
  log("  3) Codex 侧：dsh-codex-bridge setup-codex");
  return 0;
}

async function cmdSetupCodex(log = console.log) {
  log("dsh-codex-bridge setup-codex — 安装 Codex 侧");
  log("");
  const hd = dshHooksDir();
  const f = codexHooksFile();

  log(`[1/3] 检查 DSH 侧脚本是否就位`);
  const needed = ["pre-tool-use.sh", "post-tool-use.sh", "stop.sh", "session-start.sh"];
  const missing = needed.filter((n) => !existsSync(join(hd, n)));
  if (missing.length) {
    log(`  ✗ 缺少 ${missing.join("、")}`);
    log("    请先运行：dsh-codex-bridge setup --yes");
    return 1;
  }
  log(`  ✓ ${needed.length} 个脚本存在`);

  log(`[2/3] 读取现有 ${f}`);
  let existing = { hooks: {} };
  if (existsSync(f)) {
    try {
      existing = JSON.parse(readFileSync(f, "utf8"));
    } catch {
      log("  ✗ 现有 hooks.json 不是合法 JSON，已中止（不覆盖）");
      return 1;
    }
  } else {
    log("  （不存在，将新建）");
  }

  const token = readToken();
  if (!token) {
    log("  ✗ 读不到 token，中止");
    return 1;
  }

  // Codex 用原生二进制会写 spool；这里统一用脚本直连。
  const script = (n) => `sh '${join(hd, n)}'`;
  const withEnv = (n) => `AI_MEMORY_HOOK_URL=${server} AI_MEMORY_AUTH_TOKEN=${token} sh '${join(hd, n)}'`;
  const events = {
    SessionStart: script("session-start.sh"),
    UserPromptSubmit: withEnv("user-prompt-submit.sh"),
    PreToolUse: script("pre-tool-use.sh"),
    PostToolUse: script("post-tool-use.sh"),
    Stop: script("stop.sh"),
  };

  log(`[3/3] 合并 5 个事件（保留原有其他事件）`);
  existing.hooks = existing.hooks || {};
  for (const [ev, command] of Object.entries(events)) {
    existing.hooks[ev] = [{ matcher: "", hooks: [{ type: "command", command }] }];
    log(`    ${ev}`);
  }

  if (!yes) {
    log("");
    log("以上为计划。确认执行请加 --yes");
    return 0;
  }

  mkdirSync(dirname(f), { recursive: true });
  backupThenWrite(f, JSON.stringify(existing, null, 2), log);
  log("  ✓ 已写入");
  log("");
  log("⚠ Codex 的钩子有信任哈希：改完必须在 Codex 里重新信任");
  log("   打开 Codex → /hooks → 逐条确认");
  log("   （哈希绑定 event+matcher+command，改了命令就会失效且静默跳过）");
  return 0;
}

async function cmdDoctor(log = console.log) {
  log("dsh-codex-bridge doctor — 只读检查");
  log("");
  const h = await probe();
  log(`服务 ${server}: ${h ? "✓ status=ok" : "✗ 无响应"}`);

  const hd = dshHooksDir();
  const want = ["session-start.sh", "user-prompt-submit.sh", "pre-tool-use.sh",
    "post-tool-use.sh", "stop.sh", "subagent-start.sh", "subagent-stop.sh"];
  const have = want.filter((n) => existsSync(join(hd, n)));
  log(`DSH hooks 脚本: ${have.length}/${want.length}${have.length === want.length ? " ✓" : " ✗"}`);

  const hj = join(hd, "hooks.json");
  if (existsSync(hj)) {
    try {
      const d = JSON.parse(readFileSync(hj, "utf8"));
      const n = Object.keys(d.hooks || {}).length;
      const allScript = Object.values(d.hooks || {}).every((g) =>
        g[0]?.hooks?.[0]?.command?.includes(".sh"));
      log(`DSH hooks.json: ${n} 事件，${allScript ? "全脚本模式 ✓" : "含原生二进制 ✗"}`);
    } catch {
      log("DSH hooks.json: ✗ 不是合法 JSON");
    }
  } else {
    log("DSH hooks.json: ✗ 不存在");
  }

  const cf = codexHooksFile();
  if (existsSync(cf)) {
    try {
      const d = JSON.parse(readFileSync(cf, "utf8"));
      const n = Object.keys(d.hooks || {}).length;
      log(`Codex hooks.json: ${n} 事件`);
    } catch {
      log("Codex hooks.json: ✗ 不是合法 JSON");
    }
  } else {
    log("Codex hooks.json: （不存在）");
  }

  const tok = readToken();
  log(`token: ${tok ? `✓ 可读（长度 ${tok.length}）` : "✗ 读不到"}`);
  return 0;
}

function cmdPaths(log = console.log) {
  log(`package:        ${PKG_ROOT}`);
  log(`data dir:       ${dataDir()}`);
  log(`DSH hooks:      ${dshHooksDir()}`);
  log(`Codex hooks:    ${codexHooksFile()}`);
  log(`server:         ${server}`);
  return 0;
}

function cmdHelp(log = console.log) {
  log("dsh-codex-bridge — Codex ↔ DSH 接力安装工具");
  log("");
  log("用法:");
  log("  dsh-codex-bridge setup           安装 DSH 侧（先看计划，加 --yes 执行）");
  log("  dsh-codex-bridge setup-codex     安装 Codex 侧");
  log("  dsh-codex-bridge doctor          只读检查两侧状态");
  log("  dsh-codex-bridge paths           打印相关路径");
  log("");
  log("选项:");
  log("  --yes, -y        真正执行（默认只打印计划）");
  log("  --server=<url>   ai-memory 地址（默认 http://127.0.0.1:49374）");
  return 0;
}

const table = {
  setup: cmdSetup,
  "setup-codex": cmdSetupCodex,
  doctor: cmdDoctor,
  paths: cmdPaths,
  help: cmdHelp,
  "--help": cmdHelp,
  "-h": cmdHelp,
};

const fn = table[cmd];
if (!fn) {
  console.error(`未知子命令: ${cmd}`);
  cmdHelp(console.error);
  process.exit(1);
}
Promise.resolve()
  .then(() => fn())
  .then((code) => { process.exitCode = code ?? 0; })
  .catch((e) => {
    console.error("失败：", e?.message || e);
    process.exitCode = 1;
  });
