"""任务自动拆解器（2026-11-16）。

解决：大任务怎么自动拆成子任务分给不同 agent？
不是人手动拆——系统根据任务类型 + 可用 agent 能力自动拆。

核心设计：
- 拆解模板：每种任务类型有一套拆解模板（如 full_stack → frontend/backend/test/deploy）
- 依赖 DAG：子任务之间有依赖关系（backend 完成 → test 才能跑）
- 与 SmartRouter 集成：每个子任务的 task_type 直接路由到最佳 agent
- 可解释：输出拆解依据

用法：
    from task_decomposer import TaskDecomposer
    d = TaskDecomposer()
    plan = d.decompose("做一个完整的用户管理系统，包含前端页面和后端API")
    # plan.subtasks = [{desc, type, depends_on, suggested_agent}, ...]
"""
from __future__ import annotations

import re
from typing import Any


class DecomposerError(ValueError):
    pass


# 拆解模板：task_type -> 子任务模板列表
# 每个子任务：{description_template, type, depends_on (index list)}
DECOMPOSITION_TEMPLATES = {
    "full_stack": [
        {"desc": "设计数据模型和 API schema", "type": "backend", "depends_on": []},
        {"desc": "实现后端 API endpoints", "type": "backend", "depends_on": [0]},
        {"desc": "实现前端页面和交互", "type": "frontend", "depends_on": [0]},
        {"desc": "编写集成测试", "type": "testing", "depends_on": [1, 2]},
        {"desc": "部署到生产环境", "type": "deploy", "depends_on": [3]},
    ],
    "frontend_only": [
        {"desc": "实现页面布局和样式", "type": "frontend", "depends_on": []},
        {"desc": "实现交互逻辑和状态管理", "type": "frontend", "depends_on": [0]},
        {"desc": "响应式适配和浏览器测试", "type": "frontend", "depends_on": [1]},
    ],
    "backend_only": [
        {"desc": "设计 API schema 和数据模型", "type": "backend", "depends_on": []},
        {"desc": "实现 API endpoints", "type": "backend", "depends_on": [0]},
        {"desc": "编写 API 测试", "type": "testing", "depends_on": [1]},
    ],
    "bug_fix": [
        {"desc": "复现 bug 并定位根因", "type": "debug", "depends_on": []},
        {"desc": "修复代码", "type": "backend", "depends_on": [0]},
        {"desc": "验证修复并跑回归", "type": "testing", "depends_on": [1]},
    ],
    "security_audit": [
        {"desc": "扫描代码安全漏洞", "type": "security", "depends_on": []},
        {"desc": "检查权限控制和认证", "type": "security", "depends_on": []},
        {"desc": "修复发现的问题", "type": "backend", "depends_on": [0, 1]},
        {"desc": "复扫验证", "type": "security", "depends_on": [2]},
    ],
    "general": [
        {"desc": "执行任务", "type": "general", "depends_on": []},
    ],
}


class TaskDecomposer:
    """任务自动拆解器。"""

    def __init__(self, available_skills: list[str] | None = None):
        """available_skills: 系统中 agent 们实际具备的 skills 并集。
        用于过滤子任务（如果没人会做某类子任务，标记为需要人处理）。"""
        self.available_skills = set(s.lower() for s in (available_skills or []))

    def classify(self, task_desc: str) -> str:
        """分类大任务的类型（决定用哪个拆解模板）。"""
        desc = task_desc.lower()
        # 优先级从高到低
        if re.search(r"security|安全|audit|审计", desc):
            return "security_audit"
        if re.search(r"full.?stack|完整|整个系统|前后端|前端.*后端", desc) or            ("frontend" in desc and "backend" in desc) or            ("前端" in desc and "后端" in desc):
            return "full_stack"
        if re.search(r"bug|fix|修复|调试|crash|error", desc):
            return "bug_fix"
        if re.search(r"前端|frontend|页面|css|ui|ux", desc):
            return "frontend_only"
        if re.search(r"后端|backend|api|server|database", desc):
            return "backend_only"
        return "general"

    def decompose(self, task_desc: str) -> dict:
        """拆解大任务。返回：
        {
            "original": 原始描述,
            "template_used": 模板名,
            "subtasks": [
                {"index": 0, "description": "...", "type": "...",
                 "depends_on": [0], "routable": True/False},
            ],
            "dag": 拓扑排序后的执行顺序,
            "unroutable": 无法路由的子任务（没有 agent 具备该 skill）,
            "explanation": 拆解依据
        }
        """
        template_name = self.classify(task_desc)
        template = DECOMPOSITION_TEMPLATES.get(template_name)
        if not template:
            raise DecomposerError("no_template_for: %s" % template_name)

        subtasks = []
        for i, t in enumerate(template):
            task_type = t["type"]
            routable = task_type in self.available_skills
            subtasks.append({
                "index": i,
                "description": t["desc"],
                "type": task_type,
                "depends_on": list(t["depends_on"]),
                "routable": routable,
            })

        # 拓扑排序（按依赖关系）
        dag_order = self._topo_sort(subtasks)

        unroutable = [s for s in subtasks if not s["routable"]]

        return {
            "original": task_desc,
            "template_used": template_name,
            "subtasks": subtasks,
            "dag_order": dag_order,
            "unroutable": [s["description"] for s in unroutable],
            "explanation": self._explain(template_name, subtasks, unroutable),
        }

    def decompose_recursive(self, task_desc: str, max_depth: int = 2) -> dict:
        """递归拆解：子任务如果太大了继续拆。"""
        result = self.decompose(task_desc)
        if max_depth <= 0:
            return result
        # 对每个子任务，如果它自己的类型有更细的拆解模板，继续拆
        type_to_template = {"backend": "backend_only", "frontend": "frontend_only", "security": "security_audit"}
        for st in result["subtasks"]:
            template_key = type_to_template.get(st["type"], st["type"])
            if template_key in DECOMPOSITION_TEMPLATES and template_key != "general":
                sub_desc = st["description"]
                sub_result = self.decompose_recursive(sub_desc, max_depth - 1)
                if len(sub_result.get("subtasks", [])) > 1:
                    st["children"] = sub_result["subtasks"]
                elif template_key in DECOMPOSITION_TEMPLATES:
                    tmpl = DECOMPOSITION_TEMPLATES[template_key]
                    st["children"] = [{"index": i, "description": t["desc"], "type": t["type"],
                                       "depends_on": list(t["depends_on"])} for i, t in enumerate(tmpl)]
        return result

    def _topo_sort(self, subtasks: list[dict]) -> list[int]:
        """拓扑排序：返回执行顺序的 index 列表。"""
        visited = set()
        order = []
        def visit(idx):
            if idx in visited:
                return
            visited.add(idx)
            for dep in subtasks[idx]["depends_on"]:
                visit(dep)
            order.append(idx)
        for i in range(len(subtasks)):
            visit(i)
        return order

    def _explain(self, template_name: str, subtasks: list, unroutable: list) -> str:
        parts = ["decomposed using '%s' template into %d subtasks" %
                 (template_name, len(subtasks))]
        if unroutable:
            parts.append("%d subtasks unroutable (no agent has required skill): %s" %
                         (len(unroutable), ", ".join(s["type"] for s in unroutable if not s["routable"])))
            parts.append("these need human assignment or new agent registration")
        else:
            parts.append("all subtasks routable via smart router")
        deps = [s for s in subtasks if s["depends_on"]]
        if deps:
            parts.append("%d subtasks have dependencies (DAG order enforced)" % len(deps))
        return "; ".join(parts)
