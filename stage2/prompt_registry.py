"""固定提示词模板注册表（2026-11-15）。

模板库：~/.shared/prompt-templates/*.md（SKILL.md frontmatter 兼容形态）
paseo initialPrompt 从此取：load(name) → render(vars) → 纯文本提示词。

用法：
    from prompt_registry import PromptRegistry
    reg = PromptRegistry()
    prompt = reg.render("dev-implement", {"TASK": "...", "SCOPE_FILES": "a.py", "CONSTRAINTS": "read-only"})
    # → pas = paseo_execute(prompt)
"""
from __future__ import annotations

import re
from pathlib import Path

VAR_PATTERN = re.compile(r"\{([A-Z_]+)\}")
REQUIRED_META = ("name", "description")


class PromptRegistry:
    DEFAULT_DIR = Path("C:/Users/33389/.shared/prompt-templates")  # 本机固定路径，环境缺失时兜底

    def __init__(self, template_dir: str | Path | None = None):
        if template_dir:
            resolved = Path(template_dir)
        else:
            try:
                resolved = Path.home() / ".shared" / "prompt-templates"
                resolved.exists()  # 探测 home 可用性
            except RuntimeError:
                resolved = self.DEFAULT_DIR
        self.template_dir = resolved

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.template_dir.glob("*.md")):
            meta = self._parse(p)
            if meta:
                out.append({"name": meta["name"], "description": meta["description"],
                            "variables": sorted(set(VAR_PATTERN.findall(meta["body"])))})
        return out

    def load(self, name: str) -> dict:
        """按模板 name 取（frontmatter name 优先，文件名兜底）。"""
        for p in sorted(self.template_dir.glob("*.md")):
            meta = self._parse(p)
            if meta and (meta["name"] == name or p.stem == name):
                return meta
        raise FileNotFoundError("template not found: %s" % name)

    def render(self, name: str, variables: dict[str, str], strict: bool = True) -> str:
        """渲染模板。strict=True 时未提供的变量报错（防止静默留占位符）。"""
        meta = self.load(name)
        body = meta["body"]
        needed = set(VAR_PATTERN.findall(body))
        missing = sorted(needed - set(variables.keys()))
        if strict and missing:
            raise ValueError("missing_variables:%s" % ",".join(missing))
        def sub(m):
            return variables.get(m.group(1), m.group(0))
        return VAR_PATTERN.sub(sub, body).strip()

    def _parse(self, path: Path) -> dict | None:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        meta = {}
        for line in parts[1].strip().split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        if not all(k in meta for k in REQUIRED_META):
            return None
        meta["body"] = parts[2].strip()
        return meta
