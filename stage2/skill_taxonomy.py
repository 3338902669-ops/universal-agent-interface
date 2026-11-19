"""技能词表归一化（I-5 集成修复）。

问题：模块之间对「技能/任务类型」的叫法不统一——
TaskDecomposer 模板用 testing/debug，AgentProfile 里通常写 test/bug/fix，
于是拆解出来的子任务被判成「没有 agent 会做」，实际是有 agent 会做的。

本模块给出一套别名归一化，让 路由 / 协商 / 拆解 三个模块用同一把尺子。
不改变各模块自己的对外语义，只在比较时归一。
"""

from __future__ import annotations

# 归一化组：canonical -> 该组的全部别名（含中文）
ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    "code": ("code", "coding", "impl", "implementation", "dev", "开发", "编码"),
    "testing": ("testing", "test", "tests", "qa", "verify", "验证", "测试"),
    "debug": ("debug", "debugging", "bug", "bugs", "fix", "fixing",
              "troubleshoot", "修复", "调试"),
    "frontend": ("frontend", "front-end", "ui", "ux", "css", "layout",
                 "page", "responsive", "前端", "页面", "样式"),
    "backend": ("backend", "back-end", "api", "server", "db", "database",
                "migration", "后端", "接口"),
    "deploy": ("deploy", "deployment", "release", "ops", "ci",
               "cloudflare", "部署", "发布"),
    "security": ("security", "sec", "audit", "vuln", "vulnerability",
                 "安全", "审计"),
    "docs": ("docs", "doc", "document", "documentation", "readme", "spec",
             "文档"),
    "review": ("review", "reviewing", "code-review", "审查", "复核"),
    "design": ("design", "designer", "visual", "设计"),
}

# alias -> canonical
_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _aliases in ALIAS_GROUPS.items():
    for _a in _aliases:
        _ALIAS_TO_CANON[_a] = _canon


def canonical(skill: str) -> str:
    """把一个技能名归一到组名；不在词表里的原样返回（小写）。"""
    if not isinstance(skill, str):
        return ""
    s = skill.strip().lower()
    return _ALIAS_TO_CANON.get(s, s)


def matches(a: str, b: str) -> bool:
    """两个技能名是否属于同一归一化组。"""
    ca, cb = canonical(a), canonical(b)
    return bool(ca) and ca == cb


def matches_any(skill: str, candidates) -> bool:
    """skill 是否匹配候选集合中的任意一项（归一化后比较）。"""
    c = canonical(skill)
    if not c:
        return False
    return any(canonical(x) == c for x in candidates or ())


def skill_covered(required, available) -> bool:
    """required 中每一项是否都能被 available 覆盖（归一化比较）。"""
    have = {canonical(x) for x in (available or ()) if canonical(x)}
    for r in required or ():
        if canonical(r) not in have:
            return False
    return True
