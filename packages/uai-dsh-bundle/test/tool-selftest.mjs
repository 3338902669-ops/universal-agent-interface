/**
 * 工具逻辑自检：不需要 DSH、不需要 dsh-tools —— 用 pass-through 的 defineTool 拿到工具对象，
 * 直接跑 execute()，验证返回形状与摘要内容。
 *
 *   UAI_CMD="<python> -m uai_core" UAI_BASE="<base>" node test/tool-selftest.mjs
 */
import { registerTool } from "../lib/index.js";

const PASS = [];
const FAIL = [];
function check(name, cond, detail = "") {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? "PASS  " : "FAIL  ") + name + (detail ? "  " + detail : ""));
}

let registered = null;
const ctx = { tools: { register: (t) => { registered = t; } }, logger: { info() {} } };
const passthrough = (options) => options;

const ok = await registerTool(ctx, passthrough);
check("注入 defineTool 后注册成功", ok === true);
check("工具名是 wire 名 uai_status", registered !== null && registered.name === "uai_status",
  registered ? registered.name : "none");
check("参数表为空对象（无入参）",
  registered !== null && typeof registered.parameters === "object" &&
  Object.keys(registered.parameters).length === 0);
check("output.schema 声明了 ok/summary",
  registered !== null && registered.output && registered.output.schema &&
  Boolean(registered.output.schema.properties.ok) && Boolean(registered.output.schema.properties.summary));

if (registered) {
  const out = await registered.execute({});
  check("execute 返回对象", out !== null && typeof out === "object");
  check("返回 ok 字段是布尔", typeof out.ok === "boolean", "ok=" + out.ok);
  check("返回一行摘要", typeof out.summary === "string" && out.summary.length > 0,
    String(out.summary).slice(0, 90));
  if (out.ok) {
    check("摘要含 中枢/席位/监督 三项",
      out.summary.indexOf("中枢") >= 0 && out.summary.indexOf("席位") >= 0 && out.summary.indexOf("监督") >= 0,
      String(out.summary).slice(0, 90));
    check("结构性字段齐全（hub_held/seats/seats_running/supervisor）",
      typeof out.hub_held === "boolean" && typeof out.seats === "number" &&
      typeof out.seats_running === "number" && typeof out.supervisor === "number",
      JSON.stringify({ hub_held: out.hub_held, seats: out.seats, seats_running: out.seats_running, supervisor: out.supervisor }));
    check("render() 能产出文本块", Array.isArray(registered.output.render({}, out)) &&
      registered.output.render({}, out)[0].type === "text");
  }
}

console.log("\nRESULT: " + PASS.length + "/" + (PASS.length + FAIL.length) + " passed");
process.exit(FAIL.length ? 1 : 0);
