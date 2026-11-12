# GAR 侧未闭合项（C-06 ~ C-10）—— UAI 的引用与验收口径

本文件不是实现，是**边界声明 + 可验收口径**。理由很简单：这五项的状态变更发生在
`governed-agent-runtime`（GAR）进程内，UAI 仓改不到；但 UAI 声明了与它们相关的语义
（审批、证据、信任），所以必须把「我引用了哪一条、什么条件下算闭合」写死在 UAI 侧，
否则跨仓协作里没人知道这条缺口是否还在。

对照基线：`JOINT-ARCHITECTURE-AUDIT.md` 的 G1–G7、`UNIFIED-MODEL-v0.1.md`、
GAR 的 `INVARIANTS.md`（I11–I18）与 `KNOWN-FINDINGS.md`（F-GAR-01..10）。

---

## C-06 执行证据只能证明「有人这样断言」

| 项 | 内容 |
|---|---|
| 现状 | GAR 把 E6「命令真跑过」列为 OUT OF SCOPE；UAI 的证据记录（`protocol/unified/evidence-record.schema.json`）能约束**谁声明、声明了什么、载荷是否对得上**，不能证明命令真的执行过 |
| 为什么 UAI 修不了 | 要证明「跑过」，必须由**执行环境**产出不可伪造的执行记录（进程侧签名、内核审计、或受控 CI runner 的 attestation）。UAI 是可跨厂商的接口层，不拥有执行环境 |
| UAI 侧已做 | 证据等级 E1 要求 `command/exit_code/revision` 三件套；`NOT-GUARANTEED.md` 明写「命令真跑过」不在保证范围 |
| 闭合条件（外部） | 执行环境提供可验证执行记录，且 `evidence-record` 增加 `execution_attestation` 指针；在此之前任何「证据已充分」的表述都只能到 E2 |
| 引用位置 | `protocol/NOT-GUARANTEED.md` §5、`protocol/unified/evidence-record.schema.json` |

## C-07 信任根在门禁之外

| 项 | 内容 |
|---|---|
| 现状 | F-GAR-04：唯一的信任根初始化发生在 gate 之外，是唯一未受控的状态变更 |
| 为什么 UAI 修不了 | 信任根初始化是 GAR 的 bootstrap 流程，UAI 只是它的消费者（`identity` 的 `authenticated` 等级取决于它） |
| UAI 侧已做 | `stage2/identity.py` 规定「无验证器不得升级 authenticated」，把「没根就别装」变成硬约束 |
| 闭合条件（外部） | 双人 bootstrap + 初始化事件进只追加账本，且 UAI 能以 `identity` 验证器形式读到该事件 |

## C-08 授权不是纯函数

| 项 | 内容 |
|---|---|
| 现状 | F-GAR-07：`authorize()` 内有副作用，同一输入不保证同一输出，门禁不可预测 |
| 为什么 UAI 修不了 | 这是 GAR 内部函数边界问题 |
| UAI 侧已做 | 契约 §7 只把 `approval` 当**一次性凭据**消费，不承担授权逻辑；`unified.py` 的审批校验在 UAI 侧是纯函数（`validate_approval`） |
| 闭合条件（外部） | GAR 把副作用移出 `authorize()`，并提供相同输入的幂等证明（同一请求两次授权结果字节相同） |

## C-09 grant TTL 用可注入时钟

| 项 | 内容 |
|---|---|
| 现状 | F-GAR-06：TTL 判定依赖可注入时钟，生产环境需单调源 |
| 为什么 UAI 修不了 | 时钟源在 GAR |
| UAI 侧已做 | 契约 §13.4 规定 SLA 的 `deadline_at` 用**绝对时刻**、`timeout_s` 用**相对秒**，两者不混用；`A2A-SPEC-MAPPING.md` D6 同口径 |
| 闭合条件（外部） | GAR 改用 `monotonic` 源，并给出「回拨时钟不延长授权」的测试用例 |

## C-10 `internals` 形状不一致

| 项 | 内容 |
|---|---|
| 现状 | F-GAR-10：特权面 `internals` 暴露形状不统一，易误用 |
| 为什么 UAI 修不了 | 属 GAR 模块导出面 |
| UAI 侧已做 | UAI 不引用 GAR 的 `internals`；跨仓只通过词表（`check-cross-repo-vocab.py`）与证据格式对齐 |
| 闭合条件（外部） | GAR 统一 facade，且删掉对内部字段的直接导出 |

---

## 这五项在 UAI 验收里的处理

1. **不得**因为外部项未闭合就把 UAI 自身的对应能力标成「已完成」：例如审批身份目前只是 `asserted`，
   必须一直写成 `asserted`，直到有真验证器（C-03/A-01 已在代码里 fail-closed）。
2. `check-cross-repo-vocab.py` 会读取 GAR 的 `KNOWN-FINDINGS.md`；这五项仍登记在册时，跨仓检查应报
   **在册**而不是漂移（已实现：F-GAR-08 一类断言的检查项）。
3. 任何引用 GAR 结论的文档必须同时写测试的**取证等级**，不能把「GAR 说已修」当 E1。

---

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-12 | 首次登记 C-06~C-10 的 UAI 侧引用与闭合口径（此前只在 ARCH-TODO-LIST 里有一行「属 GAR 仓实现问题」） |
