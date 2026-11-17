/**
 * 色对比测量（WCAG 2.1）。
 * 用途：tokens 里的每一个「文字/背景」组合都必须有实测数字，不许凭感觉。
 *   node verify/contrast.mjs
 */
const rel = (hex) => {
  const h = hex.replace("#", "");
  const v = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  const lin = v.map((c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)));
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
};
const ratio = (a, b) => {
  const la = rel(a), lb = rel(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
};

const T = {
  bg1: "#020617", bg2: "#0E1223", bg3: "#1A1E2F",
  fg1: "#F8FAFC", fg2: "#94A3B8", fg3: "#7C8AA6",
  line: "#334155", line2: "#556784", accent: "#16A34A", green: "#3DDC84", amber: "#F5A524", red: "#F04438",
};

const pairs = [
  ["正文 fg1 / 面板 bg2", T.fg1, T.bg2, 4.5],
  ["次要 fg2 / 面板 bg2", T.fg2, T.bg2, 4.5],
  ["三级 fg3 / 面板 bg2", T.fg3, T.bg2, 4.5],
  ["正文 fg1 / 页面 bg1", T.fg1, T.bg1, 4.5],
  ["次要 fg2 / 页面 bg1", T.fg2, T.bg1, 4.5],
  ["运行绿 green / bg2", T.green, T.bg2, 4.5],
  ["警示琥珀 amber / bg2", T.amber, T.bg2, 4.5],
  ["故障红 red / bg2", T.red, T.bg2, 4.5],
  ["按钮字 #04140A / accent", "#04140A", T.accent, 4.5],
  ["装饰分隔线 line / bg2（非控件，仅记录）", T.line, T.bg2, 1.5],
  ["控件边框 line2 / bg2（非文字 3:1）", T.line2, T.bg2, 3.0],
  ["控件边框 line2 / bg1（非文字 3:1）", T.line2, T.bg1, 3.0],
];

let fail = 0;
console.log("配对".padEnd(34) + "比值".padEnd(10) + "门槛");
for (const [name, fg, bg, need] of pairs) {
  const r = ratio(fg, bg);
  const ok = r >= need;
  if (!ok) fail += 1;
  console.log(
    name.padEnd(32) +
    (r.toFixed(2) + ":1").padEnd(10) +
    need.toFixed(1) + (ok ? "  PASS" : "  FAIL"),
  );
}
console.log("\n结果：" + (pairs.length - fail) + "/" + pairs.length + " 达标" + (fail ? "（有 FAIL，必须调 tokens）" : ""));
process.exit(fail ? 1 : 0);
