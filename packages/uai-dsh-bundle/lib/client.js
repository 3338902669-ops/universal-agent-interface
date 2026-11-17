/**
 * UAI × DSH 客户端插件：侧栏「UAI 协作」页。
 *
 * 文件格式与 DSH 自带客户端插件一致：注册进 window.__ModuleLoader__，
 * 依赖（react）由宿主提供（require），不打包 React。
 *
 * 视觉令牌来自 docs/design/ui-20261117/TOKENS.md（RPA/Automation Dashboard 配方）：
 * #020617 / #0E1223 / #1A1E2F + 状态色 #22C55E / #F59E0B / #DC2626，radius 0，36px 行高。
 */
window.__ModuleLoader__.load({
  id: "@uai/dsh-bundle",
  factory: (require) => {
    const React = require("react");
    const h = React.createElement;

    const PANEL_ID = "uai-collab";
    const BRIDGE = "http://127.0.0.1:47311";

    const T = {
      bg1: "#020617", bg2: "#0E1223", bg3: "#1A1E2F",
      fg1: "#F8FAFC", fg2: "#94A3B8", fg3: "#5C6B85",
      line: "#334155", accent: "#16A34A",
      green: "#22C55E", amber: "#F59E0B", red: "#DC2626",
      micro: "11px", small: "12px", body: "14px", lead: "16px", title: "20px",
      row: "36px", pad: "12px", gap: "8px",
      mono: "ui-monospace, SFMono-Regular, Consolas, monospace",
      sans: "system-ui, -apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif",
    };

    async function getJson(path) {
      const res = await fetch(BRIDGE + path, { cache: "no-store" });
      return res.json();
    }

    function StatCell(label, value, color) {
      return h("div", { style: { padding: "8px 12px", borderRight: "1px solid " + T.line } },
        h("div", { style: { fontSize: T.micro, letterSpacing: ".14em", textTransform: "uppercase", color: T.fg2 } }, label),
        h("div", { style: { fontSize: T.lead, color: color || T.fg1, fontVariantNumeric: "tabular-nums" } }, String(value)));
    }

    function SeatRow(key, seat) {
      const held = Boolean(seat && seat.held);
      const pid = seat && seat.pid;
      return h("div", {
        key,
        style: {
          display: "grid", gridTemplateColumns: "1fr 72px 1fr", alignItems: "center",
          height: T.row, padding: "0 " + T.pad, borderTop: "1px solid " + T.line,
          fontSize: T.body, color: T.fg1, gap: T.gap,
        },
      },
        h("span", { style: { fontWeight: 600 } }, key),
        h("span", { style: { display: "inline-flex", alignItems: "center", gap: "5px", fontSize: T.small, color: T.fg2 } },
          h("i", { style: { width: "6px", height: "6px", borderRadius: "50%", background: held ? T.green : T.fg3 } }),
          held ? "运行中" : "未运行"),
        h("span", { style: { fontFamily: T.mono, fontSize: T.small, color: T.fg3 } },
          pid ? "pid " + pid : (seat && seat.why) || "-"));
    }

    function Panel() {
      const [state, setState] = React.useState({ loading: true, error: null, status: null, detect: null });

      const refresh = React.useCallback(async () => {
        setState((s) => Object.assign({}, s, { loading: true, error: null }));
        try {
          const payload = await getJson("/uai/status");
          if (payload && payload.ok === false) {
            setState({ loading: false, error: payload.error || "uai CLI unavailable", status: null, detect: null });
            return;
          }
          const data = payload && payload.data ? payload.data : payload;
          setState({ loading: false, error: null, status: data, detect: null });
        } catch (error) {
          setState({ loading: false, error: String(error).slice(0, 200), status: null, detect: null });
        }
      }, []);

      const runDetect = React.useCallback(async () => {
        setState((s) => Object.assign({}, s, { loading: true }));
        try {
          const payload = await getJson("/uai/detect");
          const data = payload && payload.data ? payload.data : payload;
          setState((s) => Object.assign({}, s, { loading: false, detect: data }));
        } catch (error) {
          setState((s) => Object.assign({}, s, { loading: false, error: String(error).slice(0, 200) }));
        }
      }, []);

      React.useEffect(() => { refresh(); }, [refresh]);

      const status = state.status || {};
      const hub = status.hub || {};
      const seats = status.seats || {};
      const processes = status.processes || {};
      const seatNames = Object.keys(seats);
      const detect = state.detect || {};

      const btn = (label, onClick, primary) =>
        h("button", {
          onClick,
          disabled: state.loading,
          style: {
            height: "30px", padding: "0 12px", borderRadius: "4px",
            cursor: state.loading ? "default" : "pointer",
            border: "1px solid " + (primary ? T.accent : T.line),
            background: primary ? T.accent : "transparent",
            color: primary ? "#04140A" : T.fg1,
            fontWeight: primary ? 600 : 500, fontSize: T.body, fontFamily: T.sans,
          },
        }, label);

      return h("div", { style: { background: T.bg1, color: T.fg1, fontFamily: T.sans, minHeight: "100%", fontSize: T.body } },
        h("div", {
          style: {
            display: "flex", alignItems: "center", gap: T.gap, height: "52px", padding: "0 " + T.pad,
            background: T.bg2, borderBottom: "1px solid " + T.line,
          },
        },
          h("span", { style: { fontSize: T.title, fontWeight: 600 } }, "UAI 协作"),
          h("span", {
            style: { fontSize: T.micro, letterSpacing: ".14em", textTransform: "uppercase", color: T.fg2, border: "1px solid " + T.line, padding: "2px 6px" },
          }, seatNames.length + " 席"),
          h("span", { style: { marginLeft: "auto", display: "flex", gap: T.gap } },
            btn(state.loading ? "检测中…" : "检测并搭建", runDetect, true),
            btn("刷新", refresh))),

        state.error
          ? h("div", { style: { margin: T.pad, padding: T.pad, border: "1px solid " + T.red, color: T.red, fontSize: T.small } },
              "桥接不可用：" + state.error)
          : null,

        h("div", { style: { display: "flex", borderBottom: "1px solid " + T.line, background: T.bg2 } },
          StatCell("中枢", hub.held ? "运行中" : "未运行", hub.held ? T.green : T.red),
          StatCell("进程", (processes.hub || 0) + " hub / " + (processes.interface || 0) + " 席"),
          StatCell("监督", processes.supervisor || 0, (processes.supervisor === 1) ? T.green : T.amber)),

        h("div", {
          style: {
            display: "grid", gridTemplateColumns: "1fr 72px 1fr", padding: "0 " + T.pad, height: "28px",
            alignItems: "center", background: T.bg2, borderBottom: "1px solid " + T.line,
            fontSize: T.micro, letterSpacing: ".14em", textTransform: "uppercase", color: T.fg2,
          },
        }, h("span", null, "席位"), h("span", null, "状态"), h("span", null, "持有者")),

        seatNames.length
          ? seatNames.map((k) => SeatRow(k, seats[k]))
          : h("div", { style: { padding: T.pad, color: T.fg2, fontSize: T.small } },
              state.loading ? "读取中…" : "还没有席位配置（先跑 uai seats --from-detect）"),

        detect && detect.results
          ? h("div", { style: { borderTop: "1px solid " + T.line, marginTop: T.pad } },
              h("div", { style: { padding: "8px " + T.pad, fontSize: T.micro, letterSpacing: ".14em", textTransform: "uppercase", color: T.fg2 } }, "检测结果"),
              detect.results.map((row) => h("div", {
                key: row.id,
                style: {
                  display: "grid", gridTemplateColumns: "1fr 96px 1fr", alignItems: "center",
                  height: "30px", padding: "0 " + T.pad, borderTop: "1px solid " + T.line, fontSize: T.small,
                },
              },
                h("span", { style: { color: T.fg1 } }, row.label || row.id),
                h("span", {
                  style: {
                    color: row.state === "ready" ? T.green : row.state === "auth_failed" ? T.amber : T.fg3,
                    fontSize: T.micro, letterSpacing: ".1em", textTransform: "uppercase",
                  },
                }, row.state),
                h("span", {
                  style: { fontFamily: T.mono, fontSize: T.micro, color: T.fg3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
                }, row.binary || "-"))))
          : null);
    }


    function StatusCard(props) {
      const T2 = T;
      const phase = props && props.phase;
      const block = props && props.block;
      const result = (block && block.result) || null;
      const value = (result && result.value) || result || null;
      const ok = value && value.ok === true;
      const label = phase === "preparing" ? "调用中" : phase === "start" ? "执行中" : ok ? "完成" : "失败";
      const color = phase === "preparing" || phase === "start" ? T2.amber : ok ? T2.green : T2.red;
      const cells = [];
      if (ok) {
        cells.push(["中枢", value.hub_held ? "运行中" : "未运行", value.hub_held ? T2.green : T2.red]);
        cells.push(["席位", String(value.seats_running) + "/" + String(value.seats), T2.fg1]);
        cells.push(["监督", String(value.supervisor), value.supervisor === 1 ? T2.green : T2.amber]);
      }
      return h("div", {
        style: {
          border: "1px solid " + T2.line, background: T2.bg1, color: T2.fg1,
          fontFamily: T2.sans, fontSize: T2.body, padding: "8px 12px",
        },
      },
        h("div", { style: { display: "flex", alignItems: "center", gap: "8px", marginBottom: cells.length ? "6px" : "0" } },
          h("i", { style: { width: "6px", height: "6px", borderRadius: "50%", background: color } }),
          h("span", { style: { fontWeight: 600 } }, "UAI 常驻面"),
          h("span", { style: { marginLeft: "auto", fontSize: T2.micro, letterSpacing: ".1em", textTransform: "uppercase", color: color } }, label)),
        cells.length
          ? h("div", { style: { display: "flex", gap: "16px" } },
              cells.map((cell) => h("div", { key: cell[0], style: { display: "flex", gap: "6px", alignItems: "baseline" } },
                h("span", { style: { fontSize: T2.micro, letterSpacing: ".12em", textTransform: "uppercase", color: T2.fg2 } }, cell[0]),
                h("span", { style: { color: cell[2], fontVariantNumeric: "tabular-nums" } }, cell[1]))))
          : null,
        !ok && value && value.summary
          ? h("div", { style: { fontSize: T2.small, color: T2.fg2 } }, String(value.summary))
          : null);
    }


    function DetectCard(props) {
      const phase = props && props.phase;
      const block = props && props.block;
      const result = (block && block.result) || null;
      const value = (result && result.value) || result || null;
      const ok = value && value.ok === true;
      const label = phase === "preparing" ? "检测中" : phase === "start" ? "检测中" : ok ? "完成" : "失败";
      const color = ok ? T.green : phase === "preparing" || phase === "start" ? T.amber : T.red;
      const readyIds = value && value.ready_ids ? String(value.ready_ids).split(",").filter(Boolean) : [];
      return h("div", {
        style: {
          border: "1px solid " + T.line, background: T.bg1, color: T.fg1,
          fontFamily: T.sans, fontSize: T.body, padding: "8px 12px",
        },
      },
        h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
          h("i", { style: { width: "6px", height: "6px", borderRadius: "50%", background: color } }),
          h("span", { style: { fontWeight: 600 } }, "AI Agent 能力检测"),
          h("span", { style: { marginLeft: "auto", fontSize: T.micro, letterSpacing: ".1em", textTransform: "uppercase", color: color } }, label)),
        value && value.summary
          ? h("div", { style: { marginTop: "6px", fontSize: T.small, color: T.fg2 } }, String(value.summary))
          : null,
        readyIds.length
          ? h("div", { style: { marginTop: "6px", display: "flex", gap: "6px", flexWrap: "wrap" } },
              readyIds.map((id) => h("span", {
                key: id,
                style: {
                  fontSize: T.micro, letterSpacing: ".08em", textTransform: "uppercase",
                  border: "1px solid " + T.line, color: T.fg1, padding: "1px 6px",
                },
              }, id)))
          : null);
    }

    function PanelIcon() {
      return h("span", {
        style: { display: "inline-block", width: "14px", height: "14px", border: "1.5px solid currentColor", borderRadius: "3px" },
        "aria-hidden": "true",
      });
    }

    function apply(ctx) {
      ctx.slots.inject("sidebar.panellist", () =>
        ctx.slots.register({ name: "sidebar.panellist", id: PANEL_ID, order: 5, label: () => "UAI 协作" }, PanelIcon));
      ctx.slots.inject("main", () =>
        ctx.slots.register({ name: "main", id: PANEL_ID }, Panel));
      // 思考链里的自定义卡片：key 是 wire 工具名（必须与宿主注册的工具名一致）
      ctx.slots.inject("tool.call.toolview", () =>
        ctx.slots.register({ name: "tool.call.toolview", key: "uai_status" }, StatusCard));
      ctx.slots.inject("tool.call.toolview", () =>
        ctx.slots.register({ name: "tool.call.toolview", key: "uai_detect" }, DetectCard));
    }

    return { apply, PANEL_ID, default: { apply } };
  },
});
