"""智能路由器（2026-11-16）。

解决：多 agent 系统里，任务来了该给谁做？
不是人手动选——系统根据每个 agent 的能力档案 + 历史成功率 + 当前负载自动选择。
路由决策可解释（输出为什么选它）。

关键设计（区别于简单轮询/静态标签路由）：
1. 能力匹配用语义向量而非关键词（agent 擅长什么不是标签列表，是历史成功记录的统计特征）
2. 历史自适应：agent 做某类任务成功率高 → 这类任务后续自动派给它；失败率高 → 权重下降
3. 可解释路由：每次路由决策输出 top-3 候选 + 各自得分 + 为什么选第一个
4. 低置信度升级：当所有候选得分接近或都很低时，升级给人决定

用法：
    from smart_router import SmartRouter, AgentProfile
    router = SmartRouter()
    router.register(AgentProfile(id="codex", skills=["code","deploy","test"], ...))
    decision = router.route("修复登录页面 CSS 在 390px 下溢出的问题")
    print(decision.selected, decision.reason, decision.confidence)
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any


class AgentProfile:
    """一个 agent 的完整档案：静态能力 + 动态统计。"""

    def __init__(self, agent_id: str, skills: list[str],
                 description: str = "",
                 max_concurrent: int = 2,
                 cost_per_task: float = 1.0):
        self.agent_id = agent_id
        self.skills = set(s.lower() for s in skills)  # 静态能力标签
        self.description = description
        self.max_concurrent = max_concurrent
        self.cost_per_task = cost_per_task
        # 动态统计（自适应路由的数据基础）
        self.stats: dict[str, dict] = {}  # task_type -> {success, fail, avg_time_s}

    def record_result(self, task_type: str, success: bool, duration_s: float):
        st = self.stats.setdefault(task_type, {"success": 0, "fail": 0, "total_time": 0.0, "count": 0})
        if success:
            st["success"] += 1
        else:
            st["fail"] += 1
        st["total_time"] += duration_s
        st["count"] += 1

    def success_rate(self, task_type: str) -> float | None:
        """该 agent 在某类任务上的历史成功率。没做过 = None（无数据）。"""
        st = self.stats.get(task_type)
        if not st or (st["success"] + st["fail"]) == 0:
            return None
        return st["success"] / (st["success"] + st["fail"])

    def avg_duration(self, task_type: str) -> float | None:
        st = self.stats.get(task_type)
        if not st or st["count"] == 0:
            return None
        return st["total_time"] / st["count"]

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "skills": sorted(self.skills),
            "description": self.description,
            "max_concurrent": self.max_concurrent,
            "cost_per_task": self.cost_per_task,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentProfile":
        p = cls(d["agent_id"], d.get("skills", []), d.get("description", ""),
                d.get("max_concurrent", 2), d.get("cost_per_task", 1.0))
        p.stats = d.get("stats", {})
        return p


class SmartRouter:
    """智能路由器：任务描述 × agent 档案 → 排序决策（可解释）。"""

    def __init__(self, registry_path: Path | None = None):
        self.agents: dict[str, AgentProfile] = {}
        self.registry_path = Path(registry_path) if registry_path else None
        self.active_tasks: dict[str, int] = {}  # agent_id -> 当前运行中任务数
        if self.registry_path and self.registry_path.exists():
            self._load()

    def register(self, profile: AgentProfile):
        self.agents[profile.agent_id] = profile
        self.active_tasks.setdefault(profile.agent_id, 0)
        self._save()

    def _load(self):
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        for d in data.get("agents", []):
            p = AgentProfile.from_dict(d)
            self.agents[p.agent_id] = p
            self.active_tasks.setdefault(p.agent_id, 0)

    def _save(self):
        if not self.registry_path:
            return
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"agents": [p.to_dict() for p in self.agents.values()]}
        self.registry_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def load_stats_from_ledger(self, ledger_path: str, task_desc_map: dict = None):
        """从 hub 账本提取历史任务结果，填充 AgentProfile.stats（自适应学习数据源）。

        ledger_path: hub 账本 JSONL 路径
        task_desc_map: 可选的 task_id -> task_description 映射（如果账本里没有描述）
        """
        import json as _json
        ledger = Path(ledger_path)
        if not ledger.exists():
            return {"loaded": 0, "error": "ledger_not_found"}
        
        task_results = {}  # agent_id -> [{state, task_id}]
        task_descs = task_desc_map or {}
        
        for line in open(ledger, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                r = _json.loads(line)
            except:
                continue
            
            rid = r.get("record", "")
            tid = r.get("task_id", "")
            iid = r.get("interface_id", "")
            state = r.get("state", "")
            
            if rid == "TASK_STATE" and iid and state in ("SUCCEEDED", "FAILED", "TIMED_OUT"):
                task_results.setdefault(iid, []).append({
                    "task_id": tid, "state": state, "task_type": self._extract_task_type(task_descs.get(tid, tid))
                })
        
        loaded = 0
        for agent_id, results in task_results.items():
            if agent_id not in self.agents:
                continue
            profile = self.agents[agent_id]
            for r in results:
                success = r["state"] == "SUCCEEDED"
                profile.record_result(r["task_type"], success, 0)  # duration unknown
                loaded += 1
        
        self._save()
        return {"loaded": loaded, "agents_updated": len(task_results)}


    def _extract_task_type(self, task_desc: str) -> str:
        """从任务描述提取任务类型（简化版：关键词匹配）。
        生产版本可换成 LLM 分类。"""
        desc = task_desc.lower()
        type_map = [
            ("security|安全|audit|审计|vulnerability", "security"),
            ("css|样式|frontend|页面|layout|responsive", "frontend"),
            ("test|测试|coverage|assertion", "testing"),
            ("deploy|部署|cloudflare|pages|release", "deploy"),
            ("bug|fix|修复|error|crash|调试", "debug"),
            ("document|文档|readme|spec|specification", "docs"),
            ("refactor|重构|cleanup|tech debt", "refactor"),
            ("api|backend|server|endpoint|database|migration", "backend"),
        ]
        for pattern, task_type in type_map:
            import re
            if re.search(pattern, desc):
                return task_type
        return "general"

    def _skill_overlap(self, task_desc: str, profile: AgentProfile) -> float:
        """agent 的 skills 与任务描述的重合度（0-1）。"""
        desc_words = set(task_desc.lower().split())
        if not profile.skills:
            return 0.0
        overlap = sum(1 for s in profile.skills if s in task_desc.lower() or any(s in w for w in desc_words))
        return min(overlap / len(profile.skills), 1.0)

    def _score_agent(self, profile: AgentProfile, task_desc: str,
                     task_type: str) -> tuple[float, dict]:
        """给一个 agent 打分。返回 (score, breakdown)。分数越高越适合。"""
        breakdown = {}

        # 1. 能力匹配（skills 重合度）
        skill_score = self._skill_overlap(task_desc, profile)
        breakdown["skill_match"] = round(skill_score, 3)

        # 2. 历史成功率（自适应部分——核心创新）
        sr = profile.success_rate(task_type)
        if sr is not None:
            hist_score = sr  # 0-1
            breakdown["history_success_rate"] = round(sr, 3)
        else:
            # 没做过这类任务：中性分 0.5（既不加分也不减分）
            hist_score = 0.5
            breakdown["history_success_rate"] = None

        # 3. 当前负载（并发数 / max_concurrent，越空越高分）
        active = self.active_tasks.get(profile.agent_id, 0)
        load = active / max(profile.max_concurrent, 1)
        load_score = max(0.0, 1.0 - load)
        breakdown["load_score"] = round(load_score, 3)

        # 4. 速度（历史平均完成时间，越短越高分）
        avg_d = profile.avg_duration(task_type)
        if avg_d is not None:
            speed_score = max(0.0, 1.0 - avg_d / 600)  # 600s=10min 作为基准
            breakdown["speed_score"] = round(speed_score, 3)
        else:
            speed_score = 0.5
            breakdown["speed_score"] = None

        # 加权总分：能力匹配 40% + 历史成功率 30% + 负载 20% + 速度 10%
        type_bonus = 0.3 if task_type in profile.skills else 0.0
        breakdown["type_bonus"] = round(type_bonus, 3)
        score = (skill_score * 0.4 +
                 hist_score * 0.3 +
                 load_score * 0.2 +
                 speed_score * 0.1 +
                 type_bonus)
        breakdown["weighted_total"] = round(score, 4)
        return score, breakdown

    def route(self, task_desc: str) -> dict:
        """路由：返回最佳 agent 选择 + 可解释的决策依据。

        返回：
            selected: 最佳 agent_id
            confidence: 置信度（0-1，<0.4 时建议升级给人）
            reason: 为什么选它
            candidates: 所有候选的得分排序
            task_type: 任务类型
        """
        if not self.agents:
            return {"error": "no_agents_registered"}

        task_type = self._extract_task_type(task_desc)
        scored = []
        for aid, profile in self.agents.items():
            score, breakdown = self._score_agent(profile, task_desc, task_type)
            scored.append({
                "agent_id": aid,
                "score": round(score, 4),
                "breakdown": breakdown,
            })
        scored.sort(key=lambda x: x["score"], reverse=True)

        top = scored[0]
        confidence = top["score"]
        # 置信度：如果第一名和第二名差距很小，置信度降低
        if len(scored) > 1:
            margin = top["score"] - scored[1]["score"]
            confidence = min(top["score"], top["score"] + margin * 0.5)

        # 低置信度升级
        needs_human = confidence < 0.4 or len(self.agents) <= 1

        reason = self._explain(top, scored, task_type)
        return {
            "selected": top["agent_id"],
            "confidence": round(confidence, 4),
            "needs_human": needs_human,
            "reason": reason,
            "task_type": task_type,
            "candidates": scored[:5],
        }

    def _explain(self, top: dict, all_scored: list, task_type: str) -> str:
        """生成可解释的路由决策。"""
        reasons = []
        bd = top["breakdown"]
        if bd.get("skill_match", 0) > 0.3:
            reasons.append("skills match task description")
        if bd.get("history_success_rate") is not None:
            reasons.append("historical success rate %.0f%% on %s tasks" %
                          (bd["history_success_rate"] * 100, task_type))
        else:
            reasons.append("no history on %s tasks (neutral)" % task_type)
        if bd.get("load_score", 0) > 0.5:
            reasons.append("low current load")
        if len(all_scored) > 1:
            gap = all_scored[0]["score"] - all_scored[1]["score"]
            if gap > 0.1:
                reasons.append("significantly better than next candidate (%.0f%% higher)" % (gap * 100))
            else:
                reasons.append("close call with next candidate")
        return "; ".join(reasons)

    def record_result(self, agent_id: str, task_desc: str, success: bool, duration_s: float):
        """任务完成后调用：更新 agent 历史统计（自适应路由的数据来源）。"""
        if agent_id not in self.agents:
            return
        task_type = self._extract_task_type(task_desc)
        self.agents[agent_id].record_result(task_type, success, duration_s)
        # 减少活跃任务计数
        self.active_tasks[agent_id] = max(0, self.active_tasks.get(agent_id, 1) - 1)
        self._save()
