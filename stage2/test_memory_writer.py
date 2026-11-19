"""P3 自检：写入层双路径（Graphiti 主路 / Neo4j 兜底 / 回读三态）。"""

from __future__ import annotations

import sys
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE2))

import memory_writer as MW  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def intent(**over):
    value = {
        "intent_id": "int-w1",
        "kind": "decision",
        "subject": "写路径",
        "claim": "主路走 Graphiti 抽取",
        "evidence_level": "E1",
        "evidence": "cmd exit=0",
        "group_id": "team",
        "reference_time": "2026-11-20T01:59:51+08:00",
    }
    value.update(over)
    return value


def test_primary():
    calls = {"post": [], "neo4j": []}

    def post(url, payload, timeout):
        calls["post"].append(payload)
        return {"ok": True}

    def verify(payload):
        return "verified"

    out = MW.write_intent(intent(), post=post, neo4j_write=None, verify=verify)
    check("MW-1 主路成功 written=True", out["written"] is True, str(out))
    check("MW-2 主路 path=graphiti", out["path"] == "graphiti", out["path"])
    check("MW-3 主路非降级", out["degraded"] is False, str(out["degraded"]))
    check("MW-4 主路回读 verified", out["verified"] == "verified", str(out["verified"]))
    check("MW-5 主路确实发出 episode", len(calls["post"]) == 1, str(len(calls["post"])))
    check("MW-6 payload 带 team group", calls["post"][0]["group_id"] == "team",
          calls["post"][0]["group_id"])


def test_fallback():
    calls = {"neo4j": []}

    def post(url, payload, timeout):
        raise RuntimeError("graphiti_down")

    def neo4j_write(payload):
        calls["neo4j"].append(payload)
        return {"ok": True, "intent_key": payload["intent_key"]}

    def verify(payload):
        return "verified"

    out = MW.write_intent(intent(), post=post, neo4j_write=neo4j_write, verify=verify)
    check("MW-7 主路失败走兜底 written=True", out["written"] is True, str(out))
    check("MW-8 兜底 path=neo4j", out["path"] == "neo4j", out["path"])
    check("MW-9 兜底标记 degraded=True", out["degraded"] is True, str(out["degraded"]))
    check("MW-10 兜底原因可查", out["reason"] == "graphiti_down", str(out["reason"]))
    check("MW-11 兜底写入了图", len(calls["neo4j"]) == 1, str(len(calls["neo4j"])))
    check("MW-12 兜底 group 仍是 team", calls["neo4j"][0]["group_id"] == "team",
          calls["neo4j"][0]["group_id"])


def test_total_failure():
    def post(url, payload, timeout):
        raise RuntimeError("graphiti_down")

    def neo4j_write(payload):
        raise RuntimeError("neo4j_down")

    out = MW.write_intent(intent(), post=post, neo4j_write=neo4j_write)
    check("MW-13 两条路都挂 written=False", out["written"] is False, str(out))
    check("MW-14 失败原因含两路", "graphiti_down" in out["reason"] and "neo4j_down" in out["reason"],
          str(out["reason"]))
    check("MW-15 失败不抛异常", True, "")


def test_verification_unknown():
    def post(url, payload, timeout):
        return {"ok": True}

    def verify(payload):
        return None

    out = MW.write_intent(intent(), post=post, neo4j_write=None, verify=verify)
    check("MW-16 无法回读时 written 仍成立", out["written"] is True, str(out))
    check("MW-17 verified 标记 unverified", out["verified"] == "unverified", str(out["verified"]))


def test_credential_guard():
    hit = {"count": 0}

    def post(url, payload, timeout):
        hit["count"] += 1
        return {"ok": True}

    out = MW.write_intent(intent(claim="把 api_key=sk-abc 落盘"), post=post)
    check("MW-18 出队二次凭据扫描拒写", out["written"] is False, str(out))
    check("MW-19 凭据内容根本没发出", hit["count"] == 0, str(hit["count"]))
    check("MW-20 拒写原因是 credential_like_text",
          out["reason"] == "credential_like_text", str(out["reason"]))


def test_drain():
    import tempfile
    import memory_outbox as MO

    ob = MO.MemoryOutbox(Path(tempfile.mkdtemp()) / "outbox.db")
    ob.enqueue(intent(intent_id="int-w2"))

    def post(url, payload, timeout):
        return {"ok": True}

    def verify(payload):
        return "verified"

    result = MW.drain(ob, post=post, neo4j_write=None, verify=verify, limit=10)
    check("MW-21 drain 写出 1 条", result["delivered"] == 1, str(result))
    check("MW-22 drain 后状态 delivered",
          ob.get("int-w2")["status"] == "delivered", ob.get("int-w2")["status"])


def test_drain_failure():
    import tempfile
    import memory_outbox as MO

    ob = MO.MemoryOutbox(Path(tempfile.mkdtemp()) / "outbox.db")
    ob.enqueue(intent(intent_id="int-w3"))

    def post(url, payload, timeout):
        raise RuntimeError("graphiti_down")

    def neo4j_write(payload):
        raise RuntimeError("neo4j_down")

    result = MW.drain(ob, post=post, neo4j_write=neo4j_write, limit=10)
    check("MW-23 drain 失败计入 retried", result["retried"] == 1, str(result))
    check("MW-24 失败后进 retry", ob.get("int-w3")["status"] == "retry",
          ob.get("int-w3")["status"])


def main() -> int:
    test_primary()
    test_fallback()
    test_total_failure()
    test_verification_unknown()
    test_credential_guard()
    test_drain()
    test_drain_failure()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
