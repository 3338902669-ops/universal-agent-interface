# 案例：用四件套审我自己的修复（2026-11-18 03:48–04:05）

> 这是一次**真活**，不是演示：把今晚修的五处「假在线」交给 codex 审、天枢验、小羽裁定。
> 全程走 hub 账本，每一轮都有 task_id、sha256、状态机记录。

## 结果：**通过**（第五轮）

```
① 五审 codex  PASS（带行号核验） 42s
② 实测验 tianshu  35 passed in 3.96s
③ 终裁 小羽  通过：引用三处核验点 + 35 passed，可以结案 17s
```

## 五轮记录（这才是价值所在）

| 轮 | 谁 | 结论 | 抓到什么 | 性质 |
|---|---|---|---|---|
| R1 | codex | **FAIL** | ①`claim` 仍调全局 `force_flush_outbox`，按 task 收窄没生效；②丢弃列表写的是 `fencing_rejected`，hub 真实返回 `fencing_stale`；③`on_reconcile` 仍物化 `range(1,highest+1)` | **代码缺陷** |
| R1 | tianshu | 34 passed | 实测验本身通过 | — |
| R1 | 小羽 | **不通过** | 审查 FAIL 不能结案 | 正确拒绝 |
| R2 | codex | **FAIL** | ①`claim` 的根本没改成（`_flush_one_event` 无调用点）；②`missing_count` 把未扫描序号算成缺失，高估 | **代码缺陷** |
| R3 | codex | **FAIL** | 工作目录里没有 `prototype/interface.py`（我给的是相对路径） | **派单契约缺陷** |
| R4 | codex | **FAIL** | 指令里的绝对路径没收到 —— 因为**指令含换行，CLI 只取第一行** | **派单契约缺陷** |
| R5 | codex | **PASS** | 单行 + 绝对路径 → 逐条核验通过 | — |
| R5 | 小羽 | **通过** | 引用三处核验点 + 35 passed，同意结案 | 正确结案 |

**统计：6 轮里 3 次拦下的是我的派单格式缺陷（相对路径 / 换行截断 / 无契约），2 次是真实代码缺陷。**

## 由此得出的两条硬结论

1. **协作环不是橡皮章**：它两次拦下我自己的代码缺陷，其中 `fencing_stale` 这一条是**字符串写错**级别的问题，
   靠人眼极容易漏（我确实漏了）；三次拦下派单格式问题。
2. **派单必须带任务契约**，否则同一个任务会因格式问题空转：
   - 路径一律绝对路径（席位工作目录是 `.uai-work/<task_id>`，不是仓库根）
   - **指令必须单行**（dsh/codex/rivet CLI 只把首段当 prompt）
   - 时限、范围（只审哪两个文件）、禁止项（不许读技能库/跑全仓扫描/改文件）
   - 回执格式（第一行 PASS/FAIL + ≤3 条风险，每条指到行）

## 期间修掉的代码（本轮新增）

| 文件 | 改动 | 触发 |
|---|---|---|
| `interface.py` | 新增 `_flush_one_event`；`claim` 改为逐条冲本任务前 20 条，不再调全局 flush | R1/R2 |
| `interface.py` | 丢弃原因补 `fencing_stale`（hub 的真实返回词） | R1 |
| `hub.py` | `on_reconcile` 有界扫描（上限 10000），未扫描部分用 `truncated`/`unscanned` 标注，不再计入 `missing_count` | R1/R3 |
| `tests/test_creds.py` | 新增回归：丢弃原因必须含 hub 真实词 | R1 |

pytest：**35 passed**。

## 交付物

- 本文件（案例记录）
- 账本：6 条以上 TASK_STATE / VERDICT 记录，含两个不同 operator（codex/tianshu/xiaoyu）
- 各轮回执原文：`<base>/runtime/interfaces/{codex,tianshu,xiaoyu}/results/<task_id>/result.json`
