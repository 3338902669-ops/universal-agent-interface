"""E-CRED: memory injection must not leak credential-like text.

Real end-to-end found this: injected graph facts contained "api_key",
which tripped hub's envelope credential check and silently blocked dispatch
(error credential_like_field_in_envelope). Two fixes, both tested here:
1. TeamMemoryBridge filters credential-like facts before injecting
2. inbound_bridge retries without memory injection if hub still rejects
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from team_memory import TeamMemoryBridge, CREDENTIAL_MARKERS, _looks_like_credential
from inbound_bridge import _should_retry_without_memory, InboundBridgeError

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def main():
    # 1. 检测器覆盖 inbound.py 的同一组字样
    for marker in ("api_key", "apikey", "password", "secret", "token="):
        check("CRED-1 detects %s" % marker,
              _looks_like_credential("x %s y" % marker.upper()), marker)
    check("CRED-2 clean text passes",
          _looks_like_credential("codex 是实现主力") is False, "")

    def memory_block(result, task):
        """只取注入的记忆块（原任务之前的全部文本）。"""
        idx = result.rfind(task)
        return result if idx < 0 else result[:idx]

    # 2. 注入的记忆块绝不含凭据样字样（原任务本身可以含，那是用户自己的话）
    mem = TeamMemoryBridge()
    probe = "API_KEY token password configuration"
    injected = mem.inject_context(probe)
    block = memory_block(injected, probe)
    leaked = [m for m in CREDENTIAL_MARKERS if m in block.lower()]
    check("CRED-3 memory block has no credential markers", not leaked, str(leaked))
    check("CRED-4 original task still present", probe in injected, "")
    mem.close()

    # 3. 回退判定只对凭据拒绝生效
    check("CRED-5 retries on credential rejection",
          _should_retry_without_memory(
              InboundBridgeError("hub_rejected: credential_like_field_in_envelope: api_key")) is True,
          "")
    check("CRED-6 no retry on other rejections",
          _should_retry_without_memory(InboundBridgeError("hub_rejected: duplicate_task")) is False,
          "")

    # 4. 真实图谱里的记忆也不再夹带凭据样字样（用会命中图谱的查询）
    mem2 = TeamMemoryBridge()
    for q in ("api key 配置", "token 中转", "neo4j password", "codex 是实现主力"):
        out = mem2.inject_context(q)
        blk = memory_block(out, q)
        bad = [m for m in CREDENTIAL_MARKERS if m in blk.lower()]
        check("CRED-7 graph memory clean: %s" % q[:18], not bad, str(bad))
    mem2.close()

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
