"""UAI 记忆写入层：主路 Graphiti 抽取，兜底 Neo4j 直写，写后回读。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

import memory_intent as MI

DEFAULT_GRAPH_URL = os.environ.get("UAI_GRAPH_URL", "http://127.0.0.1:8642")
# Graphiti 首次抽取要加载嵌入模型，实测 4.8s 起；8s 会在冷启动误判失败。
DEFAULT_TIMEOUT_S = float(os.environ.get("UAI_GRAPH_WRITE_TIMEOUT", "20"))
NEO4J_URI = os.environ.get("UAI_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("UAI_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("UAI_NEO4J_PASSWORD", "graphiti")


def _http_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/episodes",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8") or "{}"
    return json.loads(raw)


def _driver():
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD), connection_timeout=3.0
    )


def _neo4j_write(payload: dict[str, Any]) -> dict[str, Any]:
    drv = _driver()
    try:
        with drv.session() as session:
            session.run(
                "MERGE (e:Episodic {uai_intent_key: $key}) "
                "ON CREATE SET e.name=$name, e.summary=$body, e.created_at=datetime(), "
                "  e.reference_time=$rt, e.source=$src, e.group_id=$gid, e.degraded=true, "
                "  e.uai_key=$key "
                "ON MATCH SET e.uai_seen = coalesce(e.uai_seen, 0) + 1, "
                "  e.uai_key=$key, e.uai_last_seen_at = datetime()",
                {
                    "key": payload["intent_key"],
                    "name": payload["name"],
                    "body": payload["body"],
                    "rt": payload["reference_time"],
                    "src": payload["source_description"],
                    "gid": payload["group_id"],
                },
            )
        return {"ok": True, "intent_key": payload["intent_key"]}
    finally:
        try:
            drv.close()
        except Exception:
            pass


def _verify(payload: dict[str, Any], path: str | None = None) -> str | None:
    """写后回读。

    **兜底路径一律返回 None**：Neo4j 节点是我们自己 MERGE 的，用同一条写入
    结果去「验证自己」等于自证，审计会因此把未确认写入算成有记忆。
    只有 Graphiti 抽取路径才值得回读，且必须校验 group 归属，避免同名误判。
    """
    if path == "neo4j":
        return None
    try:
        drv = _driver()
    except Exception:
        return None
    try:
        with drv.session() as session:
            row = session.run(
                "MATCH (e:Episodic) WHERE e.name=$n AND e.group_id=$g "
                "RETURN count(e) AS n",
                {"n": payload["name"], "g": payload["group_id"]},
            ).single()
        return "verified" if int((row or {"n": 0})["n"]) > 0 else "unverified"
    except Exception:
        return None
    finally:
        try:
            drv.close()
        except Exception:
            pass


def _exists(payload: dict[str, Any]) -> bool:
    """同一 intent 是否已经**以主路形式**在图里。

    Graphiti /episodes 不是幂等的（实测同 payload 连投两次 → 2 个 Episodic），
    所以重投前必须查重。两条额外约束：

    1. 必须按 group_id 过滤，否则他组同名节点会被误判成「已写入」；
    2. **兜底 stub 不算已写入**：stub 没有实体/边/嵌入，若把它当完成，
       这条记忆就永远升不了级，审计里的 degraded 也会消失。
    """
    try:
        drv = _driver()
    except Exception:
        return False
    try:
        with drv.session() as session:
            row = session.run(
                "MATCH (e:Episodic) "
                "WHERE (e.name=$n OR e.uai_intent_key=$k) AND e.group_id=$g "
                "  AND coalesce(e.degraded, false) = false "
                "RETURN count(e) AS n",
                {"n": payload["name"], "k": payload["intent_key"],
                 "g": payload["group_id"]},
            ).single()
        return int((row or {"n": 0})["n"]) > 0
    except Exception:
        return False
    finally:
        try:
            drv.close()
        except Exception:
            pass


def _stamp_keys(payload: dict[str, Any]) -> None:
    """把 Graphiti 写入的节点补上新旧两套幂等键。

    旧工具（team_memory / UAI-08 自检 / smoke 脚本）按 uai_key 查，新管道按
    uai_intent_key 查；不补这一步就会出现「写进去了但旧工具检索不到」。
    """
    try:
        drv = _driver()
    except Exception:
        return
    try:
        with drv.session() as session:
            # 主路写成功后，把同 intent 的兜底 stub 删掉：否则同一段记忆会有
            # 两份节点（一份可检索、一份不可检索），审计与检索都会打架。
            session.run(
                "MATCH (e:Episodic) WHERE e.uai_intent_key=$k "
                "  AND coalesce(e.degraded, false) = true DETACH DELETE e",
                {"k": payload["intent_key"]},
            )
            session.run(
                "MATCH (e:Episodic) WHERE e.name=$n AND e.group_id=$g "
                "SET e.uai_key=$k, e.uai_intent_key=$k",
                {"n": payload["name"], "k": payload["intent_key"],
                 "g": payload["group_id"]},
            )
    except Exception:
        return
    finally:
        try:
            drv.close()
        except Exception:
            pass


def _reason_suffix(reason: str) -> str:
    return reason[len("invalid_intent:"):] if reason.startswith("invalid_intent:") else reason


def write_intent(
    intent: dict[str, Any],
    *,
    graph_url: str | None = None,
    post: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
    neo4j_write: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    verify: Callable[[dict[str, Any], str | None], str | None] | None = None,
    exists: Callable[[dict[str, Any]], bool] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """写一条记忆。永不抛异常。"""
    result: dict[str, Any] = {
        "written": False,
        "path": None,
        "degraded": False,
        "verified": "unverified",
        "reason": None,
    }

    reason = MI.validate_intent(intent)
    if reason:
        result["reason"] = _reason_suffix(reason)
        return result

    payload = MI.build_episode_payload(intent)

    # 出队二次凭据扫描：入队被绕过时也不能把密钥写进图
    if MI.looks_like_credential(payload["body"]):
        result["reason"] = "credential_like_text"
        return result

    post_fn = post or _http_post
    neo4j_fn = neo4j_write or _neo4j_write
    verify_fn = verify or _verify
    exists_fn = exists or _exists
    graphiti_error: str | None = None

    # 幂等前置检查：已存在就不再投递（Graphiti 不幂等）
    try:
        already = bool(exists_fn(payload))
    except Exception:
        already = False
    if already:
        result.update({
            "written": True,
            "path": "graphiti",
            "degraded": False,
            "reason": "already_present",
        })
        result["verified"] = (
            "verified" if _call_verify(verify_fn, payload, "graphiti") else "unverified"
        )
        return result

    try:
        post_fn(graph_url or DEFAULT_GRAPH_URL, payload, timeout or DEFAULT_TIMEOUT_S)
        _stamp_keys(payload)
        result.update({"written": True, "path": "graphiti", "degraded": False})
    except Exception as exc:
        graphiti_error = str(exc)[:160]
        try:
            neo4j_fn(payload)
            result.update(
                {"written": True, "path": "neo4j", "degraded": True,
                 "reason": graphiti_error}
            )
        except Exception as exc2:
            result["reason"] = "%s; %s" % (graphiti_error, str(exc2)[:160])
            return result

    result["verified"] = "verified" if _call_verify(verify_fn, payload, result["path"]) else "unverified"
    return result


def _call_verify(fn, payload: dict[str, Any], path: str | None) -> bool:
    """兼容两种 verify 签名：内置的 (payload, path) 与测试注入的 (payload)。"""
    try:
        verdict = fn(payload, path)
    except TypeError:
        verdict = fn(payload)
    return verdict == "verified"


def drain(
    outbox: Any,
    *,
    graph_url: str | None = None,
    post: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
    neo4j_write: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    verify: Callable[[dict[str, Any], str | None], str | None] | None = None,
    exists: Callable[[dict[str, Any]], bool] | None = None,
    limit: int = 10,
    now: float | None = None,
    timeout: float | None = None,
) -> dict[str, int]:
    outbox.recover_stale()
    items = outbox.claim_due(limit=limit, now=now)
    delivered = retried = dead = 0
    for item in items:
        outcome = write_intent(
            item["intent"],
            graph_url=graph_url,
            post=post,
            neo4j_write=neo4j_write,
            verify=verify,
            exists=exists,
            timeout=timeout,
        )
        if outcome["written"]:
            applied = outbox.mark_delivered(
                item["intent_id"],
                degraded=bool(outcome["degraded"]),
                verified=outcome["verified"],
                write_path=outcome["path"],
                last_error=outcome.get("reason") if outcome["degraded"] else None,
            )
            # 行可能已被 recover_stale 打回 retry：那时它还会被再投一次，
            # 不能算 delivered，否则计数与库内状态分叉。
            if applied:
                delivered += 1
            else:
                retried += 1
        elif outcome["reason"] == "credential_like_text":
            # 内容像凭据：不能重试，更不能把 payload 留在库里
            outbox.purge(item["intent_id"], "credential_like_text")
            dead += 1
        else:
            state = outbox.mark_failed(
                item["intent_id"], outcome["reason"] or "write_failed"
            )
            if state == "dead_letter":
                dead += 1
            elif state == "ignored":
                retried += 1
            else:
                retried += 1
    return {"delivered": delivered, "retried": retried, "dead": dead}


def flush_until_empty(outbox: Any, *, max_rounds: int = 20, **kwargs: Any) -> dict[str, int]:
    totals = {"delivered": 0, "retried": 0, "dead": 0, "rounds": 0}
    for _ in range(max(1, max_rounds)):
        batch = drain(outbox, **kwargs)
        totals["rounds"] += 1
        for key in ("delivered", "retried", "dead"):
            totals[key] += batch[key]
        if batch["delivered"] == 0 and batch["retried"] == 0 and batch["dead"] == 0:
            break
    return totals
