# Universal Agent Interface (UAI)

**跨厂商 agent 协作 + 验收治理**：来自不同 runtime 的 agent（Codex、Claude Code、DeepSeek Harness、任意 CLI）在同一中枢下协作——并带强制验收门禁：执行席自报"完成"在第三方核验裁定之前**一律不算数**。

> 诚实定位：已在真实多 agent 交付流水线中验证（4 席位、3,000+ 账本记录、拦下 7 次伪证事故）。单人单机小任务不需要这个——部署成本大于收益。

## 为什么

LLM agent 会伪造"完成"。提示词治不了，机制才能治。UAI 把**执行状态**与**验收裁定**分离：

- `SUCCEEDED` 只表示"停了并交了声明"——没有 VERDICT 裁定记录，对外一律投影为"待核验"。
- **核验席 ≠ 实现席**（强制，带测试）。
- 证据分级 E1–E4；自证不能冒充独立核验。

## 快速开始（30 分钟，Linux/macOS/WSL）

```bash
git clone https://github.com/3338902669-ops/universal-agent-interface.git
cd universal-agent-interface

# 1. 依赖
pip install nats-py jsonschema cryptography

# 2. NATS（任意 ≥2.9 版本）
nats-server -c prototype/nats-test.conf        # 监听 127.0.0.1:43321

# 3. 中枢
python prototype/hub.py --base prototype --nats-url nats://127.0.0.1:43321

# 4. 验收门禁投影（只读，不依赖任何 runtime）
python stage2/test_projection.py               # 10/10
python stage2/test_a2a_server.py               # 12/12
```

完整套件（24 个套件 300+ 用例）与真 lab 端到端（A2A 请求 → 信封 → 中枢 → 真 runtime → 账本）见 [stage2/DEVELOPMENT.md](stage2/DEVELOPMENT.md)。

## 组成

| 层 | 路径 | 职责 |
|---|---|---|
| 契约 | `protocol/contract-v1.md`、`schemas/` | v1 信封（11 必填）、canonical 裁定词表、JSON Schema、一致性套件 |
| 原型 | `prototype/` | NATS 中枢：幂等键、租约+围栏、单写者资源串行、账本哈希链 |
| 治理层 | `stage2/` | A2A 投影（AgentCard/Task/事件）、入站 message/send、SSE 事件流、webhook push（HMAC 签名）、OIDC/Ed25519 身份验证器、跨仓统一模型 |
| 桥接 | `stage2/paseo_uai_bridge.py` | Paseo daemon 编排 ↔ UAI 验收 |

## 架构一图

```
runtime 席位 (codex / claude / dsh / 你的)
        │  provider CLI
   Paseo daemon（或任意编排器）
        │  信封（契约 v1）
   UAI 中枢 — 账本（只追加+哈希链）+ 裁定
        │  投影（AgentCard / Task / SSE / webhook）
   外部 A2A 消费方
```

设计规则：**语义沿用 A2A**（名片、任务状态），**传输可替换**；验收/裁定逻辑是内部资产，绝不外包。

## 关键不变量（ENFORCED，每条带测试）

- `SUCCEEDED ≠ 通过`——无裁定的任务对外显示"待核验"
- 幂等只治丢失；伪证由裁定席处理
- 核验者 ≠ 实现者；副作用状态 unknown 禁止自动重试
- 未配置验证器 → 身份保持 `asserted`，绝不升级
- event_bridge 在真实跨厂商消费方出现前保持 `experimental_test_only` 门禁

## 示例

- [examples/bridge-paseo](examples/bridge-paseo)：Paseo 编排对接（端到端已验证）
- [examples/second-runtime](examples/second-runtime)：接入第二厂商 runtime 的 150 行 adapter 范式

## 安装（PyPI，已构建 wheel）

```bash
pip install uai-governance   # 待发布——wheel 已构建并在隔离 venv 冒烟验证
```

## 现状 / 诚实缺口

- 跨厂商身份：OIDC 验证器就绪（Microsoft 生产 JWKS 实测兼容）；拿真实 IdP 签发的 token 属账号侧操作
- A2A streaming：SSE 已做；push notification（webhook + HMAC）已做；gRPC 绑定无计划
- event_bridge v1/v2 在真实外部消费方出现前保持 `experimental_test_only` 门禁

## 授权

见 LICENSE。Paseo 桥接受上游 AGPL-3.0 约束。
