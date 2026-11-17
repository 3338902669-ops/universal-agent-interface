/**
 * 幂等地维护 DSH profile 的 bundle 列表。
 *
 *   node scripts/patch-profile.mjs add|remove|check [--profile <dir>] [--name @uai/dsh-bundle]
 *
 * 只改 dsh.profile.bundles 一个数组；改之前写 .bak（带时间戳）。
 */
import fs from "node:fs";
import path from "node:path";

const argv = process.argv.slice(2);
const action = argv[0] || "check";
const getFlag = (flag, fallback) => {
  const i = argv.indexOf(flag);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

const profileDir = getFlag("--profile", path.join(process.env.USERPROFILE || process.env.HOME || ".", ".dsh", "profiles", "desktop"));
const name = getFlag("--name", "@uai/dsh-bundle");
const pkgPath = path.join(profileDir, "package.json");

if (!fs.existsSync(pkgPath)) {
  console.error("profile package.json not found: " + pkgPath);
  process.exit(2);
}

const raw = fs.readFileSync(pkgPath, "utf8");
const pkg = JSON.parse(raw);
pkg.dsh = pkg.dsh || {};
pkg.dsh.profile = pkg.dsh.profile || {};
const bundles = Array.isArray(pkg.dsh.profile.bundles) ? pkg.dsh.profile.bundles : [];
const present = bundles.indexOf(name) >= 0;

if (action === "check") {
  console.log(JSON.stringify({ profile: pkgPath, name, present, count: bundles.length, bundles }, null, 2));
  process.exit(0);
}

let next = bundles.slice();
if (action === "add") {
  if (present) {
    console.log("already present: " + name);
    process.exit(0);
  }
  next.push(name);
} else if (action === "remove") {
  if (!present) {
    console.log("not present: " + name);
    process.exit(0);
  }
  next = next.filter((x) => x !== name);
} else {
  console.error("unknown action: " + action);
  process.exit(2);
}

const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const backup = pkgPath + ".bak-" + stamp;
fs.writeFileSync(backup, raw, "utf8");
pkg.dsh.profile.bundles = next;
fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ action, name, backup, bundles: next }, null, 2));
