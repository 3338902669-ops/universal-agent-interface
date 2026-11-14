// Paseo SDK runner:创建 agent → run → 等完成 → 取回复
// 用法: node paseo_runner.mjs '{"provider":"codex","cwd":"...","prompt":"...","timeout_s":300}'
import { createPaseoClient } from "file:///C:/Users/33389/AppData/Roaming/npm/node_modules/@getpaseo/cli/node_modules/@getpaseo/client/dist/index.js";

const cfg = JSON.parse(process.argv[2] || "{}");
const client = createPaseoClient({ url: "ws://127.0.0.1:6767/ws" });
await client.connect();

const handle = await client.agents.create({
  config: { provider: cfg.provider || "codex/model" },
  cwd: cfg.cwd,
  prompt: cfg.prompt,
  labels: { source: "uai-bridge" },
});

const result = await handle.waitForFinish({ timeoutMs: (cfg.timeout_s || 300) * 1000 });
// 取 timeline 里的最后 agent 回复
let reply = null;
try {
  const tl = await handle.timeline.refetch({ direction: "tail" });
  const items = Array.isArray(tl) ? tl : (tl.items || tl.entries || []);
  // timeline item 形状:{provider, item:{type,text,...}} —— 取最后一条 assistant_message
  const texts = items
    .map(i => i.item || i)
    .filter(i => (i.type === "assistant_message" || i.type === "agent_message") && i.text);
  reply = texts.length ? texts[texts.length-1].text : null;
} catch (e) {
  reply = "timeline_error: " + String(e).slice(0, 200);
}
console.log(JSON.stringify({ id: handle.id, status: handle.status, reply: String(reply).slice(0, 2000) }));
process.exit(0);
