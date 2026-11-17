# 四件套首跑记录（2026-11-18 02:33–02:45）

## 第一轮：对账环抓住了假成功（本轮最有价值的结果）

| 环 | 席位 | 状态 | 内容 |
|---|---|---|---|
| ① 审代码 | codex | TIMED_OUT（306s） | 在读 security 技能做深审，超 300s 上限，无结论 |
| ② 实测验 | tianshu | SUCCEEDED | 输出 Python was not found / App execution aliases —— 测试根本没跑 |
| ③ 回读对账 | xiaoyu（小羽） | SUCCEEDED | **不通过**：审代码超时无结论、实测验是 Python 未找到，两方都无有效证据 |

结论：协作链路的第一次真实价值不是「能派单」，而是**能拒绝假成功**。
天枢席位把命令失败当成功回报（SUCCEEDED + 错误文本），被对账环抓住。

## 第一轮暴露的两个真问题

1. 席位环境里 `python` 命中 Windows App Execution Alias 存根 → 命令没执行，但席位报 SUCCEEDED。
   修法：实测验给绝对解释器路径；更彻底的是席位启动时补齐 PATH（待做）。
2. 审代码 300s 不够：codex 会自己读技能库做安全审查，需 600s 或压窄范围。

## 第二轮（修完重跑）

| 环 | 席位 | 结果 |
|---|---|---|
| ② 实测验 | tianshu（--work-root + 绝对 python） | **32 passed in 3.63s**（input 55848 / output 213，真跑） |
| ① 审代码 | codex（600s，范围压到两个文件） | 仍在跑 |
| ③ 对账 | xiaoyu | 待 ① 出结论后再判 |

## 这一轮修掉的东西

- interface.py 新增 `--work-root`：任务目录放到仓库内，受限 runtime 才能访问仓库
- 踩坑：先加 argparse 参数但没改 __init__ 签名 → 四席全部崩溃（unexpected keyword argument），已修并复核 4 席在位
- seats.yml 给 seat-tianshu 加 `--work-root <repo>`
