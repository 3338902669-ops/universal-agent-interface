# 云端交接：待完成项与完成指南（2026-11-12）

> **状态：历史文件（2026-11-13 加注）。** 本文 §3 的 11 项已基本落地（PR #1 已合并），
> 文中「当前基线：stage2 12 个套件 224 条用例 / conformance 17/17」**已过时**——
> 现行数字是 **24 套件**、**conformance 22/22**（见 `bench/ci_suites.py`、
> `protocol/tests/conformance/run_conformance.py`）。请以仓库当前 HEAD 为准，
> 不要把本文基线当成待守门槛。

> 本文件供 **GrokBot（云端）** 在没有本机访问权的情况下独立完成剩余工作。
> 一切以 GitHub 仓库为准：`https://github.com/3338902669-ops/universal-agent-interface`

---

## 0 你要做什么（一句话）

把本文件 §3 的 **11 个待完成项**做完，每项产出可验证的实现或规格、跑通自检，并开 PR。
**不允许修改 main 分支**；**不允许在没有实际运行输出的情况下声称 PASS**。

---

## 1 项目是什么

一套面向异构 Agent Runtime 的接口契约 + 参考实现。三层：

| 层 | 仓库 | 职责 |
|---|---|---|
| 协作层 | `agent-orchestra` | 41 条机器可读要求（Task/Resource/Ownership/Handoff/Verification/Evidence/Approval/Recovery/Activation/Gate） |
| 治理层 | `governed-agent-runtime` | I11–I18 不变量：身份不可变、grant 单次、哈希链账本、产物篡改即失效信任 |
| 互操作层 | `universal-agent-interface`（**你要改的就是这个**） | 任务信封、状态机、租约围栏、六值裁定、A2A 边界投影 |

**核心命题**：不是"让 agent 更强"，而是"让 agent 说的话可以被证伪"——执行者的自报不算数，必须由未参与执行的一方按证据裁定。

## 2 已经完成的（不要重做）

**P0 全部 13 项已完成**，对应文件：

| 项 | 文件 |
|---|---|
| 证据等级×载荷 | `protocol/unified/evidence-record.schema.json` |
| 证据下限表 | `protocol/unified/evidence-floor.json` |
| 不保证清单 | `protocol/NOT-GUARANTEED.md` |
| 状态两轴 + 责任矩阵 | `protocol/unified/unified-model.md` |
| 跨仓一致性检查 | `stage2/check-cross-repo-vocab.py`（17 项）+ CI |
| A2A 入站→中枢 | `stage2/inbound_bridge.py` + `test_a02_inbound.py`（8/8，需真 lab） |
| 出站 SSE | `stage2/a2a_server.py` `/tasks/<id>/events` + `test_a03_stream.py`（6/6） |
| 跨域身份接口 | `stage2/identity.py` + `test_identity.py`（10/10） |
| 四档基线基准 | `bench/sim.py` + `bench/run_benchmark.py` |
| 第二 runtime | `stage2/runtime_adapters/dsh_adapter.py` + `test_a05_second_runtime.py`（10/10） |
| 中枢单例 | `stage2/hub_singleton.py` + `test_a09_singleton.py`（13/13） |
| 改派 | `stage2/reassign.py` |
| 威胁模型 | `protocol/THREAT-MODEL.md` |
| 账本哈希链 | `stage2/ledger_chain.py` |
| 故障注入统一格式 | `protocol/unified/fault-injection.schema.json` |

**基线（2026-11-12 当日；已过时，见文首状态注）：`stage2` 12 个套件 224 条用例全绿，`protocol/tests/conformance` 17/17。**
任何改动必须保持这两组不退化。

---

## 3 待完成项（11 项，按优先级）

### E-02 受控对照实验公开化（P1，最高优先）

**问题**：`bench/README.md` §3 写了五席测试电池的方法学（冻结判分脚本 / 同题逐字一致 / 仅工作目录按席替换 / 隔离会话 / 通道失败与能力失败分开统计），但没有可复现包。

**交付**：
```
bench/controlled_experiment/
  DESIGN.md          # 自变量、因变量、控制变量、失效判据
  run.py             # 一键跑，输出 raw/ 与 summary.json
  fixtures/tasks.jsonl   # 六道机械判分题（题目 + 唯一答案 + 判分器）
  analyze.py         # 读 raw/ 出 5 项指标
  raw/               # 原始结果（每次运行一个新目录）
  RESULTS.md         # 汇总
```

**验收**：
1. `python analyze.py --raw raw/<run>` 在**没有 raw 数据**时报错退出码非 0（不得假装有结果）。
2. `fixtures/tasks.jsonl` 每题都有 `scoring` 字段，且判分器是**纯函数**（同样输入必得同样分数）。
3. `DESIGN.md` 明确写出：哪些是自变量（有无 skill 编排）、哪些是控制变量（模型/工具/权限/工作区快照）、哪些是失效判据（通道失败 vs 能力失败）。
4. `RESULTS.md` 只能填**真实跑过**的数据；没跑就写「未运行」。

**禁止**：用编造数字填 RESULTS.md。这是全项目最不能犯的错。

---

### E-04 独立复跑进 CI（P1）

**问题**：现在独立复跑靠人工派 subagent，CI 只跑自跑。

**交付**：
```
.github/workflows/independent.yml    # 独立作业，不共享 cache
stage2/verify_independent.py         # 跑完全部套件，产出 evidence/independent-<sha>.json
```
`verify_independent.py` 要点：
- 逐个套件跑，记录 `RESULT` 行、退出码、耗时、Python 版本、git sha
- 任一套件非 0 → 整体失败
- 产出 JSON 含 `"evidence_level": "E3"`（自跑），并写明「独立复跑需由未参与实现的席位执行，本文件只是可核证的原始记录」

**验收**：`independent.yml` 与 `conformance.yml` 是**两个 job**，且 `independent.yml` 里不得 `uses: actions/cache`。

---

### B-03 SLA / 结算 / 数据驻留字段（P2）

**交付**：`protocol/unified/service-level.schema.json`

必含：
```json
{
  "sla": {"response_s": int, "completion_s": int, "availability": "0.xx"},
  "billing": {"unit": "task|token|minute", "rate_ref": "string|null"},
  "data_residency": {"region": "string", "max_classification": "public|internal|confidential|restricted"}
}
```
并在 `protocol/unified/unified-model.md` 加一节说明「这些字段是**声明**，不是保证；与 NOT-GUARANTEED.md 冲突时以后者为准」。

**验收**：`stage2/test_protocol_specs.py` 增加 3 条断言（schema 合法 / 样本通过 / 缺 region 被拒）。

---

### C-06 ~ C-10 GAR 侧五个已知缺陷（P2）

**这些不是改 UAI 的代码，而是给 GAR 写修复提案。** 你没有 GAR 的写权限也没关系。

**交付**：`protocol/gar-proposals/` 下五个文件：

| 文件 | 缺陷 | 要写的 |
|---|---|---|
| `F-GAR-06-E6-verifiable-execution.md` | E6「命令真跑过」OUT OF SCOPE | 如何用执行环境侧记录（进程树 + 退出码 + 时间戳）把 E6 升级为可验证；给出最小数据结构与判定规则 |
| `F-GAR-07-trust-root.md` | 信任根在 gate 外 | 双人 bootstrap 的最小实现（两次签名 + 可审计事件） |
| `F-GAR-08-pure-authorize.md` | 授权非纯函数（`assertIntegrity` 有副作用） | 把副作用移出 `authorize()` 的重构方案（检测与吊销分离），含 diff |
| `F-GAR-09-monotonic-clock.md` | grant TTL 用可注入时钟 | 换成单调源后如何保持测试可确定性（注入 seam 挪到哪一层） |
| `F-GAR-10-internals-shape.md` | `internals:true` 与 facade 形状不一致 | 统一 facade 的最小改动 + 兼容层 |

每个文件必须含：**问题复述 → 最小修复 diff（针对 `governed-agent-runtime`）→ 验收测试用例 → 不修的风险**。

**验收**：五个文件都在；每个都有一个 ` ```diff ` 块；每个都有一个「验收测试」小节。

---

### F-05 两把失效密钥作废（用户动作，不是你）

`OPNAI_GENERAL_KEY` / `OPENNEXT_API_KEY` 实测 401。**这一项由本机用户处理，你不用做**，但在最终报告里注明「未处理，需用户操作」。

---

## 4 工作方式（硬性）

1. **分支**：从 `main` 切 `cloud/todo-20261112-<你的席位名>`，全部改动提交到该分支，开 PR 说明完成了哪几项。
2. **不改 main**，不 force push，不删别人文件。
3. **每项独立提交**，commit message 前缀 `E-02:` `E-04:` `B-03:` `C-06:` 等，便于逐项 review。
4. **跑测试再声称通过**。允许的表述：
   - 「已实现，自测 X/Y 通过」——必须贴 `RESULT:` 行与退出码
   - 「未运行」——如果环境跑不了
   - **禁止**：「已完成」「已验证」「PASS」而无运行输出
5. **不许伪造数据**。`bench/**/RESULTS.md`、`evidence/*.json` 里只写真实跑出来的内容。
6. **不要引入密钥**。任何 token / key / 密码不得出现在仓库、commit、测试夹具里。测试用 `os.environ` 读，缺了就 SKIP 并打说明。
7. **保持基线**：`stage2` 224 条 + `conformance` 17/17 不得退化。退化就算 PR 不通过。

## 5 环境与命令

```bash
git clone https://github.com/3338902669-ops/universal-agent-interface
cd universal-agent-interface
python -m pip install -r protocol/tests/conformance/requirements.txt
python -m pip install nats-py

# 基线校验（必须全绿）
cd protocol/tests/conformance && python run_conformance.py      # SUMMARY 17/17 PASS
cd ../../stage2 && python test_projection.py && python test_io.py \
  && python test_unified.py && python test_protocol_specs.py \
  && python test_bridge_joins.py && python test_a2a_server.py \
  && python test_a03_stream.py && python test_identity.py \
  && python test_p1_batch.py && python test_a09_singleton.py \
  && python test_b02_ao_refs.py && python test_p2_batch.py
python check-cross-repo-vocab.py     # 兄弟仓不在时 SKIP，不算失败

# 四档基准
cd ../bench && python run_benchmark.py --self-check
```

**注意**：
- `test_a02_inbound.py` 与 `test_a05_second_runtime.py` 需要真 lab（NATS + hub）与真 runtime，云端如无条件可 SKIP，但要在报告里写明。
- `test_b02_ao_refs.py` 需要兄弟仓；没克隆时自动 SKIP。

## 6 完成后怎么报

在 PR 描述里贴这段表，逐项填：

| 项 | 状态 | 证据（RESULT 行 / 文件路径） |
|---|---|---|
| E-02 | 完成 / 部分 / 未做 | … |
| E-04 | | |
| B-03 | | |
| C-06~C-10 | | |
| F-05 | 需用户操作 | — |

末尾附：
- 基线是否仍绿（224 + 17/17）
- 哪些项你**没有**验证过（诚实列出）
- 你改动涉及的文件清单

---

## 7 如果你只能做一件事

**做 E-02。** 它是这批里唯一能把"我们声称有效"变成"别人能复现"的东西，也是外部评估里唯一还站得住的批评（有工程、没公开基准）。
