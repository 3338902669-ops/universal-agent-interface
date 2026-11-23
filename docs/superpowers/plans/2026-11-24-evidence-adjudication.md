# UAI 证据约束裁定试点实施计划

> **For agentic workers:** 按用户已选协作方式执行：Codex 为唯一实现写入者，DSH 只读评阅，天枢在实现后独立核验。每一步先满足断言，再进入下一步。

**Goal:** 构建并评测一个隔离、可离线复算的代码交付证据裁定原型，验证外层包绑定、结构错误拒绝、离线复算和合成根因归类。语义错误率、人工复核时间、成本、错误相关性与市场需求不在首轮量化。

**Architecture:** 外层试验包通过固定相对 `$ref` 复用现有 Evidence Record v1；复算单元定义为冻结仓库规则（固定 commit）+ bundle。bundle 内包含契约快照、产物字节、manifest、声明、观察记录与裁定；不声称单独 bundle 自包含所有规则源文件。实现与夹具均放在 experiments/evidence_adjudication/，不改生产 Hub。

**Tech Stack:** Python 3 标准库；JSON Schema v2020-12；Python 标准库 unittest；Mermaid 图。

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

- [ ] 记录公开基线 commit 和当前工作树已有改动；不改也不暂存既有变更。
- [ ] 固定外层 packet profile 使用 evidence_records 数组，数组项以 `../../protocol/unified/evidence-record.schema.json` 相对 `$ref` 引用既有 v1 schema；以 schema 文件 URI 为解析基准、空 registry、禁网络解析，并测试从仓库根离线解析成功；不得复制另一份 v1 定义。
- [ ] 固定 contract_snapshot 必含验收条件、禁止项、工具范围、contract ID/version/hash；冻结 bundle 必含全部产物字节和 manifest。
- [ ] 将 execution_attestation 标记为本轮 out of scope；无受信任执行证明不得声称命令真实运行已被证明。
- [ ] 明确只复用现有裁定词汇，不调用 stage2/three_layer_verdict.py 的 execute_all 或 shell 检查。
- [ ] 断言：所有契约缺口关闭后才可通过阶段 0；Evidence Record v1 的既有字段含义不被改变。记录批准的 profile 和输入文件 hash。

### Task 2: 建立自包含证据包与纯函数校验器

**Files:**
- Create: experiments/evidence_adjudication/evidence_packet.schema.json
- Create: experiments/evidence_adjudication/verifier.py
- Create: experiments/evidence_adjudication/tests/test_verifier.py

- [ ] 外层 schema 的 evidence_records 项使用相对 `$ref` 指向既有 Evidence Record v1；增加完整 contract_snapshot、artifact_manifest、reviewer_record 和 adjudication，不更改 v1。binding 表规定：task_id 在 packet、contract_snapshot 与每条 v1 record 一致；run_id/contract_id 只位于 packet/claim/verifier 层；每条 record.payload.uri 必须等于 manifest 中对应的 bundle-relative artifact path，payload sha256/bytes 必须与 manifest 和字节一致；revision 按 v1 record 与 execution_claim 比较，缺字段为 unknown，不伪造 v1 字段。
- [ ] 分为 `packet_loader`（只读文件系统、路径规范化、拒绝符号链接/junction/硬链接、bundle 根外文件）与纯函数 `verifier`（只接收已加载 JSON 与字节）。路径只接受 NFC 规范化 POSIX 相对路径，拒绝反斜杠、盘符、UNC、空段、`.`/`..`、URL、重复路径和非普通文件。
- [ ] `bundle.as_of`、`contract_snapshot.evidence_max_age_seconds` 与每条 evidence `asserted_at` 均使用 RFC3339 UTC；以 `as_of` 计算时效，过期按证据不足→未核，不自动判实质失败。
- [ ] execution_attestation 只作为保留字段说明，不执行验证、不提升可信等级；缺少 attestation 时结果明确声明这一边界。
- [ ] `verifier` 只用纯函数检查 JSON、哈希、字段交叉绑定和裁定映射；不执行 shell 命令。canonicalization 固定为试验内的 `UAI-CJSON-v1`：UTF-8 JSON、对象键按 Unicode code point 排序、无空白分隔符、非 ASCII 原样输出、仅允许整数/布尔/null/字符串/数组/对象（拒绝浮点与重复键）、所有字符串必须 NFC；manifest 按 path 排序。packet digest 覆盖 `contract_snapshot, evidence_records, artifact_manifest, execution_claim, runtime_observation, reviewer_record, adjudication_input, rules_version`；排除 `verifier_record` 与最终 event 自身，通过 `prev_digest` 链接后续记录，避免自引用。规范化器版本/hash进入冻结 manifest。此 profile 不是 RFC 8785 实现。
- [ ] 身份 ID 不同时只输出 identity_separated；盲审、只读隔离、认证状态、模型/工具/提示共享信息分别记录，不折算成独立布尔值。
- [ ] 裁定语义：逐例仅当输出 verdict 属于 oracle `allowed_verdicts` 才算裁定正确；有效样本允许集合为 `{通过}`，声明已不通过样本 `{不通过}`，证据缺陷 `{未核, 不通过}`。声明不通过组仅检查裁定标记一致性，不代表真实实质缺陷识别；首轮不包含真实语义判定。有效样本输出未核/作废均计误拒；只有夹具标签标记为作废才可从分母排除，校验器输出作废计错误；未核/作废永不计通过。oracle 只在指标阶段读取，verifier 进程只接收 packet，并用独立进程/接口测试防标签泄漏。
- [ ] 定向测试覆盖 schema `$ref` 空 registry 离线解析、包内字节读取、错任务/错修订/哈希不符、重算摘要的伪造 manifest、路径穿越/UNC/反斜杠/盘符/符号链接/junction/硬链接/重复大小写路径、过期时间、缺失产物、重放冲突、执行者自裁、未核/作废非通过、JCS 键序空白稳定性、重复键拒绝与字段替换摘要变化。
- [ ] 运行：python -m unittest discover -s experiments/evidence_adjudication/tests -p "test_*.py" -v
- [ ] 预期：所有列出的类别都有独立断言并通过；运行结果只说明该固定测试集，不声称真实环境防篡改保证。

### Task 3: 构造合成 oracle 夹具

**Files:**
- Create: experiments/evidence_adjudication/fixtures/cases.jsonl
- Create: experiments/evidence_adjudication/fixtures/oracle-labels.jsonl
- Create: experiments/evidence_adjudication/generate_cases.py

- [ ] 生成 120 个合成夹具：40 个有效包、40 个已声明不通过包（仅测裁定标记，不代表真实语义缺陷识别）、40 个证据缺陷包；各类分别报告分子和分母。
- [ ] 覆盖正确、错误、缺失、过期、错任务、错修订、内容摘要不匹配、伪造 manifest、路径越界、重复/冲突重放和仅有执行者声明。
- [ ] 每个标签明确约束要求、严重度、允许裁定集合、预期错误类别；标签由夹具规则产生，文件名和报告均称为 synthetic oracle，不称人工金标准。
- [ ] 断言：case ID 唯一、类别数符合规格、case/oracle 一一对应、无明文凭据、样本可离线加载。

### Task 4: 固定夹具运行与指标生成

**Files:**
- Create: experiments/evidence_adjudication/run_pilot.py
- Create: experiments/evidence_adjudication/metrics.py
- Create: experiments/evidence_adjudication/tests/test_metrics.py

- [ ] 对 120 个样本运行确定性校验器；verifier 输入仅为单个 packet，oracle 仅由独立 metrics 阶段载入；逐例输出裁定及标准化根因。
- [ ] 按 Task 2 的 `allowed_verdicts` 规则逐例计算正确性；仅 oracle 标记作废样本排除分母，verifier 输出作废计错。
- [ ] 计算包完整度、结构错误误接受数、有效包误拒数、逐类根因混淆矩阵和字段级 canonical 复算一致性。
- [ ] 对所有比例报告分子、分母和双侧 95% Wilson score 区间；固定测试集指标不得外推成总体可靠性。
- [ ] semantic false accept / false reject、双 reviewer 共同漏检、错误相关性、人工复核时间和业务成本字段输出 null 并标注 not_measured；不能填 0。
- [ ] 运行：python experiments/evidence_adjudication/run_pilot.py --cases experiments/evidence_adjudication/fixtures/cases.jsonl --oracle experiments/evidence_adjudication/fixtures/oracle-labels.jsonl --out experiments/evidence_adjudication/results/20261124-initial
- [ ] 预期：退出码 0；输出 JSON summary、逐例结果、按类别分层的分子/分母/区间和失败清单。相同输入重跑能重建同一 canonical 结论。

### Task 5: 冻结运行并写明证据边界

**Files:**
- Create: experiments/evidence_adjudication/results/20261124-initial/（本地产物，不纳入发布提交）
- Create: docs/experiments/evidence-adjudication-pilot-report.md

- [ ] 先冻结代码 commit、夹具 hash、规则版本、schema/规范化器 hash 和运行参数；运行后不得改 oracle。
- [ ] 报告结构校验与逻辑裁定分开列项；记录每类分子、分母、区间和失败样本。
- [ ] 报告明确区分该固定 synthetic oracle 试验、尚未测得的真实 agent 语义误差、独立性、人工时间和市场需求。
- [ ] 做包损坏与包/manifest 同时重算摘要两类测试；前者只声明意外损坏检测，后者说明本地管理员威胁边界，不称防篡改。
- [ ] 断言：相同冻结输入可离线复算相同字段级输出；所有错误类型有可定位的拒绝原因；无无证据 PASS 声明。

### Task 6: 独立核验与记忆交接

**Files:**
- Review only: experiment source, fixtures, frozen report manifest
- TianShu evidence: UAI Hub terminal task result pointer and returned report; no repository write required

- [ ] DSH 的文档只读评阅与作者改动清单归档；不把 DSH 评阅称为独立验收。
- [ ] 天枢仅在实现冻结后核验；派单包含绝对工作目录、允许读取白名单、禁止写入范围、精确命令、预期断言和机器可读回执路径。
- [ ] 若天枢审批门/沙箱/工具限制阻止读取或运行，记录真实失败并保留待核验状态；不得绕过，不由 Codex 自检代替。
- [ ] TianShu 回执要含命令、退出码、输入与输出 hash、复现说明及核验边界；报告由天枢席位写入其允许的位置。
- [ ] Go 判据：阶段 1/2 所有硬门通过；天枢回执完整且可复现（精确命令、退出码、输入/输出 hash、复现说明）；未关闭阻断为零；未测指标显式保留 null/not_measured。天枢受阻或回执缺项时只能记 pending/no-go，不得提交 go 建议。
- [ ] 完成前运行 python stage2/uai_memory.py audit；用户决定、里程碑、失败按团队记忆协议记录。

## 交付门槛

- 计划与架构文档评阅完成并上传到 feature branch；GitHub 变更只包含这四份已授权文档。
- 定向测试退出码为 0；固定夹具结果可复算；语义错误率、相关性、人工时间和市场指标明确标为未测。
- TianShu 独立核验与 Codex 实施自检分开陈述。
- 不做 merge、deployment、客户联络或商业承诺。
