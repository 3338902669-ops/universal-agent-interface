# UAI 证据约束裁定试点：项目计划书

- 状态：原型与本机复现完成；DSH 第三轮文档复核 PASS（无阻断）；天枢独立核验受审批沙箱阻挡，整体 pending/no-go
- 日期：2026-11-24
- 项目负责人：Codex
- 协作：DSH 只读评阅；天枢在实现完成后独立核验
- 执行选择：协作模式；不执行 codex-security；执行天枢独立核验
- 发布边界：只发布计划与架构资料到 GitHub 功能分支；不合并默认分支、不部署、不对外联络

## 1. 项目摘要

本项目用一个窄范围原型检验：在 AI 生成代码的交付验收中，把任务要求、执行方声明、原始产物、可观察运行证据、核验者隔离情况和最终裁定绑定起来，能否减少错误通过，并让人更快定位失败原因。

本项目不是通用多 agent 平台，也不预设 UAI 已有独有创新或付费需求。先验证协议是否能在受控任务中产生可复核、可重放的裁定，再决定是否投入生产化和客户试点。

## 2. 事实、推断与待验证假设

### 已核实事实

- UAI 已有 protocol/unified/evidence-record.schema.json，包含任务、证据等级、声明者、命令/退出码/修订号、载荷 URI/哈希/字节数/媒体类型、读取者等字段。
- protocol/NOT-GUARANTEED.md 明确说明：哈希与日志不能证明命令真实执行；执行环境侧可验证证明尚未实现。
- stage2/three_layer_verdict.py 当前以 verifier 与 implementer 的 agent ID 不同作为独立性判断的一部分；不同 ID 本身不证明身份、信息隔离或错误独立。
- 当前仓库工作树已有未提交变更。本试点会新增隔离目录，不覆盖这些变更。
- 查阅的商业计划草案将收入、成本和客户转化数字列为未经合同验证的预测，不作为本试点市场需求证据。

### 推断

若裁定记录能绑定冻结后的证据包，并让第三方在不接触执行者解释的条件下重算，可能降低追查“做了什么、凭什么通过”的人工时间。

### 待验证假设

1. 证据完整性门槛能降低已知缺陷被错误接受的比例。
2. 身份、盲审、环境隔离和共享依赖记录，比“不同 agent ID”更能解释核验者间的错误相关性。
3. 结构化裁定记录能降低人工复核时间和故障归因成本。
4. 有团队愿意为上述改善提供试点数据或付费。当前没有合同或付费客户证据支持该项。

## 3. 目标与不做事项

### 目标

- 复用现有证据记录、裁定词表与事件账本，不重复建设通用任务编排。
- 实现一个可离线重放的代码交付证据包原型；重放结构、绑定、摘要和裁定字段一致性，不从产物重新判断语义或推导最终裁定。
- 首轮只测离线包完整度、固定结构缺陷误接受、有效夹具误拒、合成根因归类与重放一致性；真实语义错误率、人工复核时间、复核成本和核验错误相关性均留待后续。
- 明确每类证据能证明和不能证明的事项。

### 不做

- 不改生产 Hub、运行席位配置或凭据。
- 不实现多 Hub 高可用、通用沙箱、完整跨组织身份联邦或通用 agent 市场。
- 不把文件哈希解释成真实性证明，不把多厂商或多席位直接称为统计独立。
- 不开展客户外联，不作价格或收入承诺，不部署、不合并到默认分支。

## 4. 范围与交付物

1. 本项目计划书（本文件）。
2. 执行计划：docs/superpowers/plans/2026-11-24-evidence-adjudication.md。
3. 架构设计：docs/architecture/2026-11-24-evidence-bound-adjudication.md。
4. 可在 GitHub 渲染的独立 Mermaid 图：docs/architecture/diagrams/evidence-bound-adjudication.mmd。
5. 隔离试验原型：experiments/evidence_adjudication/。
6. 本地试验报告与机器可读结果：记录运行版本、案例标签、逐项裁定、失败类别和局限；模型成本不适用（未调用模型）。

**GitHub 推送清单仅含交付物 1–4 的四份文档**：本项目计划书、执行计划、架构设计、独立 Mermaid 图。第 5 项原型、第 6 项报告与结果、`docs/experiments/` 下文件均保留本机，不进入该文档提交。

## 5. 试验设计

首轮是**离线协议一致性试验**，不是对 agent 判断能力或市场需求的统计验证。只使用无凭据、无外部副作用的合成代码交付包，测试 UAI 能否把证据绑定起来、拒绝结构错误，并从完整证据包离线重放结构、绑定和摘要校验；它不重新判断产物语义，也不推导新的最终裁定。

建立 120 个固定夹具：40 个有效包、40 个已明确标记为“不通过”的声明包、40 个证据缺陷包。声明不通过组有一项预置 fail check，只检验裁定/接受标记一致性，不代表检出了真实实质缺陷，也不用于估计语义错误率。每个证据缺陷包都从字段完整、裁定为“通过”的有效包基线生成，再注入一项缺陷；oracle 要求 integrity gate 拒绝（`ok=false`）且裁定为“未核”，避免样本标签先替校验器拒绝。缺陷覆盖过期、错任务、错修订、payload/manifest 摘要不符、契约摘要错误、自我核验及缺少 payload/产物。路径越界、大小写冲突和硬链接由定向 loader 单测覆盖，不计入这 120 例。测试 oracle 由夹具生成规则定义，属于合成测试标签，不称为人工金标准或真实世界标签。

每个包包含任务契约快照、符合 Evidence Record v1 schema 的 evidence_records、冻结产物与 manifest、执行声明、运行观察、唯一的 verifier_record 和 adjudication。每项验收条件须有唯一 criterion ID，checks 必须逐项覆盖且引用 evidence ID。外层 profile 用固定相对 `$ref` `../../protocol/unified/evidence-record.schema.json`，loader 只解析本地仓库文件；受限解释器只实现 profile 与 v1 schema 实际用到的关键字，遇未知关键字失败关闭，source/schema hash 固定解释器与 schema。Evidence Record v1 允许 payload 缺省；本试点 profile 额外要求每条记录有 payload，不修改 v1。task_id 在 packet/contract/每条 record 一致，contract_id/run_id 在 packet 与 claim 一致；每条 payload 的 URI、SHA-256、字节数和 media_type 与 manifest、读取字节绑定；E1 记录的 revision 与 claim/manifest 一致。`verifier_record` 和 `adjudication` 均不自带 packet digest；最终裁定者与 verifier principal 相同。packet digest 覆盖完整 packet；校验失败时若 JSON 可解析和规范化仍返回 digest，loader 无法解析的输入可能没有 digest。事件摘要若后续实现，放在 packet 外的 ledger envelope。bundle directory 是文件目录，不等同 packet 的 `bundle` 元数据对象。仅 loader 做路径规范化和链接/越界拒绝；verifier 只比较已加载字节与 manifest key。过期按固定 UTC `bundle.as_of` 与 `evidence_max_age_seconds` 计算，转为未核。

首轮不调用付费模型 API，不让 reviewer 对 120 个样本作主观判断。因此它可以检验格式、绑定、离线重放和确定性拒绝逻辑，**不能**检验语义错误通过、语义误拒、真实人工复核时间、跨厂商 verifier 独立性或付费意愿。上述项目列入后续盲审试点，需另行定义抽样、双核验者分配、顺序随机化、人工计时和费用口径；不得把席位 ID 或厂商差异单独当成独立性证明。

## 6. 指标与暂定门槛

首轮只对离线协议机制设门槛，语义表现与商业价值不作结论：

- 包完整度：以 packet 可通过 schema 结构解析且 loader 成功装载为前提，衡量必需材料是否存在且可寻址；不表示哈希或绑定内容正确。缺 payload、缺产物、缺引用/覆盖的包不完整；contract hash 错误但材料齐全仍算完整。
- 结构错误误接受：固定缺陷夹具中 `integrity gate ok=true` 的数量；不能用预先给定的 `accepted=false` 标签代替校验结果。目标为 0，只表示固定测试集结果。
- 有效包完整性误拒：固定有效夹具中 `ok=false` 的数量；与最终业务接受标志分列，不把完整性通过等同业务裁定正确。
- 离线重放：相同冻结 packet、产物与规则版本产生相同结构/绑定/摘要校验输出；不从原始产物重做语义判断，也不推导新的最终裁定。采用实验内 `UAI-CJSON-v1`：UTF-8、对象键 code point 排序、紧凑 JSON、NFC 字符串、只允许安全范围整数/布尔/null/数组/对象；转义按 Python JSON string escaping v1 固定；拒绝浮点、`-0`、重复键与孤立 surrogate。packet digest 为整个 packet JSON 的 canonical hash，覆盖 `bundle.as_of`、身份/核验、裁定、源码/schema/canonicalizer hashes；packet 外事件不混入自身摘要。该算法不声称实现 RFC 8785。
- 失败归因：对预先定义的格式错误根因输出混淆矩阵；每类样本少于 10 时不报告宏平均 F1。
- 核验者共同漏检/错误相关性、语义错误通过率、语义误拒率、人工复核时间、review 成本：本轮不测，报告中必须标为“未测”，不能填 0。
- 运行耗时和本地计算量可作为实验描述记录；若未调用模型，模型费用记为“不适用”，不能外推未来单位成本。
- 固定样本报告分子、分母和比例；这是对完整合成语料的描述性普查，不给总体置信区间，也不支持真实任务可靠性保证。

## 7. 阶段、投入与决策点

| 阶段 | 时间 | 主要工作 | 粗估投入 | 退出门 |
|---|---:|---|---:|---|
| 0. 计划与契约冻结 | 第 1 周 | 关闭 Evidence Record v1 映射、相对 schema 引用、包内产物绑定、裁定映射和安全路径约束；DSH 只读评阅 | 0.5–1 人周 | DSH 复阅未留 P0；天枢回执独立记录，不以评阅代替 |
| 1. 离线原型 | 第 1–2 周 | 外层 packet profile、受限离线 schema 解释器、纯函数校验器、冻结字节包、离线复算和故障夹具 | 1–2 人周 | v1 records 按本试验解释器通过既有 schema 关键字检查；包离线可复算；无 shell/网络/包根外读取 |
| 2. 固定样本评测 | 第 2–3 周 | 对 120 个合成夹具运行正例、缺陷、篡改、错绑和重放矩阵 | 1–2 人周 | 断言、分子/分母、失败日志和报告可由原始样本复现 |
| 3. 独立核验与决策 | 第 4 周 | 天枢按冻结源码/schema hash 独立复算；Codex 提交 go/no-go 建议，用户决定是否进入真实 verifier 试点 | 0.5–1 人周 | 独立证据与自检分开；未测指标明确列出 |

**Go/no-go 门槛：**只有阶段 1/2 硬门全部通过、天枢提供可复现回执（命令、退出码、输入/输出 hash、复现说明）、未关闭阻断清单为零且所有未测指标仍标为 null/not_measured，才能提交 go 建议。天枢回执缺失或受阻时结论只能为 pending/no-go；Codex 自检不替代独立核验。

合计粗估 2.5–5 人周，为可用 agent 时间折算的规划数，不是供应商报价、获批预算或已验证商业成本。首轮不调用付费模型 API。真实 verifier 比较、人工计时和客户验证属于下一阶段，不能由本轮自动升级。

## 8. 评阅问题闭环与当前证据

| DSH 首轮发现 | 修订 | 复现证据 / 状态 |
|---|---|---|
| 缺陷夹具预标注“不通过”，会污染 false accept | 改为 valid accepted baseline 后注入单项缺陷；oracle 从 spec 读取 `expected_integrity=false`；按 `row.ok` 计结构误接受 | 33 项单测；120/120 oracle 一致，缺陷完整性 false accept 0/40 |
| 完整度受校验顺序影响 | 按必需材料存在/可寻址计数，与 hash/binding 分离 | completeness 112/120；contract hash 错误仍完整，missing payload/artifact 不完整 |
| malformed packet/manifest 可崩溃 runner，控制字符未拒绝 | loader 验类型、路径控制字符；runner 捕获常见解析异常与 `RecursionError` 并逐例转为未核 | 深度 6000 的 malformed JSON 端到端回归通过；仍属本机自检，待独立复核 |
| Windows 实际目录大小写不匹配未查 | loader 按目录分量核对实际名称，并拒绝尾随空格/点及 Windows 保留设备名 | 大小写、alias 和保留名定向测试通过；symlink 用例仍跳过 |
| 评测逻辑未进入冻结来源 hash | frozen source 包含 loader/verifier/generator/metrics/runner/schema | 最新 `rules_source_sha256` 与复跑摘要附于本地试验报告 |
| 缺少发现到修订的闭环表 | 本表逐项记录首轮七项发现及第二轮新增修正 | DSH 下一轮只读复阅待回执 |
| 图示暗示运行时生成/回写核验与裁定 | 图示改成冻结前由外部核验流程提供记录，冻结后单向加载与重放 | 嵌入图与独立 MMD 文本一致（换行符归一后）；DSH 第三轮复核通过 |
| 四份文档状态不一致，发布清单含糊 | 四份状态统一；明确只推送计划/执行计划/架构/Mermaid 四文件 | DSH 第三轮复核通过 |
| completeness 与 schema/loader 前置关系未明 | 定义增加“schema 解析与 loader 成功”前提 | 112/120 数值不变；措辞待下一轮复阅 |
| 天枢阻塞声明缺少可寻址输出锚点 | 已列原始 stdout 路径与 SHA-256；说明 result.json 缺失 | `C:\Users\33389\.shared\comms-v2\cutover-base-20261109\runtime\interfaces\tianshu\results\25b0a037-6a4b-47a7-b1c6-8fe5df532dc2\runtime.stdout.jsonl` SHA256 `6f57308a6493ba37372b6939bdda86497a5582a5f5a1a97cb5deaa63e5796869` |
| expected_integrity 与 spec 重复手写 | generator 改为读取 `SPEC.expected_integrity[group]` | 33 项单测；spec/oracle 哈希与 summary 一致 |
| canonicalizer hash 易被理解为实现代码 hash | 报告注明它固定规范描述，实现由 `rules_source_sha256` 固定 | 报告已注明；待下一轮复阅 |

DSH 第三轮终审 task `d6bd53ce-64f8-4984-821f-4680aa0e8b51`：PASS、无阻断；仅剩 Mermaid 字节换行表述（已改为文本一致）和 Task 5 勾选状态（已改为完成）两项文档细节。回执：`C:\Users\33389\.shared\comms-v2\cutover-base-20261109\runtime\interfaces\dsh\results\d6bd53ce-64f8-4984-821f-4680aa0e8b51\result.json` SHA-256 `ffe4ef962f0cd43ff88a9b45a9d2fc84252a840941e24b2bc63c7e62613737d4`。此为 DSH 文档复核，不等于 TianShu 独立代码/运行验收。

天枢独立核验任务 `25b0a037-6a4b-47a7-b1c6-8fe5df532dc2` 读取输入 hash 并完成当时版本的 26 项测试，但将 runner 输出写入 `$TEMP` 时被沙箱审批门拦截，故未完成 runner 复算及完整机器证据。原始 stdout：`C:\Users\33389\.shared\comms-v2\cutover-base-20261109\runtime\interfaces\tianshu\results\25b0a037-6a4b-47a7-b1c6-8fe5df532dc2\runtime.stdout.jsonl`，SHA-256 `6f57308a6493ba37372b6939bdda86497a5582a5f5a1a97cb5deaa63e5796869`；Hub `result.json` 缺失。按原任务不重试、不绕过审批门，独立核验保持待完成；本机测试属于实现者自检，不能替代。未执行 codex-security。

## 9. 风险和停止条件

- 真实性风险：执行声明可能为假；本轮不实现执行 attestation，不声称命令真实运行已获证明。
- 篡改边界：哈希检查能发现当前包内容与其摘要不一致；本地管理员可同时改包和摘要。本轮测试不宣称抵御拥有存储权限的恶意管理员。
- 样本偏差：合成 oracle 只检验程序是否满足预先写定的规则，不代表真实软件缺陷分布或人类验收质量。
- 相关错误风险：本轮没有两个语义 reviewer 的配对判断，故不测 reviewer 独立性或错误相关性。
- 误拒风险：过严的结构门槛可能阻断有效交付；有效夹具和缺证据夹具分别报告。
- 商业风险：未访谈客户、未验证预算；技术门槛通过不代表市场成立。
- 停止条件：Evidence Record v1 无法兼容复用；完整冻结包无法在离线环境复算；缺陷包可通过确定性门禁；有效包误拒无法解释；试验结果不能由他人从原始夹具复算。语义可靠性和商业投入决策留待下一阶段证据。

## 10. 后续商业验证

技术试点通过后，再选择一个使用 AI 编码 agent 的小团队，取得授权的脱敏任务样本，比较现行流程和证据包流程，并询问是否愿为节省的复核时间、审计能力或降低的交付风险付费。只有访谈记录、试用行为、预算承诺或合同才升级为需求证据。营业额预测继续标为假设，直至有客户证据。
