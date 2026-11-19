# Push Notification 规格 v1（2026-11-15，方案 A：HTTP Webhook）

## 1. 定位

A2A 官方 pushNotificationConfig 语义的最小实现：客户端提交回调 URL，任务状态/裁定变化时 POST 通知。
**不触碰 event_bridge v1/v2 的 experimental_test_only 门禁**——本规格的投递管线独立读账本增量，不经过 v2 路由。

## 2. 注册

- `message/send` 的 params 新增可选 `pushNotificationConfig`：{"url": "http(s)://...", "token": 可选共享密钥}
- url 必须 http/https；token 用于回调签名头（见 §4）
- 注册与任务绑定：同一 task 的回调配置随受理入账本（TASK_ACCEPTED 的 envelope.params.uai_push 或独立 PUSH_CONFIG 记录）

## 3. 触发

- 只推**终态相关**事件：TASK_STATE 到达 SUCCEEDED/FAILED/TIMED_OUT/CANCELLED，及 VERDICT 记录
- 心跳/progress 不推（与投影红线一致：存活信号不是进展）
- 同一终态多次写入（重判）：每条都推，带 `uai/seq` 递增，消费方按 (task_id, seq) 去重

## 4. 回调签名（防伪造出站）

- 请求头：X-UAI-Task-Id、X-UAI-Seq、X-UAI-Timestamp、X-UAI-Signature
- 签名 = HMAC-SHA256(token, timestamp + "." + body)；无 token 则不带签名头（明文通道，由 URL 保密性兜底，如实降级）
- timestamp 偏差 >300s 的请求消费方可拒（防重放）

## 5. 投递与重试

- at-least-once：POST 失败（连接错误/非 2xx）指数退避重试 3 次（2s/8s/32s）
- 3 次失败后写入死信 `data/push-dead-letters.jsonl`，不丢不堵
- 投递尝试全部记入 `data/push-log.jsonl`（task_id, seq, url, status, attempt, ts）——出站方向也要可追责

## 6. 不变量

- P-1：不重复执行（投递幂等由消费方去重，投递器保证 seq 单调）
- P-2：死信不丢（失败必落盘）
- P-3：不阻塞主链路（投递失败不影响 hub 任务状态）
- P-4：无 token 不签、有 token 必签（fail-closed）

## 7. 验收矩阵

| # | 场景 | 断言 |
|---|---|---|
| A | 注册了 push 的任务到终态 | 回调收到 POST，签名校验通过，body 含 task_id/seq/state |
| B | 无 push 注册的任务 | 零回调 |
| C | 回调端 500 | 重试 3 次后死信落盘，主链路不受影响 |
| D | 带 token 的签名 | 用错 token 验签失败 |
| E | 非终态事件 | 不推（心跳/progress 零回调） |
