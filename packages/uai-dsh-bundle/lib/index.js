/**
 * UAI × DSH 宿主侧插件（P2 第一步）。
 *
 * 职责边界（刻意做窄）：
 *   1. 在 127.0.0.1 上开一个只读为主的 HTTP 小服务，把 uai_core 的状态转成 JSON；
 *   2. 客户端页面只做渲染与按钮，不直接读文件、不直接起进程。
 *
 * 为什么不用 typert/remote：那套要跟着 DSH 的版本走，第三方包会被版本绑死；
 * HTTP + JSON 是稳定契约，跨 DSH 版本都能活，也方便别的宿主复用。
 */

import http from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export const name = "uai-collab";

const DEFAULT_PORT = 47311;
const CMD_TIMEOUT_MS = 20000;

function uaiCommand() {
  // UAI_CMD 可覆盖（例如 "C:/path/python.exe -m uai_core"），默认用 PATH 上的 uai
  const raw = process.env.UAI_CMD || "uai";
  const parts = raw.split(/\s+/).filter(Boolean);
  return { file: parts[0], args: parts.slice(1) };
}

async function runUai(subArgs) {
  const { file, args } = uaiCommand();
  try {
    const { stdout } = await execFileAsync(file, [...args, ...subArgs], {
      timeout: CMD_TIMEOUT_MS,
      maxBuffer: 8 * 1024 * 1024,
      windowsHide: true,
    });
    return { ok: true, data: JSON.parse(stdout) };
  } catch (error) {
    const detail = (error && (error.stderr || error.message)) || String(error);
    return { ok: false, error: String(detail).slice(0, 800) };
  }
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "cache-control": "no-store",
  });
  res.end(payload);
}

export async function apply(ctx, config = {}) {
  const port = Number(config.port || process.env.UAI_BRIDGE_PORT || DEFAULT_PORT);
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (req.method === "OPTIONS") {
      sendJson(res, 204, {});
      return;
    }
    if (url.pathname === "/uai/ping") {
      sendJson(res, 200, { ok: true, service: "uai-collab", pid: process.pid });
      return;
    }
    if (url.pathname === "/uai/status") {
      const result = await runUai(["status"]);
      sendJson(res, result.ok ? 200 : 502, result);
      return;
    }
    if (url.pathname === "/uai/detect") {
      const result = await runUai(["detect", "--json"]);
      sendJson(res, result.ok ? 200 : 502, result);
      return;
    }
    if (url.pathname === "/uai/seats") {
      const result = await runUai(["seats"]);
      sendJson(res, result.ok ? 200 : 502, result);
      return;
    }
    sendJson(res, 404, { ok: false, error: "unknown_path", path: url.pathname });
  });

  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
  ctx?.logger?.info?.(`[uai-collab] bridge listening on 127.0.0.1:${port}`);

  ctx?.on?.("dispose", () => {
    try {
      server.close();
    } catch {
      /* 忽略关闭异常 */
    }
  });

  return { port, server };
}

export default apply;
