/** 宿主侧自检：不装进 DSH 也能验证桥接服务是否工作。
 *
 *   node test/host-selftest.mjs [--port 47312] [--skip-cli]
 *
 * 断言：/uai/ping 通、/uai/status 返回 JSON（或明确报 CLI 不可用而不是崩）、
 *      未知路径 404、CORS 头存在。
 */
import http from "node:http";
import { apply } from "../lib/index.js";

const argv = process.argv.slice(2);
const portArg = argv.indexOf("--port");
const port = portArg >= 0 ? Number(argv[portArg + 1]) : 47312;

const PASS = [];
const FAIL = [];
function check(name, cond, detail = "") {
  (cond ? PASS : FAIL).push(name);
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
}

function get(path) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, path, method: "GET" }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on("error", reject);
    req.end();
  });
}

const handle = await apply({ logger: { info() {} } }, { port });
try {
  const ping = await get("/uai/ping");
  const pingBody = JSON.parse(ping.body || "{}");
  check("桥接服务启动并响应 /uai/ping", ping.status === 200 && pingBody.ok === true,
    "status=" + ping.status);
  check("CORS 头存在（页面可跨源读取）",
    (ping.headers["access-control-allow-origin"] || "") === "*",
    String(ping.headers["access-control-allow-origin"]));

  const status = await get("/uai/status");
  let parsed = null;
  try { parsed = JSON.parse(status.body); } catch { /* 保留 null */ }
  check("/uai/status 返回合法 JSON", parsed !== null, "status=" + status.status);
  check("/uai/status 在 CLI 不可用时明确报错而不是崩溃",
    parsed && (parsed.ok === true || typeof parsed.error === "string"),
    parsed ? (parsed.ok ? "ok=true" : String(parsed.error).slice(0, 60)) : "no-json");

  const missing = await get("/uai/nope");
  check("未知路径返回 404", missing.status === 404, "status=" + missing.status);
} finally {
  handle.server.close();
}

console.log(`\nRESULT: ${PASS.length}/${PASS.length + FAIL.length} passed`);
process.exit(FAIL.length ? 1 : 0);
