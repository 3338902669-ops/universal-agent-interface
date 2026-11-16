"""E-MEM: team memory bridge test."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from team_memory import TeamMemoryBridge

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    mem = TeamMemoryBridge()

    # 1. 搜索真实图谱数据（Neo4j 7687 在跑）
    facts = mem.search("codex")
    check("MEM-1 搜索 codex 相关事实", len(facts) > 0, str(len(facts)) + " facts")
    check("MEM-2 事实有 name 或 summary", any(f.get("summary") or f.get("name") for f in facts if "error" not in f), "")

    # 2. 搜索 entities
    entities = mem.search_entities("codex")
    check("MEM-3 搜索 codex Entity", len(entities) > 0, str(len(entities)))

    # 3. inject_context: 有记忆时注入
    injected = mem.inject_context("codex 是实现主力")
    check("MEM-4 注入了团队记忆", "团队记忆" in injected, injected[:80])
    check("MEM-5 原始任务在末尾", "codex 是实现主力" in injected, "")

    # 4. inject_context: 无关任务不注入
    injected2 = mem.inject_context("完全无关的查询xyz123abc")
    check("MEM-6 无关任务不注入", "团队记忆" not in injected2, "")

    # 5. record_outcome 写回
    mem.record_outcome("test-agent", "test task for memory bridge", True, "通过")
    # 验证写回（搜新写入的记录）
    import time; time.sleep(1)
    facts2 = mem.search("test task for memory bridge")
    check("MEM-7 record_outcome 写回图谱", len(facts2) > 0, str(len(facts2)))

    mem.close()
    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
