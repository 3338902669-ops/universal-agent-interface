# AGENTS.md — UAI 仓库工作规则

> 适用范围：`universal-agent-interface` 仓库内的所有 agent（DSH / Codex / 天枢 / 其他）。
> 与其他文件冲突时：用户最新指令 > 本文件 > 仓库文档。

## 一、开工前必读

1. `C:\Users\33389\.shared\UAI-FRAMEWORK-SNAPSHOT.md`（现状一页看完）
2. `C:\Users\33389\.shared\CURRENT-TASK.md`（在途任务与负责人）
3. `docs/UAI-MEMORY-PROTOCOL.md`（团队记忆协议）

## 二、团队记忆（强制）

- 接手前：`python stage2/uai_memory.py recall "<关键词>"`；`invalid_at` 非空的事实不能当依据。
- 用户确认的决定、已验证里程碑、失败/阻塞：`python stage2/uai_memory.py remember --kind ...`，E1 必须带命令与退出码。
- 交接前：`python stage2/uai_memory.py audit`，把「有结论未入图」清零或写明原因。
- 禁止绕 outbox 直连 Neo4j 写记忆；禁止把凭据写进任何记忆。

解释器：`C:\Users\33389\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`

## 三、单写入者与核验

- 同一文件同一时间只有一个主写入者；其余只读复核或写隔离脚本。
- **核验者 ≠ 实现者**；实现者自检只报「实施完成，待核验」。
- 连续三次 FAIL 冻结上报，不做第四轮硬试。

## 四、提交与发布

- 提交前必须：目标测试全绿 + `git -c core.whitespace=cr-at-eol diff --check` 通过。
- 声称「通过 / 修好 / 可用」必须附本轮命令与退出码（E1）；没有新鲜证据就写「未验证」。
- 提交/推送属于对外可见动作，需用户明确授权。

## 五、这批文件别乱动

- `prototype/hub.py`、`prototype/interface.py`：控制平面承重件，改动必须带回归。
- `stage2/memory_*`：记忆管道；改动必须跑 `test_memory_*` 全套。
- `stage2/outcome_recorder.py`：已弃用，只剩 `graph_writes_enabled()` 被 hub 引用，不要新增调用。
