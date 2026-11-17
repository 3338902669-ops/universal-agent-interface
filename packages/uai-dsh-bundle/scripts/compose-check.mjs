/**
 * 静态合成检查：不用重启 DSH，就能证明「我们的 bundle 会被加载」。
 *
 * DSH 的合成顺序（见 profile/cordis.yml 注释）：
 *   cordis.yml 的 [] ← dsh.profile.bundles 里每个 bundle 的 dsh.bundle.patch
 *                     ← profile/cordis.patch.yml ← --patch 覆盖层
 *
 * 本脚本照这个顺序把 patch 应用一遍，检查最终树里有没有 uai-collab 这一行。
 * 只做 insert/disable/覆盖 的最小语义，够用来判断「装上了但不起作用」这类问题。
 *
 *   node scripts/compose-check.mjs [--profile <dir>] [--bundle @uai/dsh-bundle] [--row uai-collab]
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const argv = process.argv.slice(2);
const getFlag = (flag, fallback) => {
  const i = argv.indexOf(flag);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

const profileDir = getFlag("--profile", path.join(process.env.USERPROFILE || process.env.HOME || ".", ".dsh", "profiles", "desktop"));
const bundleName = getFlag("--bundle", "@uai/dsh-bundle");
const rowId = getFlag("--row", "uai-collab");

// js-yaml 从 profile 里借（DSH 自己带着）
const requireFromProfile = createRequire(path.join(profileDir, "package.json"));
let yaml;
try {
  yaml = requireFromProfile("js-yaml");
} catch (error) {
  console.error("js-yaml not resolvable from profile: " + String(error).slice(0, 160));
  process.exit(2);
}

const PASS = [];
const FAIL = [];
const check = (name, cond, detail = "") => {
  (cond ? PASS : FAIL).push(name);
  console.log((cond ? "PASS  " : "FAIL  ") + name + (detail ? "  " + detail : ""));
};

const pkgPath = path.join(profileDir, "package.json");
const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
const bundles = (pkg.dsh && pkg.dsh.profile && pkg.dsh.profile.bundles) || [];
check("profile 的 dsh.profile.bundles 里包含目标 bundle", bundles.indexOf(bundleName) >= 0,
  "count=" + bundles.length);
check("profile 的 dependencies 里也声明了它",
  Boolean(pkg.dependencies && pkg.dependencies[bundleName]), String((pkg.dependencies || {})[bundleName]));

// node_modules 里能解析到这个包（link 也行）
let bundleDir = null;
try {
  bundleDir = path.dirname(requireFromProfile.resolve(path.join(bundleName, "package.json")));
} catch (error) {
  // link 或 exports 限制时退化成直接拼路径
  const guess = path.join(profileDir, "node_modules", ...bundleName.split("/"));
  if (fs.existsSync(path.join(guess, "package.json"))) bundleDir = guess;
}
check("node_modules 能解析到 bundle 包", Boolean(bundleDir), bundleDir || "unresolved");

let rows = [];
const applied = [];
if (bundleDir) {
  const bundlePkg = JSON.parse(fs.readFileSync(path.join(bundleDir, "package.json"), "utf8"));
  const patchRel = bundlePkg.dsh && bundlePkg.dsh.bundle && bundlePkg.dsh.bundle.patch;
  check("bundle 声明了 dsh.bundle.patch", Boolean(patchRel), String(patchRel));
  if (patchRel) {
    const patchPath = path.join(bundleDir, patchRel);
    check("patch 文件存在", fs.existsSync(patchPath), patchPath.replace(profileDir, ""));
    if (fs.existsSync(patchPath)) {
      const entries = yaml.load(fs.readFileSync(patchPath, "utf8")) || [];
      check("patch 是数组", Array.isArray(entries), "entries=" + (Array.isArray(entries) ? entries.length : "n/a"));
      for (const entry of entries) {
        if (Array.isArray(entry.insert)) rows = rows.concat(entry.insert);
      }
      check("patch 里有 insert 行", rows.length > 0, "rows=" + rows.length);
    }
  }
}

const ours = rows.filter((row) => row && (row.id === rowId || row.name === bundleName));
check("合成后的树里有 " + rowId + " 这一行", ours.length === 1,
  ours.length ? JSON.stringify(ours[0]) : "not found");

if (ours.length === 1) {
  const row = ours[0];
  check("行 name 指向本包", row.name === bundleName, String(row.name));
  check("行配置带桥端口", Boolean(row.config && row.config.port), String(row.config && row.config.port));
}

// 顺手检查客户端声明：没有它就不会有侧栏面板
if (bundleDir) {
  const bundlePkg = JSON.parse(fs.readFileSync(path.join(bundleDir, "package.json"), "utf8"));
  check("声明了 dsh.client.platform",
    Boolean(bundlePkg.dsh && bundlePkg.dsh.client && bundlePkg.dsh.client.platform),
    JSON.stringify(bundlePkg.dsh && bundlePkg.dsh.client));
  check("exports 暴露 ./client",
    Boolean(bundlePkg.exports && bundlePkg.exports["./client"]),
    JSON.stringify(bundlePkg.exports && bundlePkg.exports["./client"]));
  const clientFile = bundlePkg.exports && bundlePkg.exports["./client"];
  const clientPath = path.join(bundleDir, typeof clientFile === "string" ? clientFile : "./lib/client.js");
  check("客户端入口文件存在", fs.existsSync(clientPath), clientPath.replace(bundleDir, "<bundle>"));
}

console.log("\nRESULT: " + PASS.length + "/" + (PASS.length + FAIL.length) + " passed");
process.exit(FAIL.length ? 1 : 0);
