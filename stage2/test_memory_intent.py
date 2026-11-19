"""P1 自检：MemoryIntent 契约（结构、证据分级、凭据拒收、单行归一）。"""

from __future__ import annotations

import sys
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE2))

import memory_intent as MI  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def base(**over):
    intent = {
        "intent_id": "int-0001",
        "kind": "decision",
        "subject": "UAI 记忆管道",
        "claim": "用户决定采用完整企业级记忆管道（第 3 档）",
        "evidence_level": "E1",
        "evidence": "pytest stage2/test_memory_intent.py exit=0",
        "source_agent": "dsh",
        "group_id": "team",
        "reference_time": "2026-11-20T01:59:51+08:00",
        "sensitivity": "internal",
    }
    intent.update(over)
    return intent


def test_valid():
    check("MI-1 合法 intent 通过", MI.validate_intent(base()) is None,
          str(MI.validate_intent(base())))
    norm = MI.normalize_intent(base(subject="  UAI   记忆   管道  "))
    check("MI-2 subject 空白归一", norm["subject"] == "UAI 记忆 管道", repr(norm["subject"]))
    check("MI-3 group 默认 team", norm["group_id"] == "team", norm["group_id"])


def test_kind():
    r = MI.validate_intent(base(kind="gossip"))
    check("MI-4 非法 kind 拒绝", r == "invalid_intent:unsupported_kind", str(r))
    for k in ("decision", "milestone", "failure", "blockage", "handoff", "verdict"):
        check("MI-5 kind 允许 %s" % k, MI.validate_intent(base(kind=k)) is None, k)


def test_evidence():
    r = MI.validate_intent(base(evidence=""))
    check("MI-6 E1 缺 evidence 拒绝", r == "invalid_intent:e1_requires_evidence", str(r))
    r = MI.validate_intent(base(evidence_level="E9"))
    check("MI-7 非法证据等级拒绝", r == "invalid_intent:invalid_evidence_level", str(r))
    check("MI-8 E3 无 evidence 允许", MI.validate_intent(base(evidence_level="E3", evidence="")) is None)


def test_secret_and_credential():
    r = MI.validate_intent(base(sensitivity="secret"))
    check("MI-9 sensitivity=secret 拒收", r == "invalid_intent:sensitivity_secret_rejected", str(r))
    r = MI.validate_intent(base(claim="部署时把 api_key=sk-xxx 写进环境"))
    check("MI-10 凭据样式文本拒收", r == "invalid_intent:credential_like_text", str(r))
    r = MI.validate_intent(base(evidence="token=abc123"))
    check("MI-11 evidence 含凭据同样拒收", r == "invalid_intent:credential_like_text", str(r))


def test_fields():
    r = MI.validate_intent(base(group_id="Team Layer"))
    check("MI-12 非法 group_id 拒绝", r == "invalid_intent:invalid_group_id", str(r))
    r = MI.validate_intent(base(reference_time="昨天"))
    check("MI-13 非法 reference_time 拒绝", r == "invalid_intent:invalid_reference_time", str(r))
    r = MI.validate_intent(base(claim=""))
    check("MI-14 空 claim 拒绝", r == "invalid_intent:missing_claim", str(r))
    r = MI.validate_intent(base(subject=""))
    check("MI-15 空 subject 拒绝", r == "invalid_intent:missing_subject", str(r))
    r = MI.validate_intent(base(intent_id=""))
    check("MI-16 空 intent_id 拒绝", r == "invalid_intent:missing_intent_id", str(r))
    r = MI.validate_intent(base(claim="字" * (MI.MAX_CLAIM_CHARS + 1)))
    check("MI-17 超长 claim 拒绝", r == "invalid_intent:claim_too_long", str(r))


def test_payload():
    payload = MI.build_episode_payload(base())
    check("MI-18 payload name 含 intent_id",
          payload["name"].startswith("uai:") and payload["intent_id"] in payload["name"],
          payload["name"])
    check("MI-19 payload group_id = team", payload["group_id"] == "team", payload["group_id"])
    check("MI-20 payload 带 reference_time",
          payload["reference_time"] == "2026-11-20T01:59:51+08:00", payload["reference_time"])
    check("MI-21 body 含 subject 与 claim",
          "UAI 记忆管道" in payload["body"] and "第 3 档" in payload["body"], payload["body"][:60])
    check("MI-22 body 含证据等级", "E1" in payload["body"], payload["body"][:80])
    check("MI-23 body 不含空行堆积", "\n\n\n" not in payload["body"], "")


def main() -> int:
    test_valid()
    test_kind()
    test_evidence()
    test_secret_and_credential()
    test_fields()
    test_payload()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
