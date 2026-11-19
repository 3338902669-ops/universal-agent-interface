"""UAI 编排器（I-5）：把拆解 / 路由 / 协商 / 执行 / 三层验收串成一条链。

之前的缺口：smart_router、task_decomposer、negotiation、three_layer_verdict、
team_memory 各自有测试，但没有东西把它们串起来——系统不会「收一个大任务自己跑完」。

本模块补这一层：
    大任务
      → TaskDecomposer 拆成 DAG 子任务
      → 每个子任务 SmartRouter 选候选（按历史成功率自适应）
      → NegotiationLoop 逐个协商，直到有 agent 接受
      → inbound_bridge 投递 hub（自动注入团队记忆）
      → 轮询到终态
      → 三层验收 + 独立裁定（核验者≠实现者）
      → 结果回写路由器统计与图谱

用法（库）：
    from orchestrator import Orchestrator
    orc = Orchestrator(nats_url="nats://127.0.0.1:43321", router=router)
    report = asyncio.run(orc.run("做一个完整的用户管理系统"))

设计要点：
- submit / state / verdict 三个函数可注入，便于不依赖 NATS 的确定性测试
- 任何一步失败都不抛异常中断整条链，而是记录在该子任务的结果里
- 需要人工介入（路由低置信度 / 全部 agent 拒绝 / 不可路由子任务）显式上报
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from negotiation import AgentNegotiator, NegotiationResponse
from smart_router import AgentProfile, SmartRouter
from task_decomposer import TaskDecomposer

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}


class OrchestratorError(RuntimeError):
    pass


class Orchestrator:
    """把多个协作模块串成一条可执行链。"""

    def __init__(
        self,
        *,
        nats_url: str = "nats://127.0.0.1:43321",
        router: SmartRouter,
        decomposer: TaskDecomposer | None = None,
        negotiators: dict[str, AgentNegotiator] | None = None,
        verifier_id: str = "tianshu",
        contract_version: str = "1.0",
        cls: str = "readonly",
        poll_interval_s: float = 1.0,
        max_wait_s: float = 180.0,
        submit_retries: int = 3,
        submit_backoff_s: float = 1.0,
        submit_fn: Callable[..., Awaitable[dict]] | None = None,
        state_fn: Callable[..., Awaitable[dict]] | None = None,
        verdict_fn: Callable[..., Awaitable[dict]] | None = None,
    ):
        self.nats_url = nats_url
        self.router = router
        self.decomposer = decomposer or TaskDecomposer(
            available_skills=sorted({s for a in router.agents.values() for s in a.skills})
        )
        self.negotiators = negotiators or {}
        self.verifier_id = verifier_id
        self.contract_version = contract_version
        self.cls = cls
        self.poll_interval_s = poll_interval_s
        self.max_wait_s = max_wait_s
        self.submit_retries = max(1, int(submit_retries))
        self.submit_backoff_s = submit_backoff_s

        self._submit_fn = submit_fn
        self._state_fn = state_fn
        self._verdict_fn = verdict_fn

    # ---------- 默认实现（真实 NATS） ----------

    async def _default_submit(self, request: dict, created_by: str,
                              target_interface: str | None, timeout_s: int) -> dict:
        import inbound_bridge
        return await inbound_bridge.submit(
            nats_url=self.nats_url,
            request=request,
            created_by=created_by,
            target_interface=target_interface,
            timeout_s=timeout_s,
            cls=self.cls,
            contract_version=self.contract_version,
        )

    async def _default_state(self, task_id: str) -> dict:
        import inbound_bridge
        return await inbound_bridge.query_state(
            nats_url=self.nats_url, task_id=task_id, timeout_s=5.0
        )

    async def _default_verdict(self, payload: dict) -> dict:
        import json
        import inbound_bridge
        nc = await inbound_bridge._connect(self.nats_url)
        try:
            msg = await nc.request(
                "hub.verdict",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=10,
            )
            return json.loads(msg.data.decode("utf-8"))
        finally:
            await nc.close()

    # ---------- 协商 ----------

    def _pick_agent(self, task_desc: str, required_skills: list[str]) -> dict:
        """按候选顺序协商，返回第一个接受的 agent。"""
        route = self.router.route(task_desc)
        candidates = route.get("candidates") or []
        if not candidates:
            return {"agent": None, "reason": "no_candidates", "route": route}

        tried = []
        for cand in candidates:
            aid = cand["agent_id"]
            negotiator = self.negotiators.get(aid)
            if negotiator is None:
                tried.append({"agent": aid, "response": "no_negotiator"})
                continue
            resp = negotiator.evaluate(task_desc, route.get("task_type", "general"),
                                       required_skills=required_skills or None)
            tried.append({"agent": aid, "response": resp.response_type,
                          "reason": resp.reason})
            if resp.response_type == NegotiationResponse.ACCEPT:
                return {"agent": aid, "reason": "accepted", "route": route,
                        "negotiation": tried}
            if resp.response_type == NegotiationResponse.DELEGATE and resp.delegate_to:
                if resp.delegate_to in self.router.agents:
                    return {"agent": resp.delegate_to, "reason": "delegated",
                            "route": route, "negotiation": tried}

        # 没有协商器时退回路由首选，保持可用
        if all(t["response"] == "no_negotiator" for t in tried):
            return {"agent": candidates[0]["agent_id"], "reason": "routed_without_negotiator",
                    "route": route, "negotiation": tried}
        return {"agent": None, "reason": "all_rejected", "route": route,
                "negotiation": tried}

    # ---------- 单个子任务 ----------

    async def _run_subtask(self, sub: dict, parent_task: str, created_by: str) -> dict:
        desc = sub["description"]
        entry: dict[str, Any] = {
            "index": sub["index"],
            "description": desc,
            "type": sub["type"],
            "depends_on": sub.get("depends_on", []),
        }

        if not sub.get("routable", True):
            entry.update(status="escalated", reason="unroutable_no_agent_has_skill")
            return entry

        picked = self._pick_agent(desc, [sub["type"]])
        entry["negotiation"] = picked.get("negotiation", [])
        agent = picked.get("agent")
        if not agent:
            entry.update(status="escalated", reason=picked.get("reason"))
            entry["route"] = picked.get("route")
            return entry

        entry["agent"] = agent
        entry["route_confidence"] = (picked.get("route") or {}).get("confidence")

        request = {"message": {"messageId": "sub-%s-%d" % (parent_task, sub["index"]),
                               "parts": [{"text": desc}]}}
        started = time.time()
        submit_fn = self._submit_fn or self._default_submit
        attempts = 0
        last_error = ""
        task_id = None
        while attempts < self.submit_retries:
            attempts += 1
            try:
                submitted = await submit_fn(request, created_by, agent, 120)
                task_id = submitted["task_id"]
                break
            except Exception as exc:
                last_error = str(exc)[:200]
                if attempts < self.submit_retries:
                    await asyncio.sleep(self.submit_backoff_s)
        entry["submit_attempts"] = attempts
        if task_id is None:
            entry.update(status="submit_failed", error=last_error)
            return entry
        entry["task_id"] = task_id

        state_fn = self._state_fn or self._default_state
        state = "QUEUED"
        deadline = time.time() + self.max_wait_s
        while time.time() < deadline:
            await asyncio.sleep(self.poll_interval_s)
            try:
                st = await state_fn(task_id)
            except Exception as exc:
                entry.update(status="state_query_failed", error=str(exc)[:200])
                break
            state = st.get("state") or st.get("execution_state") or state
            if state in TERMINAL_STATES:
                break
        entry["state"] = state
        entry["elapsed_s"] = round(time.time() - started, 2)

        # 三层验收 + 独立裁定
        if state == "SUCCEEDED":
            verdict_payload = {
                "task_id": task_id,
                "verdict": "通过",
                "checker": self.verifier_id,
                "evidence_level": "E1",
                "enforce_three_layer": True,
            }
            verdict_fn = self._verdict_fn or self._default_verdict
            try:
                vr = await verdict_fn(verdict_payload)
                entry["verdict_ok"] = bool(vr.get("ok"))
                entry["verdict_reason"] = vr.get("reason")
                rec = vr.get("verdict_record") or {}
                entry["three_layer_enforced"] = rec.get("three_layer_enforced")
            except Exception as exc:
                entry["verdict_ok"] = False
                entry["verdict_reason"] = "verdict_error:%s" % str(exc)[:120]
            entry["status"] = "accepted" if entry.get("verdict_ok") else "verdict_rejected"
        else:
            entry["status"] = "not_succeeded"

        # 回写路由统计（自适应数据来源）
        try:
            self.router.record_result(agent, desc,
                                      entry["status"] == "accepted",
                                      entry.get("elapsed_s") or 0.0)
        except Exception:
            pass
        return entry

    # ---------- 全链路 ----------

    async def run(self, task_desc: str, created_by: str = "orchestrator") -> dict:
        """跑完一个大任务的完整链路，返回可读报告。"""
        plan = self.decomposer.decompose(task_desc)
        order = plan.get("dag_order") or list(range(len(plan["subtasks"])))
        subtasks = plan["subtasks"]

        run_id = "run-%d" % int(time.time() * 1000)
        started = time.time()
        results: list[dict] = []
        for idx in order:
            sub = subtasks[idx]
            # 依赖未通过则跳过下游
            blocked = [
                d for d in sub.get("depends_on", [])
                if any(r["index"] == d and r["status"] not in ("accepted",) for r in results)
            ]
            if blocked:
                results.append({
                    "index": idx, "description": sub["description"], "type": sub["type"],
                    "depends_on": sub.get("depends_on", []),
                    "status": "blocked", "reason": "upstream_not_accepted",
                    "blocked_by": blocked,
                })
                continue
            results.append(await self._run_subtask(sub, run_id, created_by))

        accepted = [r for r in results if r["status"] == "accepted"]
        escalated = [r for r in results if r["status"] == "escalated"]
        if len(accepted) == len(results):
            overall = "ok"
        elif accepted:
            overall = "partial"
        else:
            overall = "failed"

        return {
            "task": task_desc,
            "run_id": run_id,
            "template": plan.get("template_used"),
            "subtask_count": len(subtasks),
            "overall": overall,
            "elapsed_s": round(time.time() - started, 2),
            "accepted": len(accepted),
            "escalated": len(escalated),
            "needs_human": bool(escalated) or overall != "ok",
            "results": results,
        }
