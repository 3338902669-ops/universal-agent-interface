/**
 * UAI × DSH 宿主侧插件（P2/P3）。
 *
 * 两块职责：
 *   1. HTTP 桥：127.0.0.1:47311 上把 uai_core 的状态转成 JSON，供客户端面板读取；
 *   2. 工具 uai_status：让模型能直接问"常驻面现在是什么状态"，
 *      思考链里因此会留下一条 UAI 的调用记录（客户端再把它渲染成卡片）。
 *
 * 为什么不用 typert/remote：那套要跟着 DSH 版本走，第三方包会被版本绑死。
 * HTTP + JSON 是稳定契约。
 */
import http from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export const name = "uai-collab";
export const inject = [];

const DEFAULT_PORT = 47311;
const CMD_TIMEOUT_MS = 20000;

function uaiCommand() {
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
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "cache-control": "no-store",
  });
  res.end(JSON.stringify(body));
}

/** 工具与桥共用的状态摘要。 */
export async function uaiStatusSummary() {
  const res = await runUai(["status"]);
  if (!res.ok) return { ok: false, error: res.error };
  const d = res.data || {};
  return {
    ok: true,
    hub: { held: Boolean(d.hub && d.hub.held), pid: (d.hub && d.hub.holder && d.hub.holder.pid) || null },
    seats: d.seats || {},
    processes: d.processes || {},
  };
}

export async function registerTool(ctx, defineToolOverride) {
  let defineTool = defineToolOverride;
  if (defineTool === undefined) {
    try {
      ({ defineTool } = await import("@deepseek-ai/dsh-tools"));
    } catch (error) {
      ctx?.logger?.info?.("[uai-collab] dsh-tools unavailable, tool not registered: " + String(error).slice(0, 120));
      return false;
    }
  }
  const tool = defineTool({
    name: "uai_status",
    description: "Read the UAI collaboration plane status: hub singleton, registered seats, process counts.",
    parameters: {},
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          ok: { type: "boolean", required: true, description: "Whether the status was read" },
          summary: { type: "string", required: true, description: "One-line human summary" },
          hub_held: { type: "boolean", description: "Hub singleton is held by a live process" },
          seats: { type: "number", description: "Configured seats" },
          seats_running: { type: "number", description: "Seats currently holding their lock" },
          supervisor: { type: "number", description: "Supervisor processes observed" },
          error: { type: "string", description: "Error detail when ok=false" },
        },
      },
      render: (_args, value) => [{ type: "text", text: value.summary }],
    },
    async execute() {
      const s = await uaiStatusSummary();
      if (!s.ok) {
        return { ok: false, summary: "UAI 状态读取失败：" + s.error, error: s.error };
      }
      const seatNames = Object.keys(s.seats || {});
      const running = seatNames.filter((k) => s.seats[k] && s.seats[k].held).length;
      const supervisor = s.processes ? s.processes.supervisor || 0 : 0;
      return {
        ok: true,
        summary: "中枢 " + (s.hub.held ? "运行中" : "未运行") +
          " · 席位 " + running + "/" + seatNames.length +
          " · 监督 " + supervisor,
        hub_held: s.hub.held,
        seats: seatNames.length,
        seats_running: running,
        supervisor,
      };
    },
  });
  ctx.tools.register(tool);
  return true;
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
  ctx?.logger?.info?.("[uai-collab] bridge listening on 127.0.0.1:" + port);

  let toolRegistered = false;
  try {
    toolRegistered = await registerTool(ctx);
  } catch (error) {
    ctx?.logger?.info?.("[uai-collab] tool registration failed: " + String(error).slice(0, 160));
  }

  ctx?.on?.("dispose", () => {
    try {
      server.close();
    } catch {
      /* 忽略关闭异常 */
    }
  });

  return { port, server, toolRegistered };
}

export default apply;
