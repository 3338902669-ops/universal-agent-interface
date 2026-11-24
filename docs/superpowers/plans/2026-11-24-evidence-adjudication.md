# UAI 证据约束裁定试点实施计划

> **执行状态（2026-11-24）：** 原型及本机复现已完成；DSH 第三轮文档复阅 PASS（无阻断）；天枢执行当时版本单测但沙箱审批阻止 runner 产物落盘，独立核验未完成，当前不可作 Go 结论。

**Goal:** 构建并评测一个隔离、可离线重放的代码交付证据包原型，验证外层包绑定、结构错误拒绝、摘要/绑定重放和合成根因归类。它不重判产物语义、不推导新裁定；语义错误率、人工复核时间、成本、错误相关性与市场需求不在首轮量化。

**Architecture:** 外层试验 packet 通过固定相对 `$ref` 复用现有 Evidence Record v1；离线重放单元为冻结规则/schema hashes + packet + bundle directory。directory 含 `packet.json` 与产物字节；packet 顶层字段保存契约快照、manifest、声明、观察、verifier_record 与 adjudication；packet 内 `bundle` 只记录 as_of 和 manifest 路径索引。packet 外事件账本本轮不实现。实现与夹具均放在 experiments/evidence_adjudication/，不改生产 Hub。

**Tech Stack:** Python 3 标准库；仅实现本试点 profile 与 v1 schema 已用关键字的 fail-closed schema 解释器（非通用 JSON Schema validator）；Python 标准库 unittest；Mermaid 图。

## Global Constraints

- 原型只处理合成或已授权脱敏样本，不读取凭据，不触发真实外部副作用。
- 不改 prototype/hub.py、prototype/interface.py、stage2/memory_*、运行席位和现有未提交文件。
- 不把 SHA-256、执行者自报、模型/席位差异当作命令真实性或核验独立性证明。
- 对外发布范围仅限用户已授权的计划与架构文档功能分支；不合并、不部署。
- 不执行 codex-security；完成后由未参与实现的天枢独立核验。
- 文档先公开到功能分支并同步全队；实验代码不自动推送 GitHub。

---

### Task 1: 冻结基线与 v1 profile

**Files:**
- Read: protocol/unified/evidence-record.schema.json
- Read: protocol/contract-v1.md
- Read: protocol/NOT-GUARANTEED.md
- Read: stage2/three_layer_verdict.py
- Create: experiments/evidence_adjudication/README.md
- Create: experiments/evidence_adjudication/experiment-spec.json

- [x] 记录公开基线 commit 和当前工作树已有改动；不改也不暂存既有变更。
- [x] 固定外层 packet profile 使用 evidence_records 数组，数组项以 `../../protocol/unified/evidence-record.schema.json` 相对 `$ref` 引用既有 v1 schema；从 packet schema 的本地 file URI 解析，仅允许该精确仓库内目标，不创建通用 registry、不访问网络；不得复制另一份 v1 定义。
- [x] 固定 contract_snapshot 必含验收条件、禁止项、工具范围、contract ID/version/hash；冻结 bundle directory 必含全部产物字节和 manifest；每条 v1 record 的 payload 缺省语义不变，但本试验 profile 强制要求 payload。
- [x] 将 execution_attestation 标记为本轮 out of scope；无受信任执行证明不得声称命令真实运行已被证明。
- [x] 明确只复用现有裁定词汇，不调用 stage2/three_layer_verdict.py 的 execute_all 或 shell 检查。
- [x] 断言：Evidence Record v1 的既有字段含义不被改变。记录批准的 profile 和输入文件 hash。

### Task 2: 建立冻结证据包与纯函数校验器

**Files:**
- Create: experiments/evidence_adjudication/evidence_packet.schema.json
- Create: experiments/evidence_adjudication/verifier.py
- Create: experiments/evidence_adjudication/tests/test_verifier.py

- [x] 外层 schema 的 evidence_records 项使用 `../../protocol/unified/evidence-record.schema.json` 相对 `$ref`；增加 contract_snapshot、artifact_manifest、唯一 `verifier_record` 和包内最终 `adjudication`，不更改 v1。task_id 在 packet/contract/每条 v1 record 一致；run_id/contract_id 在 packet 与 execution_claim 一致；profile 强制每条 record 有 payload，payload.uri 与 manifest path 完全相同且摘要/字节数/media_type 对上；E1 record/claim/manifest revision 一致。
- [x] schema loader 仅解析该固定本地 `$ref`；受限解释器支持当前两份 schema 实际使用的关键字，遇到未知关键字失败关闭；固定规则与 schema 文件 hash，不下载 metaschema。
- [x] 分为 `packet_loader`（唯一访问文件系统者，负责路径规范化、拒绝链接与 bundle 根外文件）与纯函数 `verifier`（只接收已加载 JSON 与字节）。loader 拒绝反斜杠、盘符、UNC、空段、`.`/`..`、URL、控制字符、大小写折叠冲突、Windows 实际大小写不匹配、尾随空格/点别名、保留设备名、symlink/junction/hardlink 与非普通文件；Windows symlink/junction 运行时用例仍待可用环境补验。
- [x] `bundle.as_of`、`contract_snapshot.evidence_max_age_seconds` 与时间字段均严格匹配 UTC 时间格式；证据过期按证据不足→未核，不自动判实质失败。
- [x] execution_attestation 不实现验证或可信等级提升；输出明确声明 `not_implemented` 和执行真实性边界。
- [x] `verifier` 纯函数校验已载入 JSON/字节；不执行 shell。`UAI-CJSON-v1` 固定 UTF-8、Unicode code point 键排序、紧凑 JSON、NFC 字符串与明确转义规则（见架构设计）；只接受绝对值 ≤2^53−1 的整数/bool/null/string/list/dict；拒绝 `-0`、float、重复键与孤立 surrogate。packet digest 覆盖整个 packet，包含 bundle.as_of、最终 adjudication、verifier_record 与所有 rule/schema hashes；可解析规范化的失败 packet 也返回 digest；packet 外 event/prev_digest 不在本轮实现。算法不是 RFC 8785。
- [x] 只输出 `identity_separated` 作为身份 ID 比较结果；盲审、只读、认证状态和共享依赖仍作为声明，不折算成独立性布尔值。
- [x] 裁定语义：有效 fixture `{通过}`、已声明不通过 fixture `{不通过}`、证据缺陷 `{未核}`；缺陷包从完整有效基线注入且 integrity gate 期望拒绝；无带注/条件通过例；oracle 只在指标阶段载入。
- [x] 定向测试覆盖本地 `$ref` 解析、v1 条件关键字、profile 强制 payload、E2 记录、检查覆盖、自裁绑定、离线字节、错任务/错修订/哈希与字节不符、包根路径/大小写冲突、Windows 路径 alias、过期、规范化顺序/空白稳定、重复键/`-0`/float 拒绝和 digest 字段覆盖。symlink 用例跳过；结果不声称真实环境防篡改保证。
- [x] 本机执行：`python -m unittest discover -s experiments/evidence_adjudication/tests -p "test_*.py" -v`；33 项，32 passed、1 skipped，退出码 0。新增深层 malformed JSON runner 用例通过。

### Task 3: 构造合成 oracle 夹具

**Files:**
- Create: experiments/evidence_adjudication/fixtures/cases.jsonl
- Create: experiments/evidence_adjudication/fixtures/oracle-labels.jsonl
- Create: experiments/evidence_adjudication/generate_cases.py

- [x] 生成 120 个合成夹具：40 个有效包、40 个含预置 fail check 的声明不通过包、40 个从有效 accepted baseline 注入单项缺陷的证据缺陷包；各类分别报告分子和分母。
- [x] 覆盖实际 corpus：缺失 payload/产物、过期、错任务、错修订、payload/manifest 与字节摘要不符、契约摘要错误、自我核验。路径越界、大小写冲突与 hardlink 由定向测试覆盖；没有声称 corpus 覆盖跨包重放。
- [x] 每个标签明确组别、fault_type、允许裁定集合、预期完整性门禁和接受标志；标签由夹具规则产生，文件名和报告均称为 synthetic oracle，不称人工金标准。
- [x] 断言：case ID 唯一、类别数符合规格、case/oracle 一一对应、无明文凭据、样本可离线加载。

### Task 4: 固定夹具运行与指标生成

**Files:**
- Create: experiments/evidence_adjudication/run_pilot.py
- Create: experiments/evidence_adjudication/metrics.py
- Create: experiments/evidence_adjudication/tests/test_metrics.py

- [x] 对 120 个样本运行确定性校验器；verifier 输入仅为单个 packet、artifact bytes、冻结 schemas 与预期 hash，oracle 仅由后续 metrics 阶段载入；逐例输出裁定及标准化根因。
- [x] 按机器可读 `allowed_verdicts` 逐例计算裁定与 accepted 标志一致性。
- [x] 计算材料完整度（112/120）、完整性门禁通过数（80/120）、结构缺陷误接受（按 `row.ok`，0/40）、有效样本完整性误拒（0/40）及 fault_type × verifier reason 归因表；语义表现仍未测。
- [x] 报告描述性比例的分子/分母；120 条是固定合成语料完整普查，不计算抽样置信区间，不外推总体可靠性。
- [x] semantic false accept / false reject、双 reviewer 共同漏检、错误相关性、人工复核时间和业务成本字段输出 null 并标注 not_measured；不能填 0。
- [x] 本机运行：`run_pilot.py --cases ... --oracle ... --out experiments/evidence_adjudication/results/20261124-final`，退出码 0。
- [x] 输出 JSON summary、逐例结果、按类别分层的分子/分母/比例和失败原因。相同输入重跑后 `summary.json` 与 `results.json` SHA-256 一致。

### Task 5: 冻结运行并写明证据边界

**Files:**
- Create: experiments/evidence_adjudication/results/20261124-final/（本地产物，不纳入 GitHub 文档发布提交）
- Create: docs/experiments/evidence-adjudication-pilot-report.md

- [x] 冻结 verifier/loader/schema 源 hash、规格与夹具 hash、规则版本、规范化器 hash 和运行参数；代码未提交到公开仓库，报告未编造 commit。
- [x] 报告完整性门禁与合成裁定标签一致性分开列项；记录分子、分母、比例和失败原因。
- [x] 报告明确区分该固定 synthetic oracle 试验、尚未测得的真实 agent 语义误差、独立性、人工时间和市场需求。
- [x] 做字节损坏拒绝与包/manifest 同步改写测试；前者只声明摘要不匹配可检测，后者实证说明本地管理员能重写完整证据包，绝不称防篡改。
- [x] 本机断言：固定语料每类错误有可定位拒绝原因；输出未测指标为 null；无语义正确率或真实任务 PASS 声明。

### Task 6: 独立核验与记忆交接

**Files:**
- Review only: experiment source, fixtures, frozen report manifest
- TianShu evidence: UAI Hub terminal task result pointer and returned report; no repository write required

- [x] DSH 三轮只读评阅与作者修订逐项归档于项目计划书；第三轮无阻断。不将 DSH 文档评阅称为独立代码验收。
- [x] 天枢按当时冻结版本派发核验；任务卡包含绝对工作目录、读取白名单、禁止写入范围、精确命令、预期断言与机器可读回执路径。后续修订了实现，旧版回执不适用于当前版本。
- [x] 天枢审批门阻止 runner 输出写入后记录真实失败并保留待核验状态；未绕过，不由 Codex 自检代替。
- [x] 天枢受审批沙箱阻挡后记录失败和原始输出路径/哈希，不绕过、不重派；其完成当时版本单测但没有 runner 产物哈希，故不视为完整核验。输出：`C:\Users\33389\.shared\comms-v2\cutover-base-20261109\runtime\interfaces\tianshu\results\25b0a037-6a4b-47a7-b1c6-8fe5df532dc2\runtime.stdout.jsonl` SHA256 `6f57308a6493ba37372b6939bdda86497a5582a5f5a1a97cb5deaa63e5796869`。
- [ ] Go 判据：阶段 1/2 所有硬门通过；天枢回执完整且可复现（精确命令、退出码、输入/输出 hash、复现说明）；未关闭阻断为零；未测指标显式保留 null/not_measured。天枢受阻或回执缺项时只能记 pending/no-go，不得提交 go 建议。
- [ ] 最终交接前运行 python stage2/uai_memory.py audit；用户决定、里程碑、失败按团队记忆协议记录。

## 交付门槛

- 计划与架构文档评阅完成并上传到 feature branch；GitHub 变更只包含这四份已授权文档。
- 定向测试退出码为 0；固定夹具结果可复算；语义错误率、相关性、人工时间和市场指标明确标为未测。
- TianShu 独立核验与 Codex 实施自检分开陈述。
- 不做 merge、deployment、客户联络或商业承诺。
