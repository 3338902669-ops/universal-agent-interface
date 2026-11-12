# bench：可复现验证包

对应待完善项 **E-01**（测试电池公开）· **E-03**（四档基线对照）。

本目录把两类验证放在一起，并**明确区分它们证明什么、不证明什么**：

| 目录/文件 | 类型 | 证明什么 | 不证明什么 |
|---|---|---|---|
| `run_benchmark.py` + `sim.py` | **仿真** | 每个防护组件抑制哪一类故障 | 真实吞吐、延迟、资源占用 |
| `../prototype/test_acceptance.py` | **系统级验收** | 十项契约行为在真进程 + 真 broker 上成立 | 规模与性能 |
| `test_battery_design.md` | **方法学** | 能力测试怎么设计才不被"通道故障"污染 | 无 |

---

## 1 四档基线对照（E-03）

```powershell
cd bench
python run_benchmark.py --self-check --out RESULTS.json
```

四档：

| 档 | 加了什么 | 目标故障 |
|---|---|---|
| T0 | 无防护 | — |
| T1 | 幂等键 + 重投 | 消息丢失 |
| T2 | 租约 / 尝试号 / 围栏 | 卡死、旧围栏污染 |
| T3 | 证据等级 + 六值裁定 | 谎报完成 |

指标：`lost`（丢失）· `duplicate_exec`（重复执行）· `wrong_accepted`（错误被当成功收下）· `stuck`（无终态）· `ops_per_task`（开销代理）。

实测（tasks=2000，seed=20261112）：

```text
tier   tasks   lost  duplicate  wrong  stuck     ok  ops/task
T0      2000    182        187    296    237   1285     2.812
T1      2000      0        169    326     89   1416     3.176
T2      2000      0        169    189      0   1642     3.442
T3      2000      0          0      0      0   2000     3.536
```

### 一个反直觉但重要的结果

**T0 → T1，`wrong_accepted` 从 296 升到 326。**

原因：幂等重投救回了本会丢失的 182 个任务，**暴露面变大**；而幂等对"谎报完成"毫无作用，所以错误收口的**绝对数**上升。

结论：**幂等只治丢失，不治伪证。** 这两类问题必须分开解决——这正是"证据 + 独立裁定"存在的理由，也是本基准最有价值的一条输出。

代价项：`ops_per_task` 从 2.812 升到 3.536（+25.8%）。防护不是免费的，这个数字应当随任何新机制一起报。

## 2 系统级十项验收（E-01）

```powershell
# 需要 nats-server 与 python 依赖，详见 ../stage2/README.md
$env:NATS_SERVER = "<path>\nats-server.exe"
$env:AGENT_RUNTIME_EXECUTABLE = "<path>\codex.exe"
cd ../prototype
python test_acceptance.py --base . --nats-url nats://127.0.0.1:43321
```

十项：登记与 stale / 接入检查防冒名 / 信封原样传达 / 断网 5 分钟补投 / 重复投递只执行一次 / 租约回收与围栏 / 资源串行与跨资源并发 / 超时分层 / 裁定写入 / 真 runtime 全链。

**证据等级**：套件自报 `E3-自证，未独立验证`。要升 E2，必须由未参与实现的一方复跑。

## 3 能力测试电池设计（五席方法学）

来自 2026-11-01 的实际运行，这里是可复现的规格：

1. **统一题目**：六道机械判分题（有唯一正确答案，可程序判分）。
2. **判分脚本派单前冻结**：先冻结评分器，再让被测者跑，防止事后调尺。
3. **同题逐字一致**：所有席位收到完全相同的题干文本。
4. **仅工作目录按席替换**：除工作目录外，环境完全一致。
5. **隔离会话**：云端席使用全新隔离会话，避免上下文污染。
6. **超时分档**：命令行席 1200 秒，运营席 1800 秒（能力差异导致的合理差异）。
7. **通道失败与能力失败分开统计**：不能把"通道没通"算成"能力不行"。

第 6、7 条是这套设计里最关键的两条——绝大多数多智能体评测失败在这里。

## 4 如何复现全部

```powershell
# 1) 协议一致性
cd ../protocol/tests/conformance && python run_conformance.py

# 2) Stage 2 自检（含统一模型与规格一致性）
cd ../../stage2
python test_projection.py; python test_a2a_server.py; python test_io.py
python test_bridge_joins.py; python test_unified.py; python test_protocol_specs.py
python check-cross-repo-vocab.py

# 3) 四档基准
cd ../bench && python run_benchmark.py --self-check
```

CI：`.github/workflows/conformance.yml` 跑 1)、2)、3)。

## 5 受控对照实验包（E-02）

`controlled_experiment/` 是「有无 skill 编排」这件事的完整可复现包，与本目录的四档基线
（同一份代码在不同防护档位下的**模拟**对比）不是一回事：四档基线比的是防护组件，
受控实验比的是同一个模型在有无编排环境下的**能力差异**。

```bash
cd controlled_experiment
python manifest.py --out MANIFEST.json      # 冻结题目/判分器/运行器/解释器
python test_controlled.py                   # 自检 26/26
python run.py --seat X --arm with_skill --cmd "<模板>" --out raw/run-with
python analyze.py --compare raw/run-with raw/run-without
python manifest.py --verify MANIFEST.json   # 不一致即 rc=2，拒跑
```

**当前状态：两档真实数据未运行。** 2026-10-26 跑过一次同类对照，但 `raw/` 未留存、
也没有随结果提交的 sha256，因此本包**不引用**那次结论。详见
`controlled_experiment/README.md` §4。

## 6 独立复跑门（E-04）

`ci_suites.py` 把 19 个套件跑成一份**语义清单**（只留 PASS/FAIL 判据名 + 退出码，
剔除 pid/耗时/临时路径），`compare_reruns.py` 判定两份清单是否逐套件等价。
`.github/workflows/independent-verification.yml` 用两台独立 runner 各跑一次再比对。

```bash
python bench/ci_suites.py --out rerun-A.json
python bench/ci_suites.py --out rerun-B.json
python bench/compare_reruns.py rerun-A.json rerun-B.json   # divergent 即非 0
python bench/test_rerun_compare.py                          # 比对器自检 7/7
```

边界（写在 workflow 里也写在这里）：同库同测在两台干净 runner 上复跑属于
**体系内互检（E2）**，不等于外部第三方独立验证。缺 lab 或运行时的套件记为
`skipped` 并写明原因（`requires_lab_nats_and_hub` / `requires_real_runtime_binary`），
不伪装成 PASS，也不当成用例失败。

