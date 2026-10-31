/**
 * 插件离线测试：用假 ctx 与假扫描结果，不启动 DSH、不碰生产。
 *
 * 运行：node plugin/test-plugin.mjs
 */

import { apply, name, Config } from "./src/index.js";

const results = [];
const check = (label, ok, detail = "") =>
  results.push({ label, ok: Boolean(ok), detail });

/** 造一个最小 ctx，记录注册的监听器。 */
function makeCtx() {
  const listeners = new Map();
  return {
    on(event, fn) {
      listeners.set(event, fn);
    },
    listeners,
  };
}

/** 假 downstream（该轮会 enter）。 */
const enterDownstream = (msgs = []) => ({
  kind: "enter",
  messages: msgs,
});

async function runPreStep(handler, messages) {
  return handler(
    { agent: {}, messages, turn: 1, signal: undefined },
    async () => enterDownstream(messages),
  );
}

// 用一个临时脚本代替真实扫描器，输出可控 JSON
import { writeFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const fakeScript = join(tmpdir(), `fake-recovery-${Date.now()}.py`);
writeFileSync(
  fakeScript,
  `import json,sys,time
now=int(time.time()*1000000)
rows=[
 {"agent":"codex","native_id":"session-aaa","canonical_id":"x","cwd":"/tmp/a",
  "log_path":"/tmp/a/rollout.jsonl","last_log_activity_us":now-7200*1000000,
  "verdict":"stale_autosave","verdict_detail":"存档落后 2 小时"},
 {"agent":"dsh","native_id":"session-bbb","canonical_id":"y","cwd":"/tmp/b",
  "log_path":"/tmp/b/s.jsonl","last_log_activity_us":now-3600*1000000,
  "verdict":"active","verdict_detail":"仍在活动"},
]
print(json.dumps(rows))`,
);

// 清掉可能存在的限速状态
const statePath = join(tmpdir(), "dsh-ai-memory-recovery-last-scan.json");
try { unlinkSync(statePath); } catch {}

// ---- 1) 注册事件 ----
const ctx = makeCtx();
apply(ctx, { ...Config, scriptPath: fakeScript, throttleSeconds: 0 });
check("注册了 agent/pre-step", ctx.listeners.has("agent/pre-step"));

const handler = ctx.listeners.get("agent/pre-step");

// ---- 2) 只注入可操作项（active 不应出现）----
const out1 = await runPreStep(handler, [{ role: "user" }]);
const injected = (out1.messages || []).slice(1);
check("注入了 1 条提示", injected.length === 1, `实际 ${injected.length}`);

const text = injected.length
  ? injected[0].content.map((c) => c.text).join("")
  : "";
check("提示含中断会话 session-aaa", text.includes("session-aaa"));
check("提示不含活跃会话 session-bbb", !text.includes("session-bbb"));
check("提示含日志路径", text.includes("/tmp/a/rollout.jsonl"));
check("提示提醒不要只看 ended_at", text.includes("ended_at"));

// ---- 3) 同一批会话不重复提示 ----
const out2 = await runPreStep(handler, [{ role: "user" }]);
check(
  "同一会话不重复注入",
  (out2.messages || []).length === 1,
  `消息数 ${(out2.messages || []).length}`,
);

// ---- 4) 限速生效 ----
const ctx2 = makeCtx();
apply(ctx2, { ...Config, scriptPath: fakeScript, throttleSeconds: 600 });
const h2 = ctx2.listeners.get("agent/pre-step");
// 先跑一次让它写状态
await runPreStep(h2, [{ role: "user" }]);
const out3 = await runPreStep(h2, [{ role: "user" }]);
check(
  "限速窗口内不再扫",
  (out3.messages || []).length === 1,
  `消息数 ${(out3.messages || []).length}`,
);

// ---- 5) 脚本不存在时静默 ----
const ctx3 = makeCtx();
apply(ctx3, { ...Config, scriptPath: "/nonexistent/x.py", throttleSeconds: 0 });
const out4 = await runPreStep(ctx3.listeners.get("agent/pre-step"), [{ role: "user" }]);
check("脚本缺失时静默不报错", (out4.messages || []).length === 1);

// ---- 6) 开关 ----
const ctx4 = makeCtx();
apply(ctx4, { ...Config, enabled: false, scriptPath: fakeScript });
check("enabled=false 时不注册", !ctx4.listeners.has("agent/pre-step"));

// ---- 7) downstream 不 enter 时不注入 ----
const ctx5 = makeCtx();
apply(ctx5, { ...Config, scriptPath: fakeScript, throttleSeconds: 0 });
const h5 = ctx5.listeners.get("agent/pre-step");
const out5 = await h5(
  { agent: {}, messages: [{ role: "user" }], turn: 1 },
  async () => ({ kind: "reject" }),
);
check("downstream 非 enter 时不注入", out5.kind === "reject" && !out5.messages);

try { unlinkSync(fakeScript); } catch {}
try { unlinkSync(statePath); } catch {}

const passed = results.filter((r) => r.ok).length;
for (const r of results) {
  if (!r.ok) console.log(`FAIL ${r.label}  [${r.detail}]`);
}
console.log(`\n${passed}/${results.length} 通过`);
process.exit(passed === results.length ? 0 : 1);
