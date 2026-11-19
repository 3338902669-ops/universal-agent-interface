/**
 * 把 docs/design/ui-20261118-panel/panel.css 编译给 bundle 用。
 *
 * 两个动作：
 *   1. 写 lib/panel-css.js（可被别的入口 import）
 *   2. 把同一份 CSS 内联进 lib/client.js 的 @uai-css:start … @uai-css:end 标记区段
 *      —— 客户端 bundle 在宿主里只能用 loader 提供的 require（react 等），
 *         不能 require 相对文件，所以样式必须构建期内联。
 *
 *   node scripts/build-css.mjs [--check]
 *
 * --check：只比对，不一致退出 1（提交前 / CI 用）。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(HERE, "..", "..", "..");
const cssPath = path.join(root, "docs", "design", "ui-20261118-panel", "panel.css");
const modulePath = path.join(HERE, "..", "lib", "panel-css.js");
const clientPath = path.join(HERE, "..", "lib", "client.js");

if (!fs.existsSync(cssPath)) {
  console.error("panel.css not found: " + cssPath);
  process.exit(2);
}

const css = fs.readFileSync(cssPath, "utf8");
const banner = "// 自动生成，勿手改。源：docs/design/ui-20261118-panel/panel.css\n// 重新生成：node packages/uai-dsh-bundle/scripts/build-css.mjs\n";
const moduleBody = banner + "export const PANEL_CSS = " + JSON.stringify(css) + ";\nexport default PANEL_CSS;\n";

const client = fs.readFileSync(clientPath, "utf8");
const START = "/* @uai-css:start";
const END = "/* @uai-css:end */";
const si = client.indexOf(START);
const ei = client.indexOf(END);
if (si < 0 || ei < 0) {
  console.error("client.js 缺少 @uai-css 标记区段");
  process.exit(2);
}
const head = client.slice(0, si);
const tail = client.slice(ei + END.length);
const inlined = [
  "/* @uai-css:start —— 由 scripts/build-css.mjs 从 docs/design/ui-20261118-panel/panel.css 注入，勿手改 */",
  "const PANEL_CSS = " + JSON.stringify(css) + ";",
  "/* @uai-css:end */",
].join("\n");
const nextClient = head + inlined + tail;

const check = process.argv.includes("--check");
const moduleChanged = !fs.existsSync(modulePath) || fs.readFileSync(modulePath, "utf8") !== moduleBody;
const clientChanged = nextClient !== client;

if (check) {
  if (moduleChanged || clientChanged) {
    console.error("生成物与 panel.css 不同步（module=" + moduleChanged + " client=" + clientChanged + "），请运行 node scripts/build-css.mjs");
    process.exit(1);
  }
  console.log("同步：" + css.length + " bytes css 已内联");
  process.exit(0);
}

fs.writeFileSync(modulePath, moduleBody, "utf8");
fs.writeFileSync(clientPath, nextClient, "utf8");
console.log("wrote panel-css.js + 内联 client.js（" + css.length + " bytes css）");
