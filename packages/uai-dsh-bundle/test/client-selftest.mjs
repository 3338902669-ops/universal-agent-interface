/**
 * 客户端插件自检：不需要 DSH、不需要真实 React。
 *
 * 做法：造一个 window.__ModuleLoader__ 的 mock，用一个极小的 React 垫片
 * （createElement / useState / useEffect / useCallback / Fragment）把 bundle 跑起来，
 * 然后断言：
 *   1. 模块注册到 loader，且导出 apply；
 *   2. apply() 注册了 3 个 slot：sidebar.panellist、main、tool.call.toolview（两个 key）；
 *   3. 面板与两张卡片都能渲染出文本，不抛异常。
 *
 *   node test/client-selftest.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLIENT = path.join(HERE, "..", "lib", "client.js");

const PASS = [];
const FAIL = [];
function check(name, cond, detail = "") {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? "PASS  " : "FAIL  ") + name + (detail ? "  " + detail : ""));
}

// ---- React 垫片：单次渲染语义 ----
function createElement(type, props, ...children) {
  return { type, props: Object.assign({}, props, children.length ? { children } : {}) };
}
const reactShim = {
  createElement,
  Fragment: "Fragment",
  useState: (initial) => [typeof initial === "function" ? initial() : initial, () => {}],
  useEffect: () => {},
  useMemo: (fn) => fn(),
  useCallback: (fn) => fn,
  useRef: (initial) => ({ current: initial }),
};

// ---- document mock（验 apply 里的样式注入） ----
const styles = [];
globalThis.document = {
  head: { appendChild: (el) => styles.push(el) },
  getElementById: (id) => styles.find((el) => el.id === id) || null,
  createElement: (tag) => ({ tag, id: "", textContent: "" }),
};

// ---- loader mock ----
let loaded = null;
globalThis.window = {
  __ModuleLoader__: {
    load: (entry) => {
      loaded = entry;
      loaded.exports = entry.factory((specifier) => {
        if (specifier === "react" || specifier === "react/jsx-runtime") return reactShim;
        throw new Error("unexpected require: " + specifier);
      });
    },
  },
};

const source = fs.readFileSync(CLIENT, "utf8");
new Function("window", source)(globalThis.window);

check("client.js 注册进 __ModuleLoader__", loaded !== null && loaded.id === "@uai/dsh-bundle",
  loaded ? loaded.id : "none");
check("模块导出 apply()", loaded && typeof loaded.exports.apply === "function");

// ---- 收集 slot 注册 ----
const registrations = [];
const ctx = {
  slots: {
    inject: (name, factory) => factory(),
    register: (options, component) => {
      registrations.push({ options, component });
      return () => {};
    },
  },
};
loaded.exports.apply(ctx);
check("注册了 3 类 slot / 4 个注册", registrations.length === 4, "count=" + registrations.length);

const names = registrations.map((r) => r.options.name);
check("sidebar.panellist 入口存在", names.indexOf("sidebar.panellist") >= 0);
check("main 面板存在", names.indexOf("main") >= 0);
const viewKeys = registrations.filter((r) => r.options.name === "tool.call.toolview").map((r) => r.options.key);
check("tool.call.toolview 覆盖 uai_status 与 uai_detect",
  viewKeys.indexOf("uai_status") >= 0 && viewKeys.indexOf("uai_detect") >= 0, viewKeys.join(","));

check("apply 注入了面板样式（<style id=uai-panel-style>）",
  styles.length === 1 && styles[0].id === "uai-panel-style", "styles=" + styles.length);
check("注入的 CSS 含设计令牌（--uai-bg2 / --uai-dur）",
  styles.length === 1 && styles[0].textContent.includes("--uai-bg2") &&
  styles[0].textContent.includes("--uai-dur"),
  styles.length ? String(styles[0].textContent.length) + " chars" : "none");
check("注入的 CSS 含 reduce 降级与 390 断点",
  styles.length === 1 && styles[0].textContent.includes("prefers-reduced-motion") &&
  styles[0].textContent.includes("max-width: 560px"));
check("再次 apply 不重复注入（幂等）", (() => { loaded.exports.apply(ctx); return styles.length === 1; })());

// ---- 渲染（收集文本） ----
function textOf(node, out = []) {
  if (node === null || node === undefined || node === false || node === true) return out;
  if (typeof node === "string" || typeof node === "number") {
    out.push(String(node));
    return out;
  }
  if (Array.isArray(node)) {
    for (const child of node) textOf(child, out);
    return out;
  }
  const props = node.props || {};
  if (props.children) textOf(props.children, out);
  return out;
}

function render(name, props) {
  const entry = registrations.find((r) => r.options.name === name);
  const tree = entry.component(props || {});
  return textOf(tree).join(" ");
}

try {
  const panelText = render("main");
  check("面板能渲染且含标题「UAI 协作」", panelText.indexOf("UAI 协作") >= 0, panelText.slice(0, 60));
  check("面板含三个指标区（中枢/进程/监督）",
    panelText.indexOf("中枢") >= 0 && panelText.indexOf("进程") >= 0 && panelText.indexOf("监督") >= 0);
} catch (error) {
  check("面板能渲染且含标题「UAI 协作」", false, String(error).slice(0, 120));
}

try {
  const statusEntry = registrations.find((r) => r.options.key === "uai_status");
  const cardText = textOf(statusEntry.component({
    phase: "complete",
    block: { result: { value: { ok: true, hub_held: true, seats: 4, seats_running: 4, supervisor: 1 } } },
  })).join(" ");
  check("状态卡片渲染出 中枢/席位/监督 三格",
    cardText.indexOf("中枢") >= 0 && cardText.indexOf("席位") >= 0 && cardText.indexOf("监督") >= 0,
    cardText.slice(0, 80));
  check("状态卡片显示 4/4 与监督 1",
    cardText.indexOf("4/4") >= 0 && cardText.indexOf("监督 1") >= 0, cardText.slice(0, 80));
} catch (error) {
  check("状态卡片渲染出 中枢/席位/监督 三格", false, String(error).slice(0, 120));
}

try {
  const detectEntry = registrations.find((r) => r.options.key === "uai_detect");
  const cardText = textOf(detectEntry.component({
    phase: "complete",
    block: { result: { value: { ok: true, summary: "就绪 5/12（dsh, codex）", ready_ids: "dsh,codex" } } },
  })).join(" ");
  check("检测卡片渲染出摘要与就绪标签",
    cardText.indexOf("就绪 5/12") >= 0 && cardText.indexOf("dsh") >= 0, cardText.slice(0, 80));
} catch (error) {
  check("检测卡片渲染出摘要与就绪标签", false, String(error).slice(0, 120));
}

console.log("\nRESULT: " + PASS.length + "/" + (PASS.length + FAIL.length) + " passed");
process.exit(FAIL.length ? 1 : 0);
