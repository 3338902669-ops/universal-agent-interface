r"""子进程凭据注入（Windows: HKCU\Environment 兜底）。

为什么需要：`uai serve` 常由「启动文件夹 / 计划任务」这类**环境块很早冻结**的宿主拉起，
它的 environ 里往往没有 API key（实测 supervisor.environ() 里一个 key 都没有）。
没有 key 的席位进程会活着、握着锁、发着心跳，但一接单就
`MISSING_CREDENTIAL: llm-deepseek: no API key` —— 这就是「面板显示运行中但干不了活」。

纪律（硬约束）：
- 只往子进程的 env 里传，**不写任何文件、不进日志、不进账本**；
- 对外只暴露变量**名**与命中/缺失状态，绝不返回值本身；
- 宿主环境里已有的值优先（不覆盖）。
"""
from __future__ import annotations

import os

DEFAULT_KEYS = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPNAI_API_KEY",
    "OPNAI_CLAUDE_KEY",
    "OPENROUTER_API_KEY",
)


def _from_registry(names: tuple[str, ...]) -> dict[str, str]:
    """Windows: 读 HKCU\\Environment（用户级，setx 写的就是这里）。"""
    if os.name != "nt":
        return {}
    try:
        import winreg
    except Exception:
        return {}
    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in names:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                # 同样：判空用 strip，取原值（第 2 条：登记了就不该再裁剪）
                if isinstance(value, str) and value.strip():
                    out[name] = value
    except OSError:
        return {}
    return out


def resolve(names) -> tuple[dict[str, str], dict[str, object]]:
    """→ (要注入的 env 键值, 可安全打印的诊断)

    诊断只含名字：{injected: [...], missing: [...], source: 'env|registry|none'}
    """
    wanted = tuple(dict.fromkeys(n for n in names if n))
    injected: dict[str, str] = {}
    sources: set[str] = set()
    for name in wanted:
        value = os.environ.get(name)
        # 空白字符串不算「有值」：否则一个 "   " 就会挡掉注册表回退，
        # 子进程拿到空 key → 运行时 MISSING_CREDENTIAL（codex 审查实测指出）
        # 判空用 strip，但**注入原值**：改值会破坏「宿主已有值优先、不覆盖」的约定，
        # 也会裁掉合法密钥的首尾字符（codex 席位审代码 FAIL 第 1 条）。
        if value is not None and value.strip():
            injected[name] = value
            sources.add("env")
    missing = [n for n in wanted if n not in injected]
    if missing:
        for name, value in _from_registry(tuple(missing)).items():
            injected[name] = value
            sources.add("registry")
    diag = {
        "injected": sorted(injected),
        "missing": [n for n in wanted if n not in injected],
        "source": "+".join(sorted(sources)) if sources else "none",
    }
    return injected, diag
